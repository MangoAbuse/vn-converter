"""Saya no Uta (JAST USA release, Nitroplus N2System) -> player bundle.

The story scripts are written almost entirely with a few dozen macros defined in
nss/function*.nss (BG, WIPE, IMG_CLEAR, BGM_VOL, SE_PLAY, StC ...). Each macro is
translated straight into a player op; the comments next to each handler say which
macro it mirrors.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from bundle import write_bundle
from media import MediaConverter
from npa import NpaArchive
from nss import ScriptFile, parse_file, parse_statements, parse_text_block

ARCHIVES = ("nss", "cg", "sound", "voice")
STORY_FILE = re.compile(r"^sy\d\d_.*\.nss$", re.I)

SCREEN_W, SCREEN_H = 800, 600

# voice "name"/"class" attributes -> (speaker id, display name)
SPEAKERS = {
    "郁紀": ("fuminori", "Fuminori"),
    "沙耶": ("saya", "Saya"),
    "耕司": ("koji", "Koji"),
    "凉子": ("ryoko", "Ryoko"),
    "瑶": ("yoh", "Yoh"),
    "洋佑": ("yosuke", "Yosuke"),
    "青海": ("omi", "Omi"),
    "妻": ("wife", "Wife"),
    "博美": ("hiromi", "Hiromi"),
}

# function.nss ChkExtraCG(): these images are never softened
NO_BLUR = {"ev07", "ev08", "ev10", "ev18", "ev31"}

# variables that only matter to the original engine's bookkeeping
ENGINE_VARS = {
    "$スクリプトバージョン", "$構成名", "$構文名", "$MainGameName", "$SelectGameName",
    "$PreGameName", "$GameContiune", "$AllRead",
}
IGNORED_CALLS = {
    "SetTextWindow", "ShowChapterName", "ShowChapterNameOnly", "Save", "Reset", "SkipOut",
    "SetText", "DeleteSaveFile", "ClearScore", "ClearBacklog",
}
CHOICE_VARS = {"WaitChoice02": "$２択選択", "WaitChoice03": "$３択選択", "WaitChoice04": "$４択選択"}


class NotConst(Exception):
    pass


def scene_id(name: str) -> str:
    name = name.strip().lower()
    return name[:-4] if name.endswith(".nss") else name


def color(value) -> str:
    return "#%06x" % (int(value) & 0xFFFFFF)


def safe_name(path: str) -> str:
    out = []
    for ch in path.lower().replace("\\", "/"):
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "/._-")) else "_u%04x" % ord(ch))
    return "".join(out)


class Assets:
    """Maps the paths used in scripts to extracted files and schedules their conversion."""

    def __init__(self, raw: Path, media: MediaConverter):
        self.raw = raw
        self.media = media
        self.missing: set[str] = set()
        self.index: dict[str, Path] = {}
        for top in ("cg", "sound", "voice"):
            for p in (raw / top).rglob("*"):
                if p.is_file():
                    self.index[p.relative_to(raw).as_posix().lower()] = p

    def _find(self, rel: str, exts: tuple[str, ...]) -> Path | None:
        rel = rel.replace("\\", "/").lower()
        for ext in exts:
            hit = self.index.get(rel + ext)
            if hit:
                return hit
        return None

    def image(self, script_path: str, *, quality: int | None = None, optional: bool = False) -> str | None:
        if not script_path:
            return None
        src = self._find(script_path, ("", ".png", ".jpg", ".bmp"))
        if not src:
            if not optional:
                self.missing.add(script_path)
            return None
        rel = src.relative_to(self.raw / "cg").as_posix()
        if quality is None:
            if rel.startswith("st/"):
                quality = 86
            elif rel.startswith("sys/"):
                quality = 90
            else:
                quality = 80
        out = "img/" + safe_name(rel.rsplit(".", 1)[0]) + ".webp"
        return self.media.add_image(src, out, quality=quality)

    def sound(self, kind: str, script_path: str) -> str | None:
        """kind: bgm | se | voice. script_path has no extension."""
        if not script_path:
            return None
        src = self._find(script_path, (".ogg", ""))
        if not src:
            self.missing.add(script_path)
            return None
        rel = src.relative_to(self.raw).as_posix().rsplit(".", 1)[0]
        if kind == "voice":
            out = "audio/" + safe_name(rel) + ".m4a"                # audio/voice/2/000104.m4a
            return self.media.add_audio(src, out, bitrate="48k", channels=1)
        out = "audio/" + safe_name(rel.split("/", 1)[1]) + ".m4a"   # audio/bgm/s01.m4a
        if kind == "bgm":
            return self.media.add_audio(src, out, bitrate="96k")
        return self.media.add_audio(src, out, bitrate="64k")

    def source(self, script_path: str, exts=(".ogg", "")) -> Path | None:
        return self._find(script_path, exts)


class Context:
    def __init__(self, assets: Assets, loops: dict[str, tuple[int, int]]):
        self.assets = assets
        self.loops = loops
        self.bgm: dict[str, dict] = {}
        self.warnings: list[str] = []
        self.op_counts: dict[str, int] = {}

    def bgm_track(self, name: str) -> str | None:
        track = name.lower()
        if track not in self.bgm:
            src = self.assets.sound("bgm", "sound/bgm/" + name)
            if not src:
                return None
            source = self.assets.source("sound/bgm/" + name)
            loop = self.loops.get(track)
            self.bgm[track] = {
                "src": src,
                "loop": list(loop) if loop else None,
                "ms": self.assets.media.duration_ms(source),
            }
        return track


class SceneCompiler:
    def __init__(self, script: ScriptFile, name: str, ctx: Context):
        self.script = script
        self.name = name                    # e.g. "sy01_01Cafe_Fuminori.nss"
        self.ctx = ctx
        self.ops: list[dict] = []
        self.consts: dict[str, object] = {
            "$GameName": name,
            # fast-forward ("express skip") is a feature of the original engine; the
            # player has its own skip, so these branches are compiled out.
            "$SYSTEM_skip": False,
            "#SYSTEM_skip_express": False,
        }
        self.pending_text: dict | None = None
        self.pending_choice: list[str] | None = None
        self.ended = False

    # ------------------------------------------------------------------ utils
    def warn(self, message: str) -> None:
        self.ctx.warnings.append(f"{scene_id(self.name)}: {message}")

    def emit(self, op: dict) -> None:
        self.ops.append(op)
        self.ctx.op_counts[op["op"]] = self.ctx.op_counts.get(op["op"], 0) + 1

    def const(self, e):
        kind = e[0]
        if kind in ("num", "str", "bool", "word"):
            return e[1]
        if kind == "null":
            return None
        if kind == "var":
            if e[1] in self.consts:
                return self.consts[e[1]]
            raise NotConst(e[1])
        if kind == "un":
            value = self.const(e[2])
            return (not value) if e[1] == "!" else -value
        if kind == "bin":
            op = e[1]
            if op in ("&&", "||"):
                known = []
                for side in (e[2], e[3]):
                    try:
                        known.append(bool(self.const(side)))
                    except NotConst:
                        known.append(None)
                if op == "&&":
                    if False in known:
                        return False
                    if None not in known:
                        return True
                else:
                    if True in known:
                        return True
                    if None not in known:
                        return False
                raise NotConst(op)
            a, b = self.const(e[2]), self.const(e[3])
            if op == "+":
                if isinstance(a, str) or isinstance(b, str):
                    return f"{a}{b}"
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return a / b
            if op == "==":
                return a == b
            if op == "!=":
                return a != b
            if op == "<":
                return a < b
            if op == ">":
                return a > b
            if op == "<=":
                return a <= b
            if op == ">=":
                return a >= b
        raise NotConst(kind)

    def ir(self, e):
        """Expression AST -> JSON expression for the player."""
        kind = e[0]
        if kind in ("num", "str", "bool", "word"):
            return e[1]
        if kind == "null":
            return None
        if kind == "var":
            if e[1] in self.consts:
                return self.consts[e[1]]
            return ["var", e[1]]
        if kind == "un":
            return ["!" if e[1] == "!" else "neg", self.ir(e[2])]
        if kind == "bin":
            return [e[1], self.ir(e[2]), self.ir(e[3])]
        raise NotConst(kind)

    def arg(self, args: list, i: int, default=None):
        if i >= len(args):
            return default
        try:
            return self.const(args[i])
        except NotConst:
            self.warn(f"argument {i} is not a constant")
            return default

    # ------------------------------------------------------------- statements
    def run(self, stmts: list) -> None:
        for stmt in stmts:
            kind = stmt[0]
            if kind == "call":
                self.call(stmt[1], stmt[2])
            elif kind == "assign":
                self.assign(stmt[1], stmt[2], stmt[3])
            elif kind == "if":
                self.branch(stmt[1], stmt[2], stmt[3])
            elif kind == "jump":
                self.jump(stmt[1], stmt[2])
            else:
                self.warn(f"unsupported statement: {kind}")

    def inline(self, kind: str, name: str) -> None:
        unit = self.script.find(kind, name)
        if not unit:
            self.warn(f"{kind} {name} not found")
            return
        self.run(parse_statements(unit.code))

    def jump(self, kind: str, target) -> None:
        try:
            name = self.const(target)
        except NotConst:
            self.warn(f"{kind} with a dynamic target")
            return
        if kind == "call_scene" and isinstance(name, str) and name.startswith("@->"):
            self.inline("scene", name[3:])
        elif kind == "call_cut":
            self.inline("cut", str(name))
        else:
            self.warn(f"unsupported {kind} {name}")

    def assign(self, var: str, op: str, expr) -> None:
        try:
            value = self.const(expr)
            known = True
        except NotConst:
            value, known = None, False

        if var in ENGINE_VARS or re.fullmatch(r"\$txt\d", var):
            if known and op == "=":
                self.consts[var] = value
            else:
                self.consts.pop(var, None)
            return
        if var == "$GameName":
            if not known:
                self.warn("next scene is not a constant")
                return
            self.consts[var] = value
            if value:
                self.emit({"op": "set", "var": "$next", "val": scene_id(value)})
            return
        if var == "$GameCircle":
            self.emit({"op": "end"})
            self.ended = True
            return

        if op == "=":
            if known:
                self.consts[var] = value
                self.emit({"op": "set", "var": var, "val": value})
            else:
                self.consts.pop(var, None)
                self.emit({"op": "set", "var": var, "val": self.ir(expr)})
        else:
            self.consts.pop(var, None)
            self.emit({"op": "set", "var": var, "val": [op[0], ["var", var], self.ir(expr)]})

    def branch(self, cond, then: list, other: list) -> None:
        try:
            taken = self.const(cond)
        except NotConst:
            taken = None
        if taken is not None:
            self.run(then if taken else other)
            return

        before = dict(self.consts)
        jz = {"op": "jz", "cond": self.ir(cond), "to": None}
        self.emit(jz)
        self.run(then)
        after_then = self.consts
        if other:
            jmp = {"op": "jmp", "to": None}
            self.emit(jmp)
            jz["to"] = len(self.ops)
            self.consts = dict(before)
            self.run(other)
            jmp["to"] = len(self.ops)
            after_else = self.consts
        else:
            jz["to"] = len(self.ops)
            after_else = before
        self.consts = {k: v for k, v in after_then.items() if k in after_else and after_else[k] == v}

    # ------------------------------------------------------------------- text
    def speaker(self, attrs: dict) -> str | None:
        for key in ("class", "name"):
            entry = SPEAKERS.get(attrs.get(key, ""))
            if entry:
                return entry[0]
        return None

    def inline_ops(self, code: str) -> list[dict]:
        saved, self.ops = self.ops, []
        try:
            self.run(parse_statements(code))
            sub = self.ops
        except SyntaxError as exc:
            self.warn(f"inline code {code!r}: {exc}")
            sub = []
        finally:
            self.ops = saved
        if any(op["op"] in ("jz", "jmp") for op in sub):
            self.warn("branch inside inline code is not supported")
            sub = [op for op in sub if op["op"] not in ("jz", "jmp")]
        return sub

    def text_block(self, index: int) -> None:
        block = self.script.texts[index]
        text_id, parts = parse_text_block(block.body)
        items: list[dict] = []
        for part in parts:
            kind = part[0]
            if kind == "text":
                items.append({"t": part[1]})
            elif kind == "key":
                items.append({"k": 1})
            elif kind == "voice":
                src = self.ctx.assets.sound("voice", part[1].get("src", ""))
                if src:
                    item = {"v": src}
                    who = self.speaker(part[1])
                    if who:
                        item["who"] = who
                    items.append(item)
            elif kind == "se":
                attrs = part[1]
                src = self.ctx.assets.sound("se", "sound/se/" + attrs.get("src", ""))
                if src:
                    items.append({"do": {"op": "se", "ch": int(attrs.get("id", 1)), "src": src, "ms": 0,
                                         "vol": 1, "loop": attrs.get("loop", "off").lower() == "on"}})
            elif kind == "code":
                items.extend({"do": op} for op in self.inline_ops(part[1]))
        self.pending_text = {"op": "text", "box": block.box, "id": text_id, "keep": 1, "items": items}

    def type_begin(self, keep, log: bool) -> None:
        op = self.pending_text
        self.pending_text = None
        if not op:
            self.warn("TypeBegin without a text block")
            return
        op["keep"] = int(keep or 0)
        if not log:
            op["log"] = False
        items = op["items"]
        if items and "k" in items[-1]:
            items[-1]["k"] = 2
        else:
            items.append({"k": 2})
        self.emit(op)

    # ------------------------------------------------------------------ calls
    def call(self, name: str, args: list) -> None:
        a = lambda i, default=None: self.arg(args, i, default)  # noqa: E731
        assets = self.ctx.assets

        if name == "__TEXT__":
            self.text_block(int(a(0)))
        elif name in IGNORED_CALLS:
            pass
        elif name == "TypeBegin":
            self.type_begin(a(0, 1), True)
        elif name == "TypeBeginNL":
            self.type_begin(a(0, 1), False)

        # ---- screen (function.nss)
        elif name == "IMG_CLEAR":
            self.emit({"op": "clear"})
        elif name == "BG":
            src = assets.image(a(0, ""))
            self.emit({"op": "bg", "src": src} if src else {"op": "bg"})
        elif name == "BG_COLOR":
            value = a(0, "")
            self.emit({"op": "bg"} if value == "" else {"op": "bg", "color": color(value)})
        elif name == "BG_SHADE4":
            path, stem, ext, has_dim = a(0, ""), a(1, ""), a(2, ""), a(3, False)
            op = {"op": "bg", "src": assets.image(path + stem + ext)}
            if has_dim:
                dim = assets.image(path + stem + "_a" + ext, optional=True)
                if dim:
                    op["alt"] = [{"if": ["var", "#extraCG"], "src": dim}]
            if stem not in NO_BLUR:
                op["blurIf"] = ["var", "#Shading"]
            self.emit(op)
        elif name in ("LAYER", "LAYER_XY"):
            n, src, alpha = int(a(0, 0)), assets.image(a(1, "")), a(2, 1000)
            if not src:
                self.emit({"op": "layer", "n": n})
            else:
                self.emit({"op": "layer", "n": n, "src": src, "alpha": alpha / 1000,
                           "x": a(3, 0), "y": a(4, 0)})
        elif name == "LAYER_COLOR":
            n, value, alpha = int(a(0, 0)), a(1, ""), a(2, 1000)
            if value == "":
                self.emit({"op": "layer", "n": n})
            else:
                self.emit({"op": "layer", "n": n, "color": color(value), "alpha": alpha / 1000})
        elif name in ("StC", "StL", "StR", "StCL", "StCR"):
            self.emit({"op": "sprite", "slot": name[2:], "src": assets.image(a(0, ""))})
        elif name == "WIPE":
            kind, ms = a(0, "fade"), int(a(1, 0))
            op = {"op": "wipe", "ms": ms}
            if kind != "fade":
                mask = assets.image(f"cg/data/{kind}.png", quality=92)
                if mask:
                    op["mask"] = mask
            self.emit(op)
        elif name == "FLASH_N":
            self.emit({"op": "flash", "color": color(a(0, 0xFFFFFF)), "count": int(a(1, 1))})
        elif name == "Fade":
            target = str(a(0, ""))
            m = re.fullmatch(r"@?(box\d+)img", target)
            if m:
                self.emit({"op": "box", "box": m.group(1), "show": a(2, 0) > 0})
            else:
                self.warn(f"unsupported Fade target {target}")
        elif name == "Move":
            target = str(a(0, "")).lstrip("@")
            m = re.fullmatch(r"img_layer_(\d)", target)
            if m:
                target = "layer" + m.group(1)
            elif target == "img_bg":
                target = "bg"
            else:
                self.warn(f"unsupported Move target {target}")
                return
            coords = []
            for i in (2, 3):
                expr = args[i]
                if expr[0] == "rel":
                    coords.append({"rel": self.const(expr[1])})
                else:
                    coords.append(self.const(expr))
            self.emit({"op": "move", "target": target, "ms": int(a(1, 0)),
                       "x": coords[0], "y": coords[1], "wait": bool(a(5, False))})
        elif name == "EndScene":
            self.emit({"op": "endscene"})

        # ---- timing
        elif name == "WaitKey":
            if args:
                self.emit({"op": "wait", "ms": int(a(0, 0)), "skip": True})
            else:
                self.emit({"op": "wait"})
        elif name == "Wait":
            self.emit({"op": "wait", "ms": int(a(0, 0))})

        # ---- sound (function.nss / function_sound.nss)
        elif name == "BGM_VOL":
            track, ms, pct = a(0, ""), int(a(1, 0)), a(2, 0)
            # A track the game never defined behaves like "": the current music fades out.
            track_id = None if (track in ("", "*") or not pct) else self.ctx.bgm_track(track)
            if track_id:
                self.emit({"op": "bgm", "id": track_id, "ms": ms, "vol": pct / 100})
            else:
                self.emit({"op": "bgm", "id": None, "ms": ms, "vol": 0})
        elif name == "BGM_LOOP":
            track, spec = a(0, ""), a(1, "NONE")
            if track != "*":
                self.emit({"op": "bgmloop", "id": self.ctx.bgm_track(track), "on": spec != "NONE"})
        elif name == "SE_PLAY":
            src = assets.sound("se", "sound/se/" + str(a(1, "")))
            if src:
                self.emit({"op": "se", "ch": a(0, 1), "src": src, "ms": int(a(2, 0)),
                           "vol": a(3, 100) / 100, "loop": bool(a(4, False))})
        elif name == "SE_VOL":
            self.emit({"op": "sevol", "ch": a(0, "*"), "ms": int(a(1, 0)), "vol": a(2, 0) / 100})
        elif name == "VC_PLAY":
            src = assets.sound("voice", str(a(1, "")))
            if src:
                op = {"op": "se", "ch": a(0, 1), "src": src, "ms": int(a(2, 0)),
                      "vol": a(3, 100) / 100, "loop": bool(a(4, False)), "voice": True}
                who = SPEAKERS.get(str(a(5, "")))
                if who:
                    op["who"] = who[0]
                self.emit(op)
        elif name == "SetBacklog":
            op = {"op": "log", "text": str(a(0, "") or "")}
            voice = a(1)
            if voice:
                src = assets.sound("voice", str(voice))
                if src:
                    op["v"] = src
                who = SPEAKERS.get(str(a(2, "")))
                if who:
                    op["who"] = who[0]
            self.emit(op)
        elif name == "DeleteBoxText":
            self.emit({"op": "textclear"})

        # ---- choices (function_select.nss)
        elif name in ("SetChoice02", "SetChoice03", "SetChoice04", "SetChoice04gr"):
            self.pending_choice = [str(a(i, "")) for i in range(len(args))]
        elif name in CHOICE_VARS:
            var = CHOICE_VARS[name]
            self.consts.pop(var, None)
            self.emit({"op": "choice", "options": self.pending_choice or [], "var": var})
            self.pending_choice = None
        elif name == "SetShadeExtra":
            self.emit({"op": "set", "var": "#Shading", "val": bool(a(0, False))})
            self.emit({"op": "set", "var": "#extraCG", "val": bool(a(1, False))})

        # ---- endings (function_sound.nss)
        elif name == "ShowEndRoll":
            track = self.ctx.bgm_track(str(a(0, "")))
            duration = self.ctx.bgm[track]["ms"] if track else 60000
            staff = assets.image("cg/sys/ed/staff.jpg", quality=72)
            self.emit({"op": "sevol", "ch": "*", "ms": 1000, "vol": 0})
            self.emit({"op": "wait", "ms": 1000, "skip": True})
            self.emit({"op": "bgm", "id": track, "ms": 10, "vol": 1, "loop": False})
            self.emit({"op": "clear"})
            self.emit({"op": "bg", "color": "#000000"})
            self.emit({"op": "roll", "src": staff, "ms": duration + 1000,
                       "skipIf": ["==", ["var", "#ENDING"], 1]})
            self.emit({"op": "wait", "ms": 500, "skip": True})
            self.emit({"op": "wait"})
            self.emit({"op": "bgm", "id": None, "ms": 1000, "vol": 0})
            self.emit({"op": "clear"})
            self.emit({"op": "wipe", "ms": 1500})
            self.emit({"op": "wait", "ms": 500, "skip": True})
        elif name == "ShowEstaff":
            self.emit({"op": "clear"})
            self.emit({"op": "wipe", "ms": 1000})
            self.emit({"op": "bg", "src": assets.image("cg/sys/ed/jast_credits.png")})
            self.emit({"op": "wipe", "ms": 1000})
            self.emit({"op": "wait"})
            self.emit({"op": "clear"})
            self.emit({"op": "wipe", "ms": 1500})
        else:
            self.warn(f"unsupported call {name}")


def parse_loop_table(function_sound: str) -> dict[str, tuple[int, int]]:
    """Read the intro/loop points out of BGM_LOOP() in function_sound.nss."""
    loops = {}
    pattern = re.compile(r'==\s*"(\w+)"\s*\)\s*\{[^}]*?開始ミリ秒\s*=\s*(\d+);[^}]*?終了ミリ秒\s*=\s*(\d+);', re.S)
    start = function_sound.find("function BGM_LOOP")
    for m in pattern.finditer(function_sound, max(start, 0)):
        loops[m.group(1).lower()] = (int(m.group(2)), int(m.group(3)))
    return loops


def progress_printer(label: str):
    state = {"last": -1}

    def show(done: int, total: int) -> None:
        pct = 100 if total == 0 else done * 100 // total
        if pct // 5 != state["last"] // 5 or done == total:
            state["last"] = pct
            print(f"  {label}: {done}/{total} ({pct}%)", flush=True)
    return show


def convert(game_dir: Path, work_dir: Path, out_dir: Path) -> Path:
    game_dir, work_dir, out_dir = Path(game_dir), Path(work_dir), Path(out_dir)
    raw = work_dir / "raw"

    print("[1/4] Extracting archives")
    for name in ARCHIVES:
        target = raw / name
        archive = game_dir / f"{name}.npa"
        if not archive.exists():
            sys.exit(f"Missing {archive}. Point --game at the folder that contains saya.exe.")
        if target.exists() and any(target.iterdir()):
            print(f"  {name}.npa: already extracted")
            continue
        count = NpaArchive(archive).extract_all(target)
        print(f"  {name}.npa: {count} files")

    print("[2/4] Translating scripts")
    media = MediaConverter(work_dir / "cache")
    assets = Assets(raw, media)
    nss_dir = raw / "nss"

    def read(path: Path) -> str:
        return path.read_bytes().decode("cp932", errors="replace")

    sound_script = next((p for p in nss_dir.iterdir() if p.name.lower() == "function_sound.nss"), None)
    loops = parse_loop_table(read(sound_script)) if sound_script else {}
    ctx = Context(assets, loops)

    scenes: dict[str, list] = {}
    for path in sorted(nss_dir.iterdir()):
        if not STORY_FILE.match(path.name):
            continue
        script = parse_file(read(path))
        main = next((u for u in script.units if u.kind == "scene" and u.name.endswith("_MAIN")), None)
        if not main:
            ctx.warnings.append(f"{path.name}: no _MAIN scene")
            continue
        compiler = SceneCompiler(script, main.name[:-5], ctx)
        try:
            compiler.run(parse_statements(main.code))
        except SyntaxError as exc:
            ctx.warnings.append(f"{path.name}: {exc}")
            continue
        if not compiler.ended:
            target = compiler.consts.get("$GameName")
            if isinstance(target, str) and target and target != compiler.name:
                compiler.emit({"op": "goto", "scene": scene_id(target)})
            else:
                compiler.emit({"op": "goto", "scene": ["var", "$next"]})
        scenes[scene_id(compiler.name)] = compiler.ops

    # boot_startscript.nss: first run shows the image-options question, later runs skip it
    scenes["boot"] = [
        {"op": "jz", "cond": ["var", "#FIRST_BOOT"], "to": 2},
        {"op": "goto", "scene": "sy01_01cafe_fuminori"},
        {"op": "goto", "scene": "sy00_00"},
    ]
    if "sy00_00" in scenes:
        # sy00_00's SELECT scene ends with #FIRST_BOOT=true in the original
        ops = scenes["sy00_00"]
        if not any(op["op"] == "set" and op.get("var") == "#FIRST_BOOT" for op in ops):
            ctx.warnings.append("sy00_00: #FIRST_BOOT is never set")

    # check that every goto target exists
    for sid, ops in scenes.items():
        for op in ops:
            targets = []
            if op["op"] == "goto" and isinstance(op["scene"], str):
                targets.append(op["scene"])
            if op["op"] == "set" and op.get("var") == "$next":
                targets.append(op["val"])
            for target in targets:
                if target not in scenes:
                    ctx.warnings.append(f"{sid}: jumps to unknown scene {target}")

    box_dark = assets.image("cg/sys/box/f05.png")
    box_low = assets.image("cg/sys/box/f03.png")
    manifest = {
        "id": "saya-no-uta",
        "title": "Saya no Uta",
        "screen": {"w": SCREEN_W, "h": SCREEN_H},
        "start": "boot",
        # function_box.nss: SetTextWindow() / LoadBoxNN() / SetText()
        "boxes": {
            "box09": {"x": 50, "y": 56, "w": 690, "h": 488, "bg": box_dark, "bgx": 20, "bgy": 50, "style": "nvl"},
            "box05": {"x": 240, "y": 200, "w": 320, "h": 200, "bg": box_dark, "bgx": 20, "bgy": 50, "style": "nvl"},
            "box08": {"x": 60, "y": 406, "w": 690, "h": 200, "bg": box_low, "bgx": 40, "bgy": 400, "style": "nvl"},
        },
        "font": {"family": "Tahoma", "size": 24, "lineHeight": 36, "color": "#ffffff", "outline": "#000000"},
        # function_init.nss: InitSCRN()
        "slots": {"L": 240, "R": 560, "CL": 300, "CR": 500, "C": 400},
        "bgm": ctx.bgm,
        "names": {sid: label for sid, label in SPEAKERS.values()},
        "ui": {
            "title": assets.image("cg/sys/title/title.png"),
            "choice": assets.image("cg/sys/select/select01.png"),
            "choiceActive": assets.image("cg/sys/select/select02.png"),
        },
    }

    text_ops = sum(1 for ops in scenes.values() for op in ops if op["op"] == "text")
    print(f"  {len(scenes)} scenes, {sum(len(o) for o in scenes.values())} ops, {text_ops} text blocks")

    print(f"[3/4] Converting media ({len(media.jobs)} files, {media.workers} workers)")
    converted, errors = media.run(progress_printer("media"))

    print("[4/4] Writing bundle")
    out_path = out_dir / "saya-no-uta.vnbundle.zip"
    stats = write_bundle(out_path, manifest, scenes, converted)

    by_kind: dict[str, list[int]] = {}
    for name, path in converted.items():
        kind = "/".join(name.split("/")[:2]) if name.startswith("audio/") else name.split("/")[0]
        entry = by_kind.setdefault(kind, [0, 0])
        entry[0] += 1
        entry[1] += path.stat().st_size

    print("\n=== Saya no Uta bundle ===")
    print(f"  file:  {out_path}")
    print(f"  size:  {stats['bytes'] / 1e6:.1f} MB in {stats['files']} files")
    for kind in sorted(by_kind):
        count, size = by_kind[kind]
        print(f"    {kind:14} {count:5} files {size / 1e6:8.1f} MB")
    print("  ops:   " + ", ".join(f"{k}:{v}" for k, v in sorted(ctx.op_counts.items(), key=lambda kv: -kv[1])))
    if assets.missing:
        print(f"  missing assets ({len(assets.missing)}): " + ", ".join(sorted(assets.missing)[:20]))
    if errors:
        print(f"  conversion errors ({len(errors)}):")
        for line in errors[:20]:
            print("    " + line)
    if ctx.warnings:
        print(f"  script warnings ({len(ctx.warnings)}):")
        for line in ctx.warnings[:40]:
            print("    " + line)
    else:
        print("  script warnings: none")
    return out_path
