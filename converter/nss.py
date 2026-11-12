"""Parser for the subset of NitroScript (.nss) that N2System story scripts use.

It produces a small AST; `saya.py` turns that AST into the player's JSON ops.

File structure handled:
    chapter NAME { ... }   scene NAME { ... }   cut NAME { ... }   function NAME(...) { ... }

Text blocks look like this and are lifted out before the code is tokenised:

    <PRE @box09>
    [text0010]
    <voice name="..." class="..." src="voice/2/000104">
      "Dialogue."<K>
      Narration that wraps
    onto a second source line.{StL("cg/st/x.png");}
    </PRE>
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# pre-pass: strip comments, lift <PRE> blocks out as __TEXT__(n) calls
# --------------------------------------------------------------------------- #

_PRE_OPEN = re.compile(r"<PRE\s+@(\w+)>[^\n]*\n")
_PRE_CLOSE = re.compile(r"^</PRE>", re.M)


@dataclass
class TextBlock:
    box: str
    body: str


def preprocess(source: str) -> tuple[str, list[TextBlock]]:
    out: list[str] = []
    blocks: list[TextBlock] = []
    i, n = 0, len(source)
    line_start = True
    while i < n:
        c = source[i]
        if line_start and c == "<":
            m = _PRE_OPEN.match(source, i)
            if m:
                end = _PRE_CLOSE.search(source, m.end())
                if not end:
                    raise SyntaxError("unterminated <PRE> block")
                blocks.append(TextBlock(m.group(1), source[m.end():end.start()]))
                out.append(f"__TEXT__({len(blocks) - 1});\n")
                i = end.end()
                line_start = False
                continue
        if c == "/" and source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            j = source.find('"', i + 1)
            j = n - 1 if j < 0 else j
            out.append(source[i:j + 1])
            i = j + 1
            line_start = False
            continue
        out.append(c)
        if c == "\n":
            line_start = True
        elif c not in " \t\r":
            line_start = False
        i += 1
    return "".join(out), blocks


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"""
      (?P<ws>\s+)
    | (?P<str>"[^"]*")
    | (?P<num>0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)
    | (?P<op>==|!=|<=|>=|&&|\|\||\+\+|--|\+=|-=|[(){};,=+\-*/!<>:])
    | (?P<word>[$\#@]*[^\s(){};,=+\-*/!<>&|":]+)
    | (?P<junk>.)
""", re.X)


@dataclass
class Tok:
    kind: str   # str | num | op | word | eof
    val: str


def tokenize(code: str) -> list[Tok]:
    toks: list[Tok] = []
    for m in _TOKEN.finditer(code):
        kind = m.lastgroup
        if kind in ("ws", "junk"):
            continue
        toks.append(Tok(kind, m.group()))
    toks.append(Tok("eof", ""))
    return toks


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #
# Expressions are tuples:
#   ("num", 12) ("str", "x") ("bool", True) ("null",) ("var", "$name") ("word", "Dxl2")
#   ("rel", expr)            a coordinate written as @expr (relative to the current value)
#   ("un", "!", a) ("bin", "+", a, b) ("call", "Name", [args])
# Statements are tuples:
#   ("call", name, [args]) ("assign", var, op, expr) ("incr", var, op)
#   ("if", cond, [then], [else]) ("while", cond, [body])
#   ("jump", "call_scene" | "call_cut" | "call_chapter", expr) ("return",)

_BINARY_LEVELS = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/")]


class Parser:
    def __init__(self, toks: list[Tok]):
        self.toks = toks
        self.pos = 0

    # -- helpers
    def peek(self, ahead: int = 0) -> Tok:
        return self.toks[min(self.pos + ahead, len(self.toks) - 1)]

    def next(self) -> Tok:
        tok = self.peek()
        self.pos += 1
        return tok

    def at(self, val: str) -> bool:
        tok = self.peek()
        return tok.kind in ("op", "word") and tok.val == val

    def accept(self, val: str) -> bool:
        if self.at(val):
            self.pos += 1
            return True
        return False

    def expect(self, val: str) -> None:
        if not self.accept(val):
            raise SyntaxError(f"expected {val!r}, found {self.peek().val!r}")

    # -- statements
    def block(self) -> list:
        """Statements up to a closing brace or end of input (brace not consumed)."""
        out = []
        while self.peek().kind != "eof" and not self.at("}"):
            stmt = self.statement()
            if stmt is not None:
                out.append(stmt)
        return out

    def body(self) -> list:
        """A braced block, or a single statement."""
        if self.accept("{"):
            stmts = self.block()
            self.expect("}")
            return stmts
        stmt = self.statement()
        return [] if stmt is None else [stmt]

    def statement(self):
        tok = self.peek()
        if tok.kind == "op":
            if tok.val == ";":
                self.pos += 1
                return None
            if tok.val == "{":
                self.pos += 1
                stmts = self.block()
                self.expect("}")
                return ("if", ("bool", True), stmts, [])
            raise SyntaxError(f"unexpected {tok.val!r}")
        if tok.kind != "word":
            raise SyntaxError(f"unexpected {tok.val!r}")

        word = tok.val
        if word == "if":
            self.pos += 1
            self.expect("(")
            cond = self.expr()
            self.expect(")")
            then = self.body()
            other: list = []
            if self.accept("else"):
                other = self.body()
            return ("if", cond, then, other)
        if word == "while":
            self.pos += 1
            self.expect("(")
            cond = self.expr()
            self.expect(")")
            return ("while", cond, self.body())
        if word in ("call_scene", "call_cut", "call_chapter"):
            self.pos += 1
            target = self.expr()
            self.accept(";")
            return ("jump", word, target)
        if word == "return":
            self.pos += 1
            self.accept(";")
            return ("return",)

        self.pos += 1
        if word[0] in "$#":
            if self.at("=") or self.at("+=") or self.at("-="):
                op = self.next().val
                value = self.expr()
                self.accept(";")
                return ("assign", word, op, value)
            if self.at("++") or self.at("--"):
                op = self.next().val
                self.accept(";")
                return ("incr", word, op)
            raise SyntaxError(f"unexpected token after {word}: {self.peek().val!r}")
        if self.accept("("):
            args = self.args()
            self.accept(";")
            return ("call", word, args)
        raise SyntaxError(f"unexpected word {word!r}")

    def args(self) -> list:
        args = []
        if self.accept(")"):
            return args
        while True:
            args.append(self.expr())
            if self.accept(")"):
                return args
            self.expect(",")

    # -- expressions
    def expr(self, level: int = 0):
        if level == len(_BINARY_LEVELS):
            return self.unary()
        left = self.expr(level + 1)
        while self.peek().kind == "op" and self.peek().val in _BINARY_LEVELS[level]:
            op = self.next().val
            left = ("bin", op, left, self.expr(level + 1))
        return left

    def unary(self):
        if self.accept("!"):
            return ("un", "!", self.unary())
        if self.accept("-"):
            return ("un", "-", self.unary())
        return self.primary()

    def primary(self):
        tok = self.next()
        if tok.kind == "num":
            text = tok.val
            if text[:2].lower() == "0x":
                return ("num", int(text, 16))
            return ("num", float(text) if "." in text else int(text))
        if tok.kind == "str":
            return ("str", tok.val[1:-1])
        if tok.kind == "op" and tok.val == "(":
            inner = self.expr()
            self.expect(")")
            return inner
        if tok.kind == "word":
            word = tok.val
            if word.startswith("@") and len(word) > 1 and word[1] not in "$#":
                rest = word[1:]
                if re.fullmatch(r"\d+", rest):
                    return ("rel", ("num", int(rest)))
                return ("word", word)
            if word == "@":
                return ("rel", self.unary())
            if word.startswith("@"):
                return ("rel", ("var", word[1:]))
            if word[0] in "$#":
                return ("var", word)
            if word == "true":
                return ("bool", True)
            if word == "false":
                return ("bool", False)
            if word == "null":
                return ("null",)
            if self.accept("("):
                return ("call", word, self.args())
            return ("word", word)
        raise SyntaxError(f"unexpected {tok.val!r} in expression")


def parse_statements(code: str) -> list:
    parser = Parser(tokenize(code))
    stmts = parser.block()
    if parser.peek().kind != "eof":
        raise SyntaxError(f"unexpected {parser.peek().val!r}")
    return stmts


# --------------------------------------------------------------------------- #
# file level
# --------------------------------------------------------------------------- #

@dataclass
class Unit:
    kind: str                 # chapter | scene | cut | function
    name: str
    code: str


@dataclass
class ScriptFile:
    units: list[Unit] = field(default_factory=list)
    texts: list[TextBlock] = field(default_factory=list)

    def find(self, kind: str, name: str) -> Unit | None:
        for unit in self.units:
            if unit.kind == kind and unit.name.lower() == name.lower():
                return unit
        return None


_UNIT_HEAD = re.compile(r"^[ \t]*(chapter|scene|cut|function)[ \t]+([^\s{(]+)[^{]*\{", re.M)


def parse_file(source: str) -> ScriptFile:
    code, texts = preprocess(source)
    result = ScriptFile(texts=texts)
    pos = 0
    while True:
        m = _UNIT_HEAD.search(code, pos)
        if not m:
            break
        depth, i = 1, m.end()
        while depth and i < len(code):
            c = code[i]
            if c == '"':
                j = code.find('"', i + 1)
                i = len(code) if j < 0 else j
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        result.units.append(Unit(m.group(1), m.group(2), code[m.end():i - 1]))
        pos = i
    return result


# --------------------------------------------------------------------------- #
# text blocks
# --------------------------------------------------------------------------- #

_TEXT_TAG = re.compile(
    r"<voice\s+(?P<voice>[^>]*)>"
    r"|<(?P<key>[Kk])>"
    r"|</?[Ii]>"
    r"|<PRE></PRE>"
    r"|<se\s+(?P<se>[^>]*)>"
    r"|\{(?P<code>[^}]*)\}",
    re.I,
)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def parse_text_block(body: str) -> tuple[str | None, list[tuple]]:
    """Split a <PRE> body into (text id, parts).

    Parts are ("text", str) | ("key",) | ("voice", attrs) | ("se", attrs) | ("code", str).
    Source lines that begin with two spaces start a new paragraph ("\\n"); other
    lines continue the previous one and are joined with a single space.
    """
    text_id = None
    parts: list[tuple] = []
    seen_text = False

    def add_text(text: str) -> None:
        if not text:
            return
        if parts and parts[-1][0] == "text":
            parts[-1] = ("text", parts[-1][1] + text)
        else:
            parts.append(("text", text))

    for line in body.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if text_id is None:
            m = re.fullmatch(r"\[(\w+)\]", line.strip())
            if m:
                text_id = m.group(1)
                continue
        if line.lstrip().startswith("//"):
            continue

        first = True          # no visible text emitted from this line yet
        pos = 0
        segments: list[tuple] = []
        for m in _TEXT_TAG.finditer(line):
            if m.start() > pos:
                segments.append(("raw", line[pos:m.start()]))
            if m.group("voice") is not None:
                segments.append(("voice", dict(_ATTR.findall(m.group("voice")))))
            elif m.group("key") is not None:
                segments.append(("key",))
            elif m.group("se") is not None:
                segments.append(("se", dict(_ATTR.findall(m.group("se")))))
            elif m.group("code") is not None:
                segments.append(("code", m.group("code")))
            pos = m.end()
        if pos < len(line):
            segments.append(("raw", line[pos:]))

        for seg in segments:
            if seg[0] != "raw":
                parts.append(seg)
                continue
            text = seg[1].replace("&.", ".").replace("&,", ",")
            if first:
                stripped = text.lstrip(" \t　")
                if not stripped:
                    continue
                indent = len(text) - len(stripped)
                first = False
                if seen_text:
                    text = ("\n" if indent >= 2 else " ") + stripped
                else:
                    text = stripped
            add_text(text)
            seen_text = True
    return text_id, parts
