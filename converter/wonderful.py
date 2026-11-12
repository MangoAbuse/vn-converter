"""Converter for Wonderful Everyday (Ethornell / BGI engine) -> bundle for the web player.

The game is read exactly as it is installed, patches included. The engine looks for a file
in this order, and so does the converter:

    1. the loose folders  Script\\CVTD, Graphic\\CVTD, Sound\\CVTD, Voice\\CVTD
    2. loose files in the game folder itself
    3. the archives; when two archives hold the same name, the later archive wins

(1) is how the voice patch replaces every chapter script, (2) is how the small two-file
fix works, (3) is how the publisher's patch replaces pictures of the base game.

Scenario commands (opcode >= 0x80) take their arguments from the stack. The ones this
converter acts on, worked out from how the scripts use them:

    0e0/0e1  set / get a story variable       0e6/0e7  set / get a global flag
    0f0      run another script, then carry on   0f4   end of the story (back to the title)
    110      wait n ms                        112/113  start a timer / wait until it reaches n
    140      text (wait, wait, append, name, text)     143   backlog-only line
    151/153  hide the text window             152      show it
    168      choice (option table, count)     169      chapter menu
    180      music (fade, volume, name, channel)   184/185/186  stop / fade out / volume
    190/198  looping sound                    1a0/1a1  sound effect   1a2/1a3  stop / wait for it
    1a4      extra voice on a channel         1a5      voice of the next line   1a7  wait for it
    1bf      movie (the archive that holds it)
    240/241  background only                  280/281/284  new scene: background, no sprites
    288/289  empty scene
    2c0      sprite (wait, ms, priority, ..., y, x, name, slot)   2c6  change its picture
    2c2/2c4  move it / change it              2c8      remove it
    300/302  picture layer (wait, ms, priority, alpha, ..., pivot y, pivot x, ?, y, x, mask, name, n)
    306      remove a picture layer

Positions are offsets from the middle of the 1280x960 screen. A sprite is anchored at its
bottom centre, which sits at the bottom centre of the screen when x and y are 0. A picture
layer puts its pivot point at the middle of the screen plus (x, y). Sprites and picture
layers share one draw order given by their priority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import bgi
import bgi_script
from bundle import write_bundle
from media import MediaConverter

SCREEN_W, SCREEN_H = 1280, 960
LOOSE_FOLDERS = ("Script/CVTD", "Graphic/CVTD", "Sound/CVTD", "Voice/CVTD")
BINARY = {0x20: "+", 0x21: "-", 0x22: "*", 0x23: "/", 0x24: "%", 0x25: "&", 0x26: "|",
          0x30: "==", 0x31: "!=", 0x32: "<=", 0x33: ">=", 0x34: "<", 0x35: ">", 0x38: "&&", 0x39: "||"}

# The chapter menu (command 169), in the order of the numbers it returns: the script the entry
# starts, the flag that makes it available, and its label. The original draws the names as
# pictures, so the labels are written out here.
CHAPTER_MENU = [
    ("1-2_0000", 230, "Down the Rabbit-Hole II"),
    ("2_0000", 231, "It's my own Invention"),
    ("3_0000", 232, "Looking-glass Insects"),
    ("4_0000", 233, "Jabberwocky"),
    ("5_0000", 234, "Which Dreamed It"),
    ("6-1", 235, "JabberwockyII"),
    ("7-1_sunflower", 204, "Ending: Hill of Sunflowers"),
    ("7-2_wonderful", 205, "Ending: Wonderful Everyday"),
    ("7-3_endofsky", 206, "Ending: End Sky II"),
]

SE_CH, AMB_CH, AMB2_CH, VOICE_CH, BGM2_CH = 10, 20, 30, 40, 50

# commands that leave a value on the stack, with the number of arguments they take
RETURNS = {0x0A0: 1, 0x0E1: 1, 0x0E3: 1, 0x0E5: 1, 0x0E7: 1, 0x101: 0, 0x12C: 0, 0x12D: 0, 0x168: 2, 0x169: 0}


def safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_.\-]", "_", name.lower())


# --------------------------------------------------------------------------- #
# files, in the engine's search order
# --------------------------------------------------------------------------- #

class GameFiles:
    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        self.index: dict[str, tuple[str, int, int]] = {}     # lower name -> (file, offset, size or -1)
        self.origin: dict[str, str] = {}                     # lower name -> where it came from
        for arc_path in sorted(game_dir.glob("*.arc")):      # later archives replace earlier ones
            arc = bgi.Arc(arc_path)
            for key, (_name, offset, size) in arc.entries.items():
                self._add(key, (str(arc_path), offset, size), arc_path.name)
        for path in sorted(game_dir.iterdir()):
            if path.is_file() and path.suffix == "":
                self._add(path.name.lower(), (str(path), 0, -1), "game folder")
        for folder in LOOSE_FOLDERS:
            base = game_dir / folder
            if base.is_dir():
                for path in sorted(base.iterdir()):
                    if path.is_file():
                        self._add(path.name.lower(), (str(path), 0, -1), folder.replace("/", "\\"))

    def _add(self, key: str, part: tuple[str, int, int], origin: str) -> None:
        self.index[key] = part
        self.origin[key] = origin
        if "." in key:                                       # movies are asked for without ".mpg"
            self.index.setdefault(key.rsplit(".", 1)[0], part)
            self.origin.setdefault(key.rsplit(".", 1)[0], origin)

    def head(self, key: str, count: int) -> bytes:
        path, offset, size = self.index[key]
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(count if size < 0 else min(count, size))

    def read(self, key: str) -> bytes:
        return bgi.read_part(*self.index[key])


class Assets:
    def __init__(self, files: GameFiles, media: MediaConverter, work_dir: Path):
        self.files = files
        self.media = media
        self.work_dir = work_dir
        self.image_jobs: dict[str, dict] = {}
        self.images: dict[str, tuple[str, int, int] | None] = {}
        self.sounds: dict[tuple[str, str], tuple[str, int, bool] | None] = {}
        self.movies: dict[str, str | None] = {}
        self.bgm: dict[str, dict] = {}
        self.missing: set[str] = set()
        self.used: Counter = Counter()

    def image(self, name: str) -> tuple[str, int, int] | None:
        """(file in the bundle, width, height) for a picture name, or None."""
        key = name.lower()
        if key in self.images:
            return self.images[key]
        meta = None
        if key not in self.files.index:
            self.missing.add(f"picture {name}")
        else:
            head = self.files.head(key, 64)
            if head.startswith(b"DSC FORMAT"):
                head = bgi.dsc_decompress(self.files.read(key))[:64]
            size = bgi.image_size(head)
            if not size:
                self.missing.add(f"picture {name} (unknown format)")
            else:
                out = f"img/{safe_name(key)}.webp"
                path, offset, length = self.files.index[key]
                self.image_jobs[out] = {"path": path, "offset": offset, "size": length, "quality": 84}
                meta = (out, size[0], size[1])
                self.used[self.files.origin[key]] += 1
        self.images[key] = meta
        return meta

    def sound(self, kind: str, name: str) -> tuple[str, int, bool] | None:
        """(file in the bundle, length in ms, loops) for a sound; kind is bgm, se or voice."""
        key = name.lower()
        if (kind, key) in self.sounds:
            return self.sounds[(kind, key)]
        meta = None
        if key not in self.files.index:
            self.missing.add(f"{kind} {name}")
        else:
            info = bgi.audio_info(self.files.head(key, 0x40))
            if not info:
                self.missing.add(f"{kind} {name} (unknown format)")
            else:
                part = self.files.index[key]
                out = f"audio/{kind}/{safe_name(key)}.m4a"
                tag = f"{key}:{part[1]}:{part[2]}"

                def load(part=part) -> bytes:
                    return bgi.audio_payload(bgi.read_part(*part))

                if kind == "voice":
                    self.media.add_audio(Path(part[0]), out, bitrate="40k", channels=1, rate=32000, loader=load, tag=tag)
                elif kind == "bgm":
                    self.media.add_audio(Path(part[0]), out, bitrate="96k", loader=load, tag=tag)
                else:
                    self.media.add_audio(Path(part[0]), out, bitrate="64k", loader=load, tag=tag)
                ms = info["samples"] * 1000 // info["rate"] if info["rate"] else 0
                meta = (out, ms, bool(info["extra"][0]))
                self.used[self.files.origin[key]] += 1
        self.sounds[(kind, key)] = meta
        return meta

    def bgm_track(self, name: str) -> str | None:
        meta = self.sound("bgm", name)
        if not meta:
            return None
        track = safe_name(name)
        self.bgm[track] = {"src": meta[0], "loop": None, "ms": meta[1]}
        return track

    def movie(self, arc_name: str) -> str | None:
        """A movie is named by the archive that holds it; the file is copied out for ffmpeg."""
        key = arc_name.lower()
        if key in self.movies:
            return self.movies[key]
        out = None
        arc_path = self.files.game_dir / arc_name
        if not arc_path.exists():
            self.missing.add(f"movie {arc_name}")
        else:
            arc = bgi.Arc(arc_path)
            entry = next(iter(arc.entries.values()), None)
            if entry:
                with arc_path.open("rb") as probe:
                    probe.seek(entry[1])
                    if bgi.sniff(probe.read(32)) != "mpeg":
                        entry = None
            if not entry:
                # The game itself plays nothing here either (a patch reused the archive's name).
                self.missing.add(f"movie {arc_name} (the archive holds no movie)")
            else:
                name, offset, size = entry
                target = self.work_dir / "raw" / safe_name(name)
                if not target.exists() or target.stat().st_size != size:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with arc_path.open("rb") as src, target.open("wb") as dst:
                        src.seek(offset)
                        left = size
                        while left > 0:
                            block = src.read(min(left, 8 << 20))
                            if not block:
                                break
                            dst.write(block)
                            left -= len(block)
                out = f"video/{safe_name(name.rsplit('.', 1)[0])}.mp4"
                self.media.add_video(target, out, height=720, crf=27, audio_bitrate="128k")
        self.movies[key] = out
        return out


# --------------------------------------------------------------------------- #
# script translation
# --------------------------------------------------------------------------- #

class Context:
    def __init__(self, assets: Assets, known: set[str]):
        self.assets = assets
        self.known = known
        self.op_counts: Counter = Counter()
        self.ignored: Counter = Counter()
        self.warnings: Counter = Counter()
        self.text_marks: Counter = Counter()
        self.where: dict[str, list[str]] = {}

    def warn(self, message: str, where: str | None = None) -> None:
        self.warnings[message] += 1
        if where and len(self.where.setdefault(message, [])) < 4:
            self.where[message].append(where)


class Compiler:
    def __init__(self, sid: str, script: bgi_script.Script, ctx: Context):
        self.sid = sid
        self.script = script
        self.ctx = ctx
        self.ops: list[dict] = []
        self.pos: dict[int, int] = {}
        self.stack: list = []
        self.strings: dict[int, str] = {}          # text stored in local memory (choice tables)
        self.pending: int | None = None            # staged screen changes waiting for a wipe
        self.sprites: dict[int, dict] = {}
        self.objects: dict[int, int] = {}
        self.voice: tuple[str, int] | None = None
        self.clocks: dict[str, list[int]] = {}     # name -> [length ms, time passed since it started]
        self.bgm_now: str | None = None
        self.log_off = False
        self.targets: set[str] = set()
        self.line = 0

    # ------------------------------------------------------------- plumbing
    def warn(self, message: str) -> None:
        self.ctx.warn(message, f"{self.sid}:{self.line}")

    def emit(self, op: dict) -> None:
        self.ops.append(op)
        self.ctx.op_counts[op["op"]] += 1

    def passed(self, ms: int) -> None:
        for clock in self.clocks.values():
            clock[1] += ms

    def flush(self) -> None:
        if self.pending is not None:
            self.emit({"op": "wipe", "ms": self.pending})
            self.passed(self.pending)
            self.pending = None

    def stage(self, ms: int, wait: bool) -> None:
        self.pending = max(self.pending or 0, max(0, ms))
        if wait:
            self.flush()

    def num(self, value, default: int = 0) -> int:
        return value if isinstance(value, int) else default

    def text_of(self, value) -> str | None:
        return value[1] if isinstance(value, tuple) and value[0] == "str" else None

    def expr(self, value):
        if isinstance(value, (int, list)):
            return value
        if isinstance(value, tuple) and value[0] == "str":
            return value[1]
        self.warn(f"value of kind {value[0] if isinstance(value, tuple) else type(value).__name__} used in an expression")
        return 0

    def pop(self):
        return self.stack.pop() if self.stack else 0

    # ------------------------------------------------------------- main loop
    def compile(self) -> list[dict]:
        code = self.script.code
        jump_targets = {a for _, op, a, _ in code if op == 0x01}
        for addr, op, a, b in code:
            if addr in jump_targets:
                self.flush()
            self.pos[addr] = len(self.ops)
            if op == 0x7F:
                self.stack = []
                self.line = b
            elif op == 0x00:
                self.stack.append(a if a < 0x80000000 else a - 0x100000000)
            elif op == 0x01:
                self.stack.append(("addr", a))
            elif op == 0x02:
                self.stack.append(("ptr", a if a < 0x80000000 else a - 0x100000000))
            elif op == 0x03:
                self.stack.append(("str", self.script.string(a)))
            elif op == 0x08:
                where = self.pop()
                if isinstance(where, tuple) and where[0] == "ptr":
                    self.stack.append(["var", self.local(where[1])])
                else:
                    self.warn("read through a computed address")
                    self.stack.append(0)
            elif op == 0x09:
                value = self.pop()
                self.store(self.pop(), value)
            elif op == 0x0A:
                where = self.pop()
                self.store(where, self.pop())
            elif op == 0x10:
                self.stack.append(("base", 0))
            elif op in (0x11, 0x1E):
                self.pop()
            elif op == 0x1F:
                pass
            elif op == 0x18:
                target = self.pop()
                self.flush()
                self.emit({"op": "jmp", "to": self.target(target)})
            elif op == 0x19:
                target = self.pop()
                cond = self.expr(self.pop())
                self.flush()
                self.emit({"op": "jnz" if a else "jz", "cond": cond, "to": self.target(target)})
            elif op == 0x1B:
                self.flush()
                self.emit({"op": "end"})
            elif op in BINARY:
                y = self.pop()
                x = self.pop()
                self.stack.append(self.binary(BINARY[op], x, y))
            elif op == 0x3A:
                self.stack.append(["!", self.expr(self.pop())])
            elif op >= 0x80:
                if op in RETURNS:                     # takes a fixed number of arguments, leaves a value
                    count = min(RETURNS[op], len(self.stack))
                    args = self.stack[len(self.stack) - count:]
                    del self.stack[len(self.stack) - count:]
                else:
                    args = self.stack
                    self.stack = []
                handler = getattr(self, f"c_{op:03x}", None)
                result = handler(args) if handler else self.other(op, args)
                self.stack.append(result if result is not None else ("void", op))
            else:
                self.warn(f"unhandled instruction {op:02x}")
        self.flush()
        if not self.ops or self.ops[-1]["op"] != "end":
            self.emit({"op": "end"})
        end = len(self.ops) - 1
        for op in self.ops:
            if isinstance(op.get("to"), tuple):
                op["to"] = self.pos.get(op["to"][1], end)
        return self.ops

    def target(self, value):
        if isinstance(value, tuple) and value[0] == "addr":
            return ("addr", value[1])
        self.warn("jump to a computed address")
        return ("addr", -1)

    def binary(self, sign: str, x, y):
        if isinstance(x, int) and isinstance(y, int):
            if sign == "+":
                return x + y
            if sign == "-":
                return x - y
            if sign == "*":
                return x * y
        if isinstance(x, tuple) and x[0] in ("ptr", "base") and isinstance(y, int) and sign in "+-":
            return (x[0], x[1] + (y if sign == "+" else -y))
        return [sign, self.expr(x), self.expr(y)]

    def store(self, where, value) -> None:
        if isinstance(where, tuple) and where[0] == "base":
            return                                           # frame set-up of the script itself
        if not (isinstance(where, tuple) and where[0] == "ptr"):
            self.warn("write through a computed address")
            return
        text = self.text_of(value)
        if text is not None:
            self.strings[where[1]] = text
            return
        self.emit({"op": "set", "var": self.local(where[1]), "val": self.expr(value)})

    def local(self, offset: int) -> str:
        """A script's own variable. The name carries the script, because scripts call each other."""
        return f"${self.sid}.{offset}"

    def other(self, op: int, args: list):
        self.ctx.ignored[f"{op:03x}"] += 1
        return None

    # ------------------------------------------------------------- flow
    def c_0e0(self, a):
        self.set_var("$s", a)

    def c_0e2(self, a):
        self.set_var("$t", a)

    def c_0e4(self, a):
        self.set_var("$u", a)

    def c_0e6(self, a):
        self.set_var("#f", a)

    def set_var(self, prefix: str, a: list) -> None:
        if len(a) >= 2 and isinstance(a[-1], int):
            self.emit({"op": "set", "var": f"{prefix}{a[-1]}", "val": self.expr(a[-2])})
        else:
            self.warn(f"variable {prefix} set with unexpected arguments")

    def get_var(self, prefix: str, a: list):
        if a and isinstance(a[-1], int):
            return ["var", f"{prefix}{a[-1]}"]
        self.warn(f"variable {prefix} read with unexpected arguments")
        return 0

    def c_0e1(self, a):
        return self.get_var("$s", a)

    def c_0e3(self, a):
        return self.get_var("$t", a)

    def c_0e5(self, a):
        return self.get_var("$u", a)

    def c_0e7(self, a):
        return self.get_var("#f", a)

    def c_12c(self, a):
        return 0                                             # "is the reader skipping?": treat as no

    def c_12d(self, a):
        return 0

    def c_0f0(self, a):
        name = self.text_of(a[-1]) if a else None
        self.flush()
        if not name or name.lower() not in self.ctx.known:
            self.warn(f"jump to a script that does not exist ({'computed' if not name else name})")
            self.emit({"op": "end"})
            return
        # The other script runs and, when it ends, this one carries on (endings rely on that).
        self.targets.add(name.lower())
        self.emit({"op": "call", "scene": name.lower()})

    def c_0f4(self, a):
        self.flush()
        self.emit({"op": "goto", "scene": None})             # the story is over: back to the title

    def c_0a0(self, a):
        return a[0] if a else 0                              # a coordinate conversion; only feeds text layout

    def c_101(self, a):
        return 0                                             # the clock, used to time a scene: always "in time"

    def c_168(self, a):
        """Choice. Each option is a 16-byte record: picture, y, x, text (the text is what we show)."""
        base = a[0][1] if len(a) == 2 and isinstance(a[0], tuple) and a[0][0] == "ptr" else None
        count = self.num(a[-1]) if a else 0
        if base is None or count <= 0:
            self.warn("choice with unexpected arguments")
            return 0
        options = [self.strings.get(base + i * 16 + 12, "") for i in range(count)]
        if not all(options):
            self.warn("choice with a missing option text")
        if count > 12:                                       # a screen covered in the same few phrases
            options = list(dict.fromkeys(options))
        self.flush()
        self.emit({"op": "choice", "options": options, "var": "$pick", "base": 0})
        return ["var", "$pick"]

    def c_169(self, a):
        self.flush()
        self.emit({"op": "choice", "var": "$pick", "base": 0,
                   "options": [label for _, _, label in CHAPTER_MENU],
                   "when": [["var", f"#f{flag}"] for _, flag, _ in CHAPTER_MENU]})
        return ["var", "$pick"]

    # ------------------------------------------------------------- timing
    def wait(self, ms: int) -> None:
        if ms > 0:
            self.flush()
            self.emit({"op": "wait", "ms": ms, "skip": True})
            self.passed(ms)

    def c_110(self, a):
        self.wait(self.num(a[-1]) if a else 0)

    def c_112(self, a):
        self.clocks["timer"] = [0, 0]

    def c_113(self, a):
        clock = self.clocks.get("timer", [0, 0])
        self.wait(self.num(a[-1]) - clock[1] if a else 0)

    def wait_for(self, name: str) -> None:
        clock = self.clocks.get(name)
        if clock:
            self.wait(min(clock[0] - clock[1], 60000))
            self.clocks.pop(name, None)

    def c_1a3(self, a):
        self.wait_for(f"se{self.num(a[-1]) if a else 0}")

    def c_1a7(self, a):
        self.wait_for("voice")

    def c_2dc(self, a):
        self.flush()

    # ------------------------------------------------------------- text
    def c_140(self, a):
        if len(a) < 5:
            self.warn("text with unexpected arguments")
            return
        text = self.text_of(a[-1])
        name = self.text_of(a[-2])
        if text is None:
            self.warn("text that is not a constant")
            return
        self.flush()
        text = self.clean(text)
        if not text.strip():
            if self.num(a[0]):
                self.emit({"op": "wait"})
            return
        op: dict = {"op": "text", "box": "adv"}
        if name:
            op["who"] = self.clean(name)
        items: list[dict] = []
        if self.voice:
            items.append({"v": self.voice[0]})
            self.clocks["voice"] = [self.voice[1], 0]
            self.voice = None
        items.append({"t": text})
        if self.num(a[0]):
            items.append({"k": 2})
        else:
            op["hold"] = True
        if self.log_off:
            op["log"] = False
        op["items"] = items
        self.emit(op)

    def clean(self, text: str) -> str:
        for mark in re.findall(r"<[^<>]{0,12}>|\\[a-zA-Z]|[\x00-\x1f]", text):
            self.ctx.text_marks[re.sub(r"[0-9]", "9", re.sub(r"[A-Za-z]", "a", mark)) if len(mark) > 2 else repr(mark)] += 1
        return re.sub(r"</?[A-Za-z]>", "", text.replace("\r", ""))

    def c_143(self, a):
        text = self.text_of(a[0]) if a else None
        if text and text.strip():
            op = {"op": "log", "text": self.clean(text)}
            who = self.text_of(a[1]) if len(a) > 1 else None
            if who:
                op["who"] = self.clean(who)
            self.emit(op)

    def c_144(self, a):
        self.log_off = bool(self.num(a[-1])) if a else False

    def box(self, show: bool) -> None:
        self.emit({"op": "box", "box": "adv", "show": show})

    def c_151(self, a):
        self.box(False)

    def c_152(self, a):
        self.box(True)

    def c_153(self, a):
        self.box(False)

    def c_158(self, a):
        if a and self.num(a[-1], 1) == 0:
            self.box(False)

    # ------------------------------------------------------------- sound
    @staticmethod
    def volume(value) -> float:
        return round(max(0, min(128, value if isinstance(value, int) else 128)) / 128, 3)

    def c_180(self, a):
        if len(a) < 4:
            return
        name = self.text_of(a[2])
        channel = self.num(a[3])
        if not name:
            self.warn("music that is not a constant")
            return
        if channel == 0:
            track = self.ctx.assets.bgm_track(name)
            if track:
                self.bgm_now = track
                self.emit({"op": "bgm", "id": track, "ms": self.num(a[0]), "vol": self.volume(a[1])})
        else:
            meta = self.ctx.assets.sound("bgm", name)
            if meta:
                self.emit({"op": "se", "ch": BGM2_CH + channel, "src": meta[0], "ms": self.num(a[0]),
                           "vol": self.volume(a[1]), "loop": True})

    def music_off(self, channel: int, ms: int) -> None:
        if channel == 0:
            self.bgm_now = None
            self.emit({"op": "bgm", "id": None, "ms": ms, "vol": 0})
        else:
            self.emit({"op": "sevol", "ch": BGM2_CH + channel, "ms": ms, "vol": 0})

    def c_184(self, a):
        self.music_off(self.num(a[-1]) if a else 0, 0)

    def c_185(self, a):
        if len(a) >= 3:
            channel = self.num(a[0]) if self.num(a[0]) < 8 else 0
            self.music_off(channel, self.num(a[1]))
            if self.num(a[2]) == 1:
                self.wait(self.num(a[1]))

    def c_186(self, a):
        if len(a) >= 3:
            channel = self.num(a[2])
            if channel == 0:
                if self.bgm_now:
                    self.emit({"op": "bgm", "id": self.bgm_now, "ms": self.num(a[0]), "vol": self.volume(a[1])})
            else:
                self.emit({"op": "sevol", "ch": BGM2_CH + channel, "ms": self.num(a[0]), "vol": self.volume(a[1])})

    def loop_sound(self, base: int, a: list) -> None:
        if len(a) < 4:
            return
        name = self.text_of(a[2])
        meta = self.ctx.assets.sound("se", name) if name else None
        if meta:
            self.emit({"op": "se", "ch": base + self.num(a[3]), "src": meta[0], "ms": self.num(a[0]),
                       "vol": self.volume(a[1]), "loop": True})

    def c_190(self, a):
        self.loop_sound(AMB_CH, a)

    def c_198(self, a):
        self.loop_sound(AMB2_CH, a)

    def c_194(self, a):
        self.emit({"op": "sevol", "ch": AMB_CH + (self.num(a[-1]) if a else 0), "ms": 0, "vol": 0})

    def c_19c(self, a):
        self.emit({"op": "sevol", "ch": AMB2_CH + (self.num(a[-1]) if a else 0), "ms": 0, "vol": 0})

    def fade_group(self, base: int, a: list) -> None:
        if len(a) >= 3:
            self.emit({"op": "sevol", "ch": base + (self.num(a[0]) if self.num(a[0]) < 8 else 0),
                       "ms": self.num(a[1]), "vol": 0})
            if self.num(a[2]) == 1:
                self.wait(self.num(a[1]))

    def c_195(self, a):
        self.fade_group(AMB_CH, a)

    def c_19d(self, a):
        self.fade_group(AMB2_CH, a)

    def c_196(self, a):
        if len(a) >= 3:
            self.emit({"op": "sevol", "ch": AMB_CH + self.num(a[2]), "ms": self.num(a[0]), "vol": self.volume(a[1])})

    def c_19e(self, a):
        if len(a) >= 3:
            self.emit({"op": "sevol", "ch": AMB2_CH + self.num(a[2]), "ms": self.num(a[0]), "vol": self.volume(a[1])})

    def effect(self, name, volume, channel: int) -> None:
        meta = self.ctx.assets.sound("se", name) if name else None
        if not meta:
            return
        op = {"op": "se", "ch": SE_CH + channel, "src": meta[0], "ms": 0, "vol": self.volume(volume)}
        if meta[2]:
            op["loop"] = True
        self.emit(op)
        self.clocks[f"se{channel}"] = [meta[1], 0]

    def c_1a0(self, a):
        if len(a) >= 4:
            self.effect(self.text_of(a[2]), a[1], self.num(a[3]))

    def c_1a1(self, a):
        if len(a) >= 2:
            self.effect(self.text_of(a[1]), a[0], 0)

    def c_1a2(self, a):
        self.emit({"op": "sevol", "ch": SE_CH + (self.num(a[-1]) if a else 0), "ms": 0, "vol": 0})

    def c_1a4(self, a):
        if len(a) >= 3:
            name = self.text_of(a[1])
            meta = self.ctx.assets.sound("voice", name) if name else None
            if meta:
                self.emit({"op": "se", "ch": VOICE_CH + self.num(a[2]), "src": meta[0], "ms": 0,
                           "vol": self.volume(a[0]), "voice": True})

    def c_1a5(self, a):
        name = self.text_of(a[-1]) if a else None
        meta = self.ctx.assets.sound("voice", name) if name else None
        self.voice = (meta[0], meta[1]) if meta else None

    def c_1a6(self, a):
        self.emit({"op": "voicestop"})

    def c_1bf(self, a):
        name = self.text_of(a[-1]) if a else None
        out = self.ctx.assets.movie(name) if name else None
        if out:
            self.flush()
            self.emit({"op": "video", "src": out})

    # ------------------------------------------------------------- screen
    @staticmethod
    def slot_key(priority: int, layer: bool, index: int) -> int:
        return max(0, priority) * 1000 + (500 if layer else 0) + max(0, min(499, index))

    def scene(self, name, ms: int) -> None:
        """A new scene: the background changes and sprites and picture layers go away."""
        meta = self.ctx.assets.image(name) if name else None
        self.emit({"op": "init", "layer": 1})
        self.sprites.clear()
        self.objects.clear()
        self.emit({"op": "bg", "src": meta[0]} if meta else {"op": "bg"})
        self.stage(ms, True)

    def c_280(self, a):
        if len(a) >= 2:
            self.scene(self.text_of(a[-1]), self.num(a[0]))

    c_281 = c_280
    c_284 = c_280

    def c_288(self, a):
        self.scene(None, self.num(a[0]) if a else 0)

    c_289 = c_288

    def background(self, a: list) -> None:
        name = self.text_of(a[-1]) if a else None
        if len(a) >= 3 and name:
            meta = self.ctx.assets.image(name)
            if meta:
                self.emit({"op": "bg", "src": meta[0]})
                self.stage(self.num(a[1]), self.num(a[0]) != 0)

    def c_240(self, a):
        self.background(a)

    c_241 = c_240
    c_242 = c_240
    c_248 = c_240

    def place(self, slot: int, name: str | None, x: int, y: int, priority: int) -> bool:
        old = self.sprites.get(slot)
        name = name or (old["name"] if old else None)
        meta = self.ctx.assets.image(name) if name else None
        key = self.slot_key(priority, False, slot)
        if old and (old["key"] != key or not meta):
            self.emit({"op": "img", "layer": 1, "slot": old["key"]})
            self.sprites.pop(slot, None)
        if not meta:
            return False
        out, width, height = meta
        self.emit({"op": "img", "layer": 1, "slot": key, "src": out, "x": SCREEN_W // 2 + x, "y": SCREEN_H + y,
                   "ax": width // 2, "ay": height})
        self.sprites[slot] = {"key": key, "name": name, "x": x, "y": y, "priority": priority}
        return True

    def c_2c0(self, a):
        if len(a) != 16:
            self.warn(f"sprite command with {len(a)} arguments")
            return
        name = self.text_of(a[14])
        if not name:
            self.warn("sprite whose picture is not a constant")
            return
        self.place(self.num(a[15]), name, self.num(a[13]), self.num(a[12]), self.num(a[2]))
        self.stage(self.num(a[1]), self.num(a[0]) != 0)

    def c_2c6(self, a):
        if len(a) != 4:
            self.warn(f"sprite change with {len(a)} arguments")
            return
        slot = self.num(a[3])
        old = self.sprites.get(slot, {"x": 0, "y": 0, "priority": 0})
        name = self.text_of(a[2])
        if name:
            self.place(slot, name, old["x"], old["y"], old["priority"])
            self.stage(self.num(a[1]), self.num(a[0]) != 0)

    def c_2c2(self, a):
        if len(a) != 8:
            self.warn(f"sprite move with {len(a)} arguments")
            return
        slot = self.num(a[7])
        old = self.sprites.get(slot)
        name = self.text_of(a[6])
        if not name and not old:
            return
        if self.place(slot, name, self.num(a[5]), self.num(a[4]), old["priority"] if old else 0):
            self.stage(self.num(a[1]), self.num(a[0]) != 0)

    c_2c4 = c_2c2

    def c_2c8(self, a):
        if len(a) != 8:
            self.warn(f"sprite removal with {len(a)} arguments")
            return
        old = self.sprites.pop(self.num(a[7]), None)
        if old:
            self.emit({"op": "img", "layer": 1, "slot": old["key"]})
            self.stage(self.num(a[1]), self.num(a[0]) != 0)

    def layer(self, a: list, name_at: int) -> None:
        name = self.text_of(a[name_at])
        index = self.num(a[name_at + 1])
        if not name:
            self.warn("picture layer whose picture is not a constant")
            return
        meta = self.ctx.assets.image(name)
        old = self.objects.pop(index, None)
        key = self.slot_key(self.num(a[2]), True, index)
        if old is not None and (old != key or not meta):
            self.emit({"op": "img", "layer": 1, "slot": old})
        if not meta:
            return
        op = {"op": "img", "layer": 1, "slot": key, "src": meta[0],
              "x": SCREEN_W // 2 + self.num(a[12]) - self.num(a[9]),
              "y": SCREEN_H // 2 + self.num(a[11]) - self.num(a[8]), "ax": 0, "ay": 0}
        alpha = self.num(a[3])
        if 0 < alpha < 256:
            op["alpha"] = round(alpha / 256, 3)
        self.emit(op)
        self.objects[index] = key
        self.stage(self.num(a[1]), self.num(a[0]) != 0)

    def c_300(self, a):
        if len(a) != 16:
            self.warn(f"picture layer with {len(a)} arguments")
            return
        self.layer(a, 14)

    def c_302(self, a):
        if len(a) != 19:
            self.warn(f"animated picture layer with {len(a)} arguments")
            return
        self.layer(a, 17)

    def c_306(self, a):
        if len(a) != 3:
            return
        old = self.objects.pop(self.num(a[2]), None)
        if old is not None:
            self.emit({"op": "img", "layer": 1, "slot": old})
            self.stage(self.num(a[1]), self.num(a[0]) != 0)

    def c_3f1(self, a):
        self.emit({"op": "shake", "style": self.num(a[0]) if a else 0})

    def c_236(self, a):
        self.emit({"op": "shake", "style": 0})

    c_237 = c_236


# --------------------------------------------------------------------------- #
# pictures (worker processes)
# --------------------------------------------------------------------------- #

def _convert_image(job: dict) -> str:
    target = Path(job["target"])
    image = bgi.decode_image(bgi.read_part(job["path"], job["offset"], job["size"]))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if image.mode in ("LA", "PA") else "RGB")
    if image.mode == "RGBA" and image.getextrema()[3][0] == 255:
        image = image.convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.stem + ".part.webp")
    image.save(tmp, "WEBP", quality=job["quality"], alpha_quality=90, method=4)
    os.replace(tmp, target)
    return job["out"]


def convert_images(jobs: dict[str, dict], cache_dir: Path, workers: int, progress) -> tuple[dict[str, Path], list[str]]:
    results: dict[str, Path] = {}
    errors: list[str] = []
    todo = []
    for out, job in jobs.items():
        stamp = os.stat(job["path"])
        key = json.dumps([job["path"], job["offset"], job["size"], stamp.st_size, int(stamp.st_mtime), job["quality"]])
        digest = hashlib.sha1(key.encode()).hexdigest()
        target = cache_dir / digest[:2] / (digest + ".webp")
        if target.exists() and target.stat().st_size > 0:
            results[out] = target
        else:
            todo.append(dict(job, out=out, target=str(target)))
    done, total = len(results), len(jobs)
    progress(done, total)
    if todo:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_convert_image, job): job for job in todo}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    results[future.result()] = Path(job["target"])
                except Exception as exc:      # keep going; report at the end
                    errors.append(f"{job['out']}: {type(exc).__name__}: {exc}")
                done += 1
                progress(done, total)
    return results, errors


