"""Little Busters! English Edition (Prototype LUCA System) -> player bundle.

What the script commands mean was worked out from the game's own data, with the
opcode table and message/choice layouts taken from LuckSystem (see luca.py).

Script model
    Every SEENnnnn script becomes one scene. Variables are numbered; `#6001` in an
    expression is variable 6001 and `#6451[expr]` is variable 6451 + expr. FARCALL
    passes its arguments in variables 20011, 20012, ... of the called script.

Commands translated to player ops
    MESSAGE, SELECT                      text / choice
    EQU, EQUN, ADD, SUB, MUL, DIV, MOD, AND, OR, RANDOM, FLAGCLR   variables
    IFN, IFY, GOTO, ONGOTO, GOSUB, RETURN, JUMP, FARCALL, FARRETURN, END   flow
    INIT, IMAGELOAD, DRAW, CLOSE_WINDOW, WAIT, WAIT_COUNT, SHAKELIST, SG_QUAKE  screen
    BGM, SE, KOEP, VOICE, MOVIE          sound and video

Everything else (the battle, baseball and other minigame commands, trophies, CG
flags) is written as an `ext` op that names the command, so the player can skip it
or handle it; nothing is silently dropped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from bundle import write_bundle
from luca import Instruction, PakArchive, cz_decode, cz_header, disassemble, oggpak_extract
from media import MediaConverter

SCREEN_W, SCREEN_H = 1280, 720
FPS = 60                      # script timings are in frames

IMAGE_ARCHIVES = {            # archive -> folder in the bundle
    "BGCG": "bg", "EVENTCG": "ev", "FACECG": "face", "CHARCG": "ch", "OTHCG": "oth",
    "GENCG": "gen", "SYSCG": "sys", "SYSCG2": "sys2", "PARTS": "parts",
}
SOUND_ARCHIVES = {"VOICE0": "voice", "VOICE2": "voice", "MUSIC": "bgm", "SE": "se"}
COLOR_IDS = {65001: "#000000", 65000: "#ffffff"}     # image ids that mean a flat colour

EXT_OPS = {
    "TASK", "BTFUNC", "BATTLE", "BT_ACCESSORY_SELECT", "UNDO_CLEAR", "PTFUNC", "PT", "GMFUNC", "GM",
    "DEL_CALLSTACK", "LBFUNC", "LBBG", "TROPHY", "SETCGFLAG", "SETBGMFLAG", "ARFLAGSET", "EX", "SET",
    "FFSTOP", "HAIKEI_SET", "SCALE", "MOVE", "MOVE2", "FADE", "WIPE", "VOLUME", "BGM_WAITFADE",
    "SHAKELIST_SET", "SAYAVOICETEXT", "VARSTR_SET", "UNKNOWN", "FULLQUAKE_ZOOM",
}


def frames(n: int) -> int:
    return round(n * 1000 / FPS)


# --------------------------------------------------------------------------- #
# expressions
# --------------------------------------------------------------------------- #

_EXPR_TOKEN = re.compile(r"\s*(?:(\d+)|#(\d+)|(==|!=|<=|>=|&&|\|\||[-+*/%<>&|!()\[\]]))")
_LEVELS = [("||",), ("&&",), ("|",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]


class ExprError(ValueError):
    pass


def parse_expr(text: str):
    """C-style expression string -> JSON expression for the player."""
    text = text.strip()
    if not text:
        return 0
    tokens: list[tuple[str, object]] = []
    pos = 0
    while pos < len(text):
        m = _EXPR_TOKEN.match(text, pos)
        if not m:
            raise ExprError(f"cannot read {text!r} at {pos}")
        if m.group(1) is not None:
            tokens.append(("num", int(m.group(1))))
        elif m.group(2) is not None:
            tokens.append(("var", int(m.group(2))))
        else:
            tokens.append(("op", m.group(3)))
        pos = m.end()
    tokens.append(("end", None))
    index = 0

    def peek():
        return tokens[index]

    def take():
        nonlocal index
        tok = tokens[index]
        index += 1
        return tok

    def expect(op):
        if take() != ("op", op):
            raise ExprError(f"expected {op} in {text!r}")

    def binary(level):
        if level == len(_LEVELS):
            return unary()
        left = binary(level + 1)
        while peek()[0] == "op" and peek()[1] in _LEVELS[level]:
            op = take()[1]
            left = [op, left, binary(level + 1)]
        return left

    def unary():
        if peek() == ("op", "-"):
            take()
            inner = unary()
            return -inner if isinstance(inner, int) else ["neg", inner]
        if peek() == ("op", "!"):
            take()
            return ["!", unary()]
        return primary()

    def primary():
        kind, value = take()
        if kind == "num":
            return value
        if kind == "var":
            node = ["var", value]
            while peek() == ("op", "["):
                take()
                inner = binary(0)
                expect("]")
                # #a[i] is variable a+i; #a[i][j] indexes from the value found there
                node = ["at", value, inner] if node[0] == "var" else ["at", node, inner]
            return node
        if (kind, value) == ("op", "("):
            inner = binary(0)
            expect(")")
            return inner
        raise ExprError(f"unexpected {value!r} in {text!r}")

    result = binary(0)
    if peek()[0] != "end":
        raise ExprError(f"trailing input in {text!r}")
    return result


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

_SPEAKER = re.compile(r"`([^@]*)@\s*")


def clean_text(text: str) -> str:
    text = text.replace("❝", "“").replace("❞", "”")
    text = re.sub(r"\$K\d+", "", text)                 # glossary keyword markers
    text = re.sub(r"\$C\[[0-9a-fA-F]*\]", "", text)    # colour
    text = re.sub(r"\$S\d{3}", "", text)               # font size
    text = re.sub(r"\$I\{\d+\}", " ", text)            # tab stop
    text = text.replace("$d", "\n").replace("$w", "").replace("\\_", "")
    return text


def split_speaker(text: str) -> tuple[str | None, str]:
    m = _SPEAKER.match(text)
    if not m:
        return None, text
    return m.group(1).strip() or None, text[m.end():]


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #

class Assets:
    def __init__(self, files_dir: Path, media: MediaConverter):
        self.files_dir = files_dir
        self.media = media
        self.images: dict[int, tuple[PakArchive, object, str]] = {}
        self.sounds: dict[int, tuple[PakArchive, object, str]] = {}
        self.paks: dict[str, PakArchive] = {}
        for name, folder in IMAGE_ARCHIVES.items():
            path = files_dir / f"{name}.PAK"
            if path.exists():
                pak = self.paks[name] = PakArchive(path)
                for entry in pak.entries:
                    self.images.setdefault(entry.id, (pak, entry, folder))
        for name, folder in SOUND_ARCHIVES.items():
            path = files_dir / f"{name}.PAK"
            if path.exists():
                pak = self.paks[name] = PakArchive(path)
                for entry in pak.entries:
                    self.sounds.setdefault(entry.id, (pak, entry, folder))
        self.image_jobs: dict[str, dict] = {}       # out name -> job description
        self.image_meta: dict[int, dict] = {}       # image id -> {"src", "ax", "ay", "base"?}
        self.missing: set[str] = set()
        self.bgm: dict[str, dict] = {}

    # -- images
    def image(self, image_id: int) -> dict | None:
        """Schedule an image and return {"src", "ax", "ay"}; ax/ay is the point of the
        picture that scripts position (the sprite origin, or minus the patch offset).

        Variations are stored as patches on a full image (PakArchive.base_ids):
          * event pictures: the patch is opaque, so it is merged into its base here
          * character sprites: the patch is a small overlay (a face). It stays a
            separate image and the result carries "base", the body to draw under it.
        """
        if image_id in self.image_meta:
            return self.image_meta[image_id]
        hit = self.images.get(image_id)
        if not hit:
            self.missing.add(f"image {image_id}")
            return None
        pak, entry, folder = hit
        with open(pak.path, "rb") as f:
            f.seek(entry.offset)
            head_bytes = f.read(40)
        out = f"img/{folder}/{image_id}.webp"
        job = {"pak": str(pak.path), "offset": entry.offset, "length": entry.length, "base": None,
               "quality": 84 if folder in ("ch", "gen", "face") else 78}
        meta: dict = {"src": out, "ax": 0, "ay": 0}
        if head_bytes[:2] == b"CZ":
            head = cz_header(head_bytes)
            meta["ax"] = head.origin[0] - head.pos[0]
            meta["ay"] = head.origin[1] - head.pos[1]
            base_id = pak.base_ids.get(image_id)
            if base_id and base_id in self.images:
                if folder == "ch":
                    self.image_meta[image_id] = meta        # guards against a table that loops
                    base = self.image(base_id)
                    if base:
                        meta["base"] = {"src": base["src"], "ax": base["ax"], "ay": base["ay"]}
                else:
                    base_pak, base_entry, _ = self.images[base_id]
                    job["base"] = {"pak": str(base_pak.path), "offset": base_entry.offset,
                                   "length": base_entry.length}
                    meta["ax"] = meta["ay"] = 0             # merged result covers the whole canvas
        self.image_jobs[out] = job
        self.image_meta[image_id] = meta
        return meta

    # -- sounds
    def sound(self, sound_id: int) -> str | None:
        hit = self.sounds.get(sound_id)
        if not hit:
            self.missing.add(f"sound {sound_id}")
            return None
        pak, entry, folder = hit

        def load(pak=pak, entry=entry) -> bytes:
            return oggpak_extract(pak.read(entry))

        tag = f"{pak.path.name}:{entry.id}"
        if folder == "voice":
            out = f"audio/voice/{sound_id // 1000:02d}/{sound_id}.m4a"
            return self.media.add_audio(pak.path, out, bitrate="40k", channels=1, rate=32000, loader=load, tag=tag)
        if folder == "bgm":
            out = f"audio/bgm/{entry.name.lower()}.m4a"
            return self.media.add_audio(pak.path, out, bitrate="96k", loader=load, tag=tag)
        out = f"audio/se/{sound_id}.m4a"
        return self.media.add_audio(pak.path, out, bitrate="64k", loader=load, tag=tag)

    def bgm_track(self, sound_id: int) -> str | None:
        hit = self.sounds.get(sound_id)
        if not hit or hit[2] != "bgm":
            self.missing.add(f"bgm {sound_id}")
            return None
        track = hit[1].name.lower()
        if track not in self.bgm:
            self.bgm[track] = {"src": self.sound(sound_id), "loop": None}
        return track


# --------------------------------------------------------------------------- #
# script translation
# --------------------------------------------------------------------------- #

class Context:
    def __init__(self, assets: Assets, strings: dict[int, str]):
        self.assets = assets
        self.strings = strings
        self.warnings: dict[str, int] = {}
        self.op_counts: dict[str, int] = {}
        self.ext_counts: dict[str, int] = {}
        self.speakers: dict[str, int] = {}
        self.movies: dict[str, str] = {}

    def warn(self, message: str) -> None:
        self.warnings[message] = self.warnings.get(message, 0) + 1


class SceneCompiler:
    def __init__(self, name: str, ins: list[Instruction], ctx: Context):
        self.name = name
        self.ins = ins
        self.ctx = ctx
        self.ops: list[dict] = []
        self.index_at: dict[int, int] = {}      # script byte offset -> op index

    def emit(self, op: dict) -> None:
        self.ops.append(op)
        counts = self.ctx.op_counts
        counts[op["op"]] = counts.get(op["op"], 0) + 1

    def expr(self, text: str):
        try:
            return parse_expr(text)
        except ExprError as exc:
            self.ctx.warn(f"expression not understood: {exc}")
            return 0

    def compile(self) -> list[dict]:
        end = 0
        for i in self.ins:
            self.index_at[i.pos] = len(self.ops)
            handler = getattr(self, "op_" + i.op, None)
            try:
                if handler:
                    handler(i)
                else:
                    self.ext(i)
            except (struct.error, IndexError, ExprError) as exc:
                self.ctx.warn(f"{i.op}: could not decode ({type(exc).__name__})")
                self.ext(i)
            end = i.pos
        self.index_at[end + 1_000_000_000] = len(self.ops)
        # local jump targets were stored as byte offsets
        starts = sorted(self.index_at)
        for op in self.ops:
            for key in ("to",):
                if key in op and isinstance(op[key], tuple):
                    op[key] = self.resolve(op[key][1], starts)
            if op["op"] == "switch":
                op["targets"] = [self.resolve(t, starts) for t in op["targets"]]
        return self.ops

    def resolve(self, pos: int, starts: list[int] | None = None) -> int:
        if pos in self.index_at:
            return self.index_at[pos]
        starts = starts or sorted(self.index_at)
        for start in starts:
            if start >= pos:
                return self.index_at[start]
        return len(self.ops)

    def ext(self, i: Instruction, **extra) -> None:
        """A command the player does not need for the story: keep its name and first values."""
        self.ctx.ext_counts[i.op] = self.ctx.ext_counts.get(i.op, 0) + 1
        op = {"op": "ext", "name": i.op, "w": i.words()[:6]}
        op.update(extra)
        self.emit(op)

    # ------------------------------------------------------------- variables
    def target(self, var: int, index_text: str | None):
        return ["at", var, self.expr(index_text)] if index_text is not None else var

    def _strings(self, data: bytes) -> list[str]:
        parts = data.split(b"\x00")
        if parts and parts[-1] == b"":
            parts.pop()
        return [p.decode("cp932", errors="replace") for p in parts]

    def _assign(self, i: Instruction, operator: str | None) -> None:
        var, at = i.u16(0)
        parts = self._strings(i.data[at:])
        if len(parts) == 1:
            index, value = None, parts[0]
        elif len(parts) == 2:
            index, value = parts
        else:
            raise ExprError(f"{i.op} with {len(parts)} strings")
        target = self.target(var, index)
        value_ir = self.expr(value)
        if operator:
            current = ["var", var] if index is None else target
            value_ir = [operator, current, value_ir]
        self.emit({"op": "set", "var": target, "val": value_ir})

    def op_EQU(self, i): self._assign(i, None)
    def op_ADD(self, i): self._assign(i, "+")
    def op_SUB(self, i): self._assign(i, "-")
    def op_MUL(self, i): self._assign(i, "*")
    def op_DIV(self, i): self._assign(i, "/")
    def op_MOD(self, i): self._assign(i, "%")
    def op_AND(self, i): self._assign(i, "&")
    def op_OR(self, i): self._assign(i, "|")

    def op_EQUN(self, i):
        """variable = number. Forms: var | var value | var "index" | var "index" value."""
        var, at = i.u16(0)
        rest = i.data[at:]
        index = None
        value = 0
        if len(rest) == 2:
            value = struct.unpack("<h", rest)[0]
        elif rest:
            end = rest.index(b"\x00")
            index = rest[:end].decode("cp932", errors="replace")
            tail = rest[end + 1:]
            if len(tail) >= 2:
                value = struct.unpack_from("<h", tail)[0]
        self.emit({"op": "set", "var": self.target(var, index), "val": value})

    op_EQUV = op_EQUN

    def op_RANDOM(self, i):
        var, at = i.u16(0)
        parts = self._strings(i.data[at:])
        if len(parts) != 2:
            raise ExprError("RANDOM")
        self.emit({"op": "rand", "var": var, "lo": self.expr(parts[0]), "hi": self.expr(parts[1])})

    def op_FLAGCLR(self, i):
        first, last, value = struct.unpack_from("<HHh", i.data, 0)
        self.emit({"op": "fill", "from": first, "to": last, "val": value})

    def op_VARSTR(self, i):
        slot, _jp, en = struct.unpack_from("<3H", i.data, 0)
        self.emit({"op": "setstr", "slot": slot, "text": self.ctx.strings.get(en, "")})

    # ------------------------------------------------------------------ flow
    def op_IFN(self, i):
        text, at = i.sjis(0)
        target, _ = i.u32(at)
        self.emit({"op": "jz", "cond": self.expr(text), "to": ("pos", target)})

    def op_IFY(self, i):
        text, at = i.sjis(0)
        target, _ = i.u32(at)
        self.emit({"op": "jnz", "cond": self.expr(text), "to": ("pos", target)})

    def op_GOTO(self, i):
        self.emit({"op": "jmp", "to": ("pos", i.u32(0)[0])})

    def op_ONGOTO(self, i):
        text, at = i.sjis(0)
        count = (len(i.data) - at) // 4
        targets = list(struct.unpack_from(f"<{count}I", i.data, at))
        self.emit({"op": "switch", "on": self.expr(text), "targets": targets})

    def op_GOSUB(self, i):
        _arg, at = i.u16(0)
        self.emit({"op": "gosub", "to": ("pos", i.u32(at)[0])})

    def op_RETURN(self, i):
        self.emit({"op": "return"})

    def op_JUMP(self, i):
        name, at = i.sjis(0)
        pos = i.u32(at)[0] if len(i.data) >= at + 4 else 0
        self.emit({"op": "goto", "scene": name.lower(), "at": pos})

    def op_FARCALL(self, i):
        _site, at = i.u16(0)
        name, at = i.sjis(at)
        pos, at = i.u32(at)
        args = [self.expr(a) for a in self._strings(i.data[at:])]
        self.emit({"op": "call", "scene": name.lower(), "at": pos, "args": args})

    def op_FARRETURN(self, i):
        self.emit({"op": "ret"})

    def op_END(self, i):
        self.emit({"op": "end"})

    def op_JUMPPOINT(self, i):
        pass

    # markers for the original engine (screen update bracket, stop fast-forward): nothing to do
    def op_HAIKEI_SET(self, i): pass
    def op_FFSTOP(self, i): pass

    # ------------------------------------------------------------------ text
    def op_MESSAGE(self, i):
        voice, at = i.u16(0)
        _jp, at = i.utf16(at)
        en, at = i.utf16(at)
        tail = i.data[at:]
        end = tail[0] if tail else 5
        who, body = split_speaker(en)
        body = clean_text(body)

        if not body.strip():
            # an empty line after a line that did not wait is just "wait for a tap"
            if end == 5:
                self.emit({"op": "wait"})
            return
        op: dict = {"op": "text", "box": "adv"}
        if who:
            op["who"] = who
            self.ctx.speakers[who] = self.ctx.speakers.get(who, 0) + 1
        items: list[dict] = []
        if voice:
            src = self.ctx.assets.sound(voice)
            if src:
                items.append({"v": src})
        items.append({"t": body})
        if end == 5:
            items.append({"k": 2})
        elif end == 1:
            items.append({"k": 1})
            op["hold"] = True               # the next line continues in the same box
        else:
            op["hold"] = True               # 2: no wait, the script carries on
        if "$(" in body or "$v(" in body or (who and "$(" in who):
            op["dyn"] = True                # contains $(slot,lang) or $v(expr) placeholders
        if len(tail) >= 3:
            face = struct.unpack_from("<H", tail, 1)[0]
            meta = self.ctx.assets.image(face)
            if meta:
                op["face"] = meta["src"]
        op["items"] = items
        self.emit(op)

    def op_SELECT(self, i):
        var, at = i.u16(0)
        at += 6
        _jp, at = i.utf16(at)
        en, at = i.utf16(at)
        options = [clean_text(o).strip() for o in en.split("$d")]
        self.emit({"op": "choice", "options": options, "var": var, "base": 0})

    def op_MESSAGE_CLEAR(self, i):
        self.emit({"op": "textclear"})

    def op_CLOSE_WINDOW(self, i):
        self.emit({"op": "box", "box": "adv", "show": False})

    def op_LOG(self, i): pass
    def op_LOG_PAUSE(self, i): pass
    def op_LOG_END(self, i): pass

    # ---------------------------------------------------------------- screen
    def op_INIT(self, i):
        if len(i.data) != 2:
            return self.ext(i)
        layer, mode = i.data[0], i.data[1]
        if layer == 255 or mode != 255:
            return                          # "keep what is there": nothing to stage
        self.emit({"op": "init", "layer": layer})

    def op_IMAGELOAD(self, i):
        words = i.words()
        layer, slot = words[0] & 0xFF, words[0] >> 8
        image_id = words[1]
        op: dict = {"op": "img", "layer": layer, "slot": slot}
        if len(words) >= 5:
            x, y = struct.unpack_from("<hh", i.data, 6)
            op["x"], op["y"] = x, y
        if image_id in COLOR_IDS:
            op["color"] = COLOR_IDS[image_id]
        else:
            meta = self.ctx.assets.image(image_id)
            if not meta:
                return
            op["src"] = meta["src"]
            if meta["ax"] or meta["ay"]:
                op["ax"], op["ay"] = meta["ax"], meta["ay"]
            if "base" in meta:
                op["base"] = meta["base"]       # body image to draw underneath this face patch
        self.emit(op)

    def op_DRAW(self, i):
        words = i.words()
        op = {"op": "wipe", "ms": frames(words[0]) if words else 0}
        if len(words) > 1:
            op["style"] = words[1]
        self.emit(op)

    def op_WAIT(self, i):
        words = i.words()
        if words:
            self.emit({"op": "wait", "ms": frames(words[0]), "skip": True})

    def op_WAIT_COUNT(self, i):
        words = i.words()
        self.emit({"op": "wait", "ms": frames(words[0]) if words else 0, "skip": True})

    op_WAIT_TIME = op_WAIT_COUNT

    def op_SHAKELIST(self, i):
        words = i.words()
        if words and words[0] >> 8 == 0:        # issued once per layer group; one shake is enough
            self.emit({"op": "shake", "style": words[2] if len(words) > 2 else 0})

    def op_SG_QUAKE(self, i):
        words = i.words()
        if words and words[0] >> 8 == 0:
            self.emit({"op": "shake", "style": 0})

    # ----------------------------------------------------------------- sound
    def op_BGM(self, i):
        if len(i.data) < 4:
            return self.emit({"op": "bgm", "id": None, "ms": 0, "vol": 0})
        sound_id, at = i.u32(0)
        fade = frames(i.u16(at)[0]) if len(i.data) >= at + 2 else 0
        track = self.ctx.assets.bgm_track(sound_id) if sound_id else None
        if track:
            self.emit({"op": "bgm", "id": track, "ms": fade, "vol": 1})
        else:
            self.emit({"op": "bgm", "id": None, "ms": fade, "vol": 0})

    def op_SE(self, i):
        data = i.data
        if len(data) > 16:
            return self.ext(i)
        if not data:
            return self.emit({"op": "sevol", "ch": "*", "ms": 0, "vol": 0})
        channel = data[0]
        if len(data) < 5:
            return self.emit({"op": "sevol", "ch": channel, "ms": 0, "vol": 0})
        sound_id = struct.unpack_from("<I", data, 1)[0]
        loop = len(data) >= 6 and data[5] == 2
        fade = 0
        if len(data) == 7:
            fade = frames(struct.unpack_from("<H", data, 5)[0])
            loop = False
        elif len(data) >= 10:
            fade = frames(struct.unpack_from("<H", data, 8)[0])
        if not sound_id:
            return self.emit({"op": "sevol", "ch": channel, "ms": fade, "vol": 0})
        src = self.ctx.assets.sound(sound_id)
        if src:
            self.emit({"op": "se", "ch": channel, "src": src, "ms": fade, "vol": 1, "loop": loop})

    def op_SE_STOP(self, i):
        self.emit({"op": "sevol", "ch": i.data[0] if i.data else "*", "ms": 0, "vol": 0})

    def op_KOEP(self, i):
        channel, at = i.u16(0)
        sound_id, _ = i.u32(at)
        if sound_id == 0xFFFFFFFF or not sound_id:
            return self.emit({"op": "sevol", "ch": 100 + channel, "ms": 0, "vol": 0})
        src = self.ctx.assets.sound(sound_id)
        if src:
            self.emit({"op": "se", "ch": 100 + channel, "src": src, "ms": 0, "vol": 1, "loop": False,
                       "voice": True})

    def op_VOICE(self, i):
        self.emit({"op": "voicestop"})

    def op_MOVIE(self, i):
        name, _ = i.sjis(0)
        self.ctx.movies.setdefault(name.upper(), f"video/{name.lower()}.mp4")
        self.emit({"op": "video", "src": f"video/{name.lower()}.mp4"})

    # ------------------------------------------------------------- minigames
    def op_BATTLE(self, i):
        """Battle minigame command. The play-by-play lines (101/102) carry text."""
        words = i.words()
        code = words[0] if words else None
        text = None
        try:
            if code in (101, 102):
                at = 2
                flag, at = i.u16(at)
                if code == 101:
                    flag, at = i.u16(at)
                if flag == 0:
                    _extra, at = i.u16(at)
                    _expr, at = i.sjis(at)
                else:
                    at = 4 if code == 101 else 2
                _jp, at = i.utf16(at)
                text, at = i.utf16(at)
        except (struct.error, IndexError):
            text = None
        if text:
            self.ext(i, text=clean_text(text))
        else:
            self.ext(i)


# --------------------------------------------------------------------------- #
# image conversion (CPU-bound, so it runs in worker processes)
# --------------------------------------------------------------------------- #

def _read(part: dict) -> bytes:
    with open(part["pak"], "rb") as f:
        f.seek(part["offset"])
        return f.read(part["length"])


def _convert_image(job: dict) -> str:
    import io

    from PIL import Image

    target = Path(job["target"])
    data = _read(job)
    if data[:2] == b"CZ":
        image, head = cz_decode(data)
        if job["base"]:
            canvas, _ = cz_decode(_read(job["base"]))
            canvas = canvas.convert("RGBA")
            canvas.alpha_composite(image.convert("RGBA"), head.pos)
            image = canvas
    else:
        image = Image.open(io.BytesIO(data))
        image.load()
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
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
        key = json.dumps([job["pak"], job["offset"], job["length"], job["base"], job["quality"]], sort_keys=True)
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
                except Exception as exc:
                    errors.append(f"{job['out']}: {exc}")
                done += 1
                progress(done, total)
    return results, errors


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def progress_printer(label: str):
    state = {"last": -1}

    def show(done: int, total: int) -> None:
        pct = 100 if total == 0 else done * 100 // total
        if pct // 5 != state["last"] // 5 or done == total:
            state["last"] = pct
            print(f"  {label}: {done}/{total} ({pct}%)", flush=True)
    return show


def load_strings(script_pak: PakArchive) -> dict[int, str]:
    """_VARSTR holds the table of names that $(slot,lang) placeholders refer to."""
    entry = script_pak.by_name.get("_VARSTR")
    strings: dict[int, str] = {}
    if entry:
        for ins in disassemble(script_pak.read(entry)):
            if ins.op == "VARSTR_SET":
                string_id, at = ins.u16(0)
                strings[string_id] = ins.utf16(at)[0]
    return strings


def convert(game_dir: Path, work_dir: Path, out_dir: Path, scripts_only: bool = False) -> Path | None:
    game_dir, work_dir, out_dir = Path(game_dir), Path(work_dir), Path(out_dir)
    files_dir = game_dir / "files"
    if not (files_dir / "SCRIPT.PAK").exists():
        sys.exit(f"Missing {files_dir / 'SCRIPT.PAK'}. Point --game at the folder that contains LITBUS_WIN32.exe.")

    print("[1/4] Reading archives")
    media = MediaConverter(work_dir / "cache")
    assets = Assets(files_dir, media)
    script_pak = PakArchive(files_dir / "SCRIPT.PAK")
    print(f"  {len(script_pak.entries)} scripts, {len(assets.images)} images, {len(assets.sounds)} sounds")

    print("[2/4] Translating scripts")
    ctx = Context(assets, load_strings(script_pak))
    scenes: dict[str, list] = {}
    compilers: dict[str, SceneCompiler] = {}
    skipped = []
    for entry in script_pak.entries:
        if not entry.name.upper().startswith("SEEN"):
            continue
        try:
            ins = disassemble(script_pak.read(entry))
        except ValueError:
            skipped.append(entry.name)       # data tables stored next to the scripts
            continue
        compiler = SceneCompiler(entry.name.lower(), ins, ctx)
        scenes[entry.name.lower()] = compiler.compile()
        compilers[entry.name.lower()] = compiler

    # jumps into other scripts were stored as byte offsets in the target script
    for sid, ops in scenes.items():
        for op in ops:
            if op["op"] in ("goto", "call"):
                target = compilers.get(op["scene"])
                if not target:
                    ctx.warn(f"jump to missing script {op['scene']}")
                    op["to"] = 0
                else:
                    op["to"] = target.resolve(op.pop("at"))
                op.pop("at", None)

    messages = ctx.op_counts.get("text", 0)
    print(f"  {len(scenes)} scenes, {sum(len(o) for o in scenes.values())} ops, {messages} text lines, "
          f"{ctx.op_counts.get('choice', 0)} choices")
    if skipped:
        print(f"  not scripts (skipped): {', '.join(skipped)}")

    movies: dict[str, Path] = {}
    for name, out in ctx.movies.items():
        for candidate in (f"{name}_en.webm", f"{name}.webm"):
            path = files_dir / "movie" / candidate
            if path.exists():
                movies[out] = path
                media.add_video(path, out, height=720, crf=28, audio_bitrate="128k")
                break
        else:
            assets.missing.add(f"movie {name}")

    manifest = {
        "id": "little-busters-ee",
        "title": "Little Busters! English Edition",
        "screen": {"w": SCREEN_W, "h": SCREEN_H},
        "start": "seen0513",
        "fps": FPS,
        "boxes": {"adv": {"x": 160, "y": 520, "w": 960, "h": 180, "bg": None, "style": "adv"}},
        "font": {"family": "sans-serif", "size": 30, "lineHeight": 42, "color": "#ffffff", "outline": "#000000"},
        "bgm": assets.bgm,
        "names": {},
        # variables 20000-20999 are local to a FARCALL; 20011.. receive its arguments
        "vars": {"argBase": 20011, "locals": [20000, 20999], "globals": [[10000, 19999]]},
        "ui": {},
        # Minigames run inside the original engine, so the player cannot play them.
        "minigames": {
            # While these commands are running nothing is shown, so timed waits are dropped.
            "rush": ["BATTLE", "BTFUNC", "BT_ACCESSORY_SELECT", "LBBG", "LBFUNC", "GMFUNC", "PT", "PTFUNC"],
            # The battle loop (seen8530) ends when a fighter's stamina, #8188 or #8189, reaches 0.
            # The original lowers it inside BATTLE 111/112; the player picks the loser at random
            # there, and the script's own ending and aftermath then run as written.
            "resolve": [{"name": "BATTLE", "codes": [111, 112], "zeroOneOf": [8188, 8189]}],
        },
    }

    def report(converted: dict[str, Path] | None, stats: dict | None, errors: list[str]) -> None:
        print("\n=== Little Busters! bundle ===")
        if stats:
            print(f"  file:  {out_path}")
            print(f"  size:  {stats['bytes'] / 1e6:.1f} MB in {stats['files']} files")
        if converted:
            by_kind: dict[str, list[int]] = {}
            for name, path in converted.items():
                kind = "/".join(name.split("/")[:2])
                entry = by_kind.setdefault(kind, [0, 0])
                entry[0] += 1
                entry[1] += path.stat().st_size
            for kind in sorted(by_kind):
                count, size = by_kind[kind]
                print(f"    {kind:14} {count:6} files {size / 1e6:8.1f} MB")
        print(f"  media referenced: {len(assets.image_jobs)} images, {len(media.jobs)} audio/video files")
        print("  ops:   " + ", ".join(f"{k}:{v}" for k, v in sorted(ctx.op_counts.items(), key=lambda kv: -kv[1])))
        print("  ext (kept by name, not acted on): "
              + ", ".join(f"{k}:{v}" for k, v in sorted(ctx.ext_counts.items(), key=lambda kv: -kv[1])))
        if assets.missing:
            print(f"  missing assets ({len(assets.missing)}): " + ", ".join(sorted(assets.missing)[:30]))
        if errors:
            print(f"  conversion errors ({len(errors)}):")
            for line in errors[:20]:
                print("    " + line)
        if ctx.warnings:
            print(f"  script warnings ({sum(ctx.warnings.values())}):")
            for line, count in sorted(ctx.warnings.items(), key=lambda kv: -kv[1])[:30]:
                print(f"    x{count} {line}")
        else:
            print("  script warnings: none")

    out_path = out_dir / "little-busters-ee.vnbundle.zip"
    if scripts_only:
        report(None, None, [])
        return None

    print(f"[3/4] Converting media: {len(assets.image_jobs)} images, {len(media.jobs)} audio/video files "
          f"({media.workers} workers)")
    images, image_errors = convert_images(assets.image_jobs, work_dir / "cache", media.workers,
                                          progress_printer("images"))
    converted, errors = media.run(progress_printer("audio/video"))
    converted.update(images)
    errors = image_errors + errors

    print("[4/4] Writing bundle")
    stats = write_bundle(out_path, manifest, scenes, converted, progress_printer("zip"))
    report(converted, stats, errors)
    return out_path
