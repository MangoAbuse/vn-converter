"""Disassembler for Ethornell scenario scripts ("BurikoCompiledScriptVer1.00").

Layout: the 28-byte magic, a header (u32 size, u32 0, u32 count, then per entry point a
NUL-terminated name padded to 4 bytes and a u32 code offset), the code, and the strings.
Code is a stream of little-endian u32 words for a small stack machine:

    00 n     push the number n            01 a   push the code address a
    02 n     push a frame-relative address 03 s   push the string at offset s
    08 n     load (n = 0/1/2: byte/word/dword) from the address on the stack
    09 n     store to an address          0a n   same, for an argument
    10 / 11  read / write the frame base   18     jump to the address on the stack
    19 n     conditional jump             1a     call     1b   return
    20..2b   arithmetic, 30..35 comparisons, 38..3a logic
    7f s n   source file name s and line n (debug information before each statement)
    80 and up  engine commands; they take their arguments from the stack

Offsets (code addresses and strings) count from the start of the code.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"BurikoCompiledScriptVer1.00\0"

# opcodes below 0x80 that are followed by one operand word; 0x7f has two
ONE_OPERAND = frozenset({0x00, 0x01, 0x02, 0x03, 0x08, 0x09, 0x0A, 0x19, 0x3F})
TWO_OPERANDS = frozenset({0x7F})


class ScriptError(ValueError):
    pass


@dataclass
class Script:
    entries: dict[str, int]              # entry point name -> code offset
    code: list[tuple[int, int, int, int]]   # (address, opcode, operand 1, operand 2)
    index: dict[int, int]                # address -> position in `code`
    data: bytes                          # code + strings, for reading strings
    code_end: int

    def string(self, offset: int) -> str:
        end = self.data.find(b"\0", offset)
        raw = self.data[offset:end if end >= 0 else len(self.data)]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp932", "replace")


def disassemble(data: bytes, one=ONE_OPERAND, two=TWO_OPERANDS) -> Script:
    if not data.startswith(MAGIC):
        raise ScriptError("not a compiled scenario script")
    (hsize,) = struct.unpack_from("<I", data, len(MAGIC))
    header = data[len(MAGIC):len(MAGIC) + hsize]
    entries: dict[str, int] = {}
    (count,) = struct.unpack_from("<I", header, 8)
    pos = 12
    for _ in range(count):
        end = header.find(b"\0", pos)
        if end < 0 or end + 5 > len(header):
            break
        name = header[pos:end].decode("ascii", "replace")
        (offset,) = struct.unpack_from("<I", header, end + 1)      # not aligned
        pos = end + 5
        entries[name] = offset
    body = data[len(MAGIC) + hsize:]

    code: list[tuple[int, int, int, int]] = []
    index: dict[int, int] = {}
    limit = len(body) & ~3
    first_string = limit
    at = 0
    while at < first_string:
        (op,) = struct.unpack_from("<I", body, at)
        a = b = 0
        size = 4
        if op in one:
            (a,) = struct.unpack_from("<I", body, at + 4)
            size = 8
            if op == 0x03 and a < first_string:
                first_string = a
        elif op in two:
            a, b = struct.unpack_from("<II", body, at + 4)
            size = 12
            if a < first_string:
                first_string = a
        index[at] = len(code)
        code.append((at, op, a, b))
        at += size
    return Script(entries, code, index, body, at)