def progress_printer(label: str):
    state = {"last": -1}

    def show(done: int, total: int) -> None:
        pct = 100 if total == 0 else done * 100 // total
        if pct // 5 != state["last"] // 5 or done == total:
            state["last"] = pct
            print(f"  {label}: {done}/{total} ({pct}%)", flush=True)
    return show


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def convert(game_dir: Path, work_dir: Path, out_dir: Path, scripts_only: bool = False) -> Path | None:
    game_dir, work_dir, out_dir = Path(game_dir), Path(work_dir), Path(out_dir)
    if not (game_dir / "BGI.exe").exists():
        sys.exit(f"Missing {game_dir / 'BGI.exe'}. Point --game at the folder that contains BGI.exe.")

    print("[1/4] Reading the game folder")
    files = GameFiles(game_dir)
    by_origin = Counter(files.origin.values())
    print(f"  {len(files.index)} files: " + ", ".join(f"{k} {v}" for k, v in sorted(by_origin.items()) if "CVTD" in k or k == "game folder"))
    media = MediaConverter(work_dir / "cache")
    assets = Assets(files, media, work_dir)

    print("[2/4] Translating scripts")
    loaded: dict[str, bgi_script.Script] = {}

    def load(name: str) -> bgi_script.Script | None:
        if name not in loaded:
            script = None
            if name in files.index:
                data = bgi.dsc_decompress(files.read(name))
                if data.startswith(bgi_script.MAGIC):
                    script = bgi_script.disassemble(data)
            loaded[name] = script
        return loaded[name]

    known = {key for key, origin in files.origin.items()
             if (origin.startswith("data01") or origin in ("game folder", "Script\\CVTD"))
             and files.head(key, 16).startswith((b"BurikoCompiled", b"DSC FORMAT")) and load(key) is not None}
    ctx = Context(assets, known)
    scenes: dict[str, list] = {}
    queue = ["main"]
    while queue:
        sid = queue.pop()
        if sid in scenes:
            continue
        script = load(sid)
        if script is None:
            ctx.warn(f"script {sid} is missing")
            scenes[sid] = [{"op": "end"}]
            continue
        compiler = Compiler(sid, script, ctx)
        scenes[sid] = compiler.compile()
        queue.extend(sorted(compiler.targets - set(scenes)))
    unreached = sorted(known - set(scenes))
    print(f"  {len(scenes)} scenes, {sum(len(o) for o in scenes.values())} ops, {ctx.op_counts.get('text', 0)} text lines, "
          f"{ctx.op_counts.get('choice', 0)} choices")
    print(f"  scripts not reached from the start ({len(unreached)}): {', '.join(unreached)}")

    manifest = {
        "id": "wonderful-everyday",
        "title": "Wonderful Everyday",
        "screen": {"w": SCREEN_W, "h": SCREEN_H},
        "start": "main",
        "boxes": {"adv": {"x": 150, "y": 728, "w": 980, "h": 212, "bg": None, "style": "adv"}},
        "font": {"family": "sans-serif", "size": 32, "lineHeight": 44, "color": "#ffffff", "outline": "#000000"},
        "bgm": assets.bgm,
        "names": {},
        "ui": {},
    }

    def report(converted: dict[str, Path] | None, stats: dict | None, errors: list[str]) -> None:
        print("\n=== Wonderful Everyday bundle ===")
        if stats:
            print(f"  file:  {out_path}")
            print(f"  size:  {stats['bytes'] / 1e6:.1f} MB in {stats['files']} files")
        if converted:
            by_kind: dict[str, list[int]] = {}
            for name, path in converted.items():
                kind = "/".join(name.split("/")[:2]) if name.startswith("audio/") else name.split("/")[0]
                entry = by_kind.setdefault(kind, [0, 0])
                entry[0] += 1
                entry[1] += path.stat().st_size
            for kind in sorted(by_kind):
                count, size = by_kind[kind]
                print(f"    {kind:14} {count:6} files {size / 1e6:8.1f} MB")
        print(f"  media referenced: {len(assets.image_jobs)} pictures, {len(media.jobs)} audio/video files")
        print("  taken from: " + ", ".join(f"{k} {v}" for k, v in sorted(assets.used.items())))
        print("  ops:   " + ", ".join(f"{k}:{v}" for k, v in sorted(ctx.op_counts.items(), key=lambda kv: -kv[1])))
        print("  commands not acted on: " + ", ".join(f"{k}:{v}" for k, v in sorted(ctx.ignored.items(), key=lambda kv: -kv[1])))
        if ctx.text_marks:
            print("  markup seen in text: " + ", ".join(f"{k} x{v}" for k, v in ctx.text_marks.most_common(20)))
        if assets.missing:
            print(f"  missing assets ({len(assets.missing)}): " + ", ".join(sorted(assets.missing)[:40]))
        if errors:
            print(f"  conversion errors ({len(errors)}):")
            for line in errors[:20]:
                print("    " + line)
        if ctx.warnings:
            print(f"  script warnings ({sum(ctx.warnings.values())}):")
            for line, count in sorted(ctx.warnings.items(), key=lambda kv: -kv[1])[:30]:
                print(f"    x{count} {line}" + (f"   at {', '.join(ctx.where[line])}" if line in ctx.where else ""))
        else:
            print("  script warnings: none")

    out_path = out_dir / "wonderful-everyday.vnbundle.zip"
    if scripts_only:
        report(None, None, [])
        return None

    print(f"[3/4] Converting media: {len(assets.image_jobs)} pictures, {len(media.jobs)} audio/video files "
          f"({media.workers} workers)")
    images, image_errors = convert_images(assets.image_jobs, work_dir / "cache", media.workers, progress_printer("pictures"))
    converted, errors = media.run(progress_printer("audio/video"))
    converted.update(images)
    errors = image_errors + errors

    print("[4/4] Writing bundle")
    stats = write_bundle(out_path, manifest, scenes, converted, progress_printer("zip"))
    report(converted, stats, errors)
    return out_path
