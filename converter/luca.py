"""Readers for Prototype's LUCA System formats (Little Busters! English Edition).

The layouts follow the open-source LuckSystem (github.com/wetor/LuckSystem, MIT; its
licence notice is at the end of this file) and lbee-utils (github.com/G2-Games/lbee-utils,
GPLv3) projects:

  *.PAK    archive: header, entry table (offset in blocks, length), optional names
  CZ0/1/3/4 images: raw, LZW, LZW + line delta, LZW + line delta with split alpha
  OGGPAK   an Ogg Vorbis stream behind a 15-byte header
  scripts  a flat list of instructions: u16 length, u8 opcode, u8 flag, payload
"""
from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

# --------------------------------------------------------------------------- #
# PAK
# --------------------------------------------------------------------------- #

FLAG_IMAGE_INFO = 0x100     # 12 bytes per entry after the entry table
FLAG_NAMES = 0x200          # NUL-terminated names after that


@dataclass
class PakEntry:
    index: int
    id: int
    name: str | None
    offset: int
    length: int
    info: tuple[int, ...] | None = None    # (width, height, x, y, ?, ?) for image archives


class PakArchive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            head = f.read(0x24)
            data_offset, count, id_start, block = struct.unpack_from("<4I", head, 0)
            self.flags = struct.unpack_from("<I", head, 0x20)[0]
            f.seek(0)
            header = f.read(data_offset)
        self.id_start = id_start
        self.block_size = block

        # a few header words of varying number precede the entry table; the table
        # starts at the word equal to the first entry's block offset
        first_block = data_offset // block
        pos = 0x24
        while struct.unpack_from("<I", header, pos)[0] != first_block:
            pos += 4
            if pos + 8 > len(header):
                raise ValueError(f"{self.path.name}: entry table not found")
        table = struct.unpack_from(f"<{count * 2}I", header, pos)
        pos += count * 8

        infos: list[tuple[int, ...] | None] = [None] * count
        if self.flags & FLAG_IMAGE_INFO:
            for i in range(count):
                infos[i] = struct.unpack_from("<6H", header, pos + i * 12)
            pos += count * 12
        names: list[str | None] = [None] * count
        if self.flags & FLAG_NAMES:
            for i in range(count):
                end = header.index(b"\x00", pos)
                names[i] = header[pos:end].decode("cp932")
                pos = end + 1

        self.entries = [
            PakEntry(i, id_start + i, names[i], table[i * 2] * block, table[i * 2 + 1], infos[i])
            for i in range(count)
        ]

        # Image archives may store variations as small patches. One of the u16-per-entry
        # tables that follow gives, for each entry, the id of the full image it patches
        # (0 = it is a full image). Its position varies, so take the first table that
        # consists only of zeros and ids from this archive.
        self.base_ids: dict[int, int] = {}
        for skip in range(3):
            start = pos + skip * count * 2
            if start + count * 2 > len(header):
                break
            values = struct.unpack_from(f"<{count}H", header, start)
            if any(values) and all(v == 0 or id_start <= v < id_start + count for v in values):
                self.base_ids = {id_start + i: v for i, v in enumerate(values) if v}
                break
        self.by_name = {e.name.upper(): e for e in self.entries if e.name}
        self.by_id = {e.id: e for e in self.entries}

    def read(self, entry: PakEntry) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            return f.read(entry.length)


# --------------------------------------------------------------------------- #
# CZ images
# --------------------------------------------------------------------------- #

@dataclass
class CzHeader:
    kind: int                 # 0, 1, 3, 4
    header_length: int
    width: int
    height: int
    depth: int
    block: int                # number of delta blocks (CZ3/CZ4)
    pos: tuple[int, int] = (0, 0)             # where the stored pixels sit inside `bounds`
    bounds: tuple[int, int] | None = None     # size of the full canvas the image belongs to
    origin: tuple[int, int] = (0, 0)          # point of the canvas that scripts position (sprites)


def cz_header(data: bytes) -> CzHeader:
    """Header layout: magic, u32 header length, u16 width, height, depth, u8 delta blocks,
    pad, u16 x, y (position in the canvas), u16 crop w, h, u16 canvas w, h and, when the
    header is 36 bytes long, u16 origin x, y."""
    if data[:2] != b"CZ" or data[3:4] != b"\x00":
        raise ValueError("not a CZ image")
    length, width, height, depth, block = struct.unpack_from("<IHHHB", data, 4)
    head = CzHeader(int(chr(data[2])), length, width, height, depth, block)
    if length >= 28:
        x, y, _cw, _ch, bw, bh = struct.unpack_from("<6H", data, 16)
        head.pos, head.bounds = (x, y), (bw, bh)
    if length >= 32:
        head.origin = struct.unpack_from("<2H", data, 28)
    return head


def lzw_decode(codes) -> bytes:
    """The engine's LZW variant: 16-bit codes, dictionary reset for every block."""
    table = [bytes((i,)) for i in range(256)]
    out = []
    push = out.append
    grow = table.append
    size = 256
    prev = table[codes[0]]
    for code in codes:
        if code < size:
            entry = table[code]
        elif code == size:
            entry = prev + prev[:1]
        else:
            raise ValueError(f"bad LZW code {code}")
        push(entry)
        grow(prev + entry[:1])
        size += 1
        prev = entry
    return b"".join(out)


def _lzw_blocks(data: bytes, pos: int) -> bytes:
    (count,) = struct.unpack_from("<I", data, pos)
    sizes = struct.unpack_from(f"<{count * 2}I", data, pos + 4)
    pos += 4 + count * 8
    parts = []
    for i in range(count):
        n = sizes[i * 2]
        codes = array("H")
        codes.frombytes(data[pos:pos + n * 2])
        pos += n * 2
        parts.append(lzw_decode(codes))
    return b"".join(parts)


def _undelta(image: Image.Image, block_height: int) -> Image.Image:
    """Undo the per-line delta: every row is stored as the difference from the row
    above it, restarting at each block. A doubling prefix sum keeps this in C."""
    width, height = image.size
    out = Image.new(image.mode, image.size)
    for top in range(0, height, block_height):
        block = image.crop((0, top, width, min(top + block_height, height)))
        rows = block.height
        shift = 1
        while shift < rows:
            moved = Image.new(block.mode, block.size)
            moved.paste(block.crop((0, 0, width, rows - shift)), (0, shift))
            block = ImageChops.add_modulo(block, moved)
            shift *= 2
        out.paste(block, (0, top))
    return out


def _palette_image(head: CzHeader, palette: bytes, indices: bytes) -> Image.Image:
    lut = [palette[i * 4:i * 4 + 4] for i in range(256)]
    pixels = b"".join(lut[i] for i in indices[:head.width * head.height])
    return Image.frombytes("RGBA", (head.width, head.height), pixels)


def cz_decode(data: bytes) -> tuple[Image.Image, CzHeader]:
    head = cz_header(data)
    size = (head.width, head.height)
    pos = head.header_length
    npix = head.width * head.height

    if head.kind == 0:
        if head.depth == 32:
            return Image.frombytes("RGBA", size, data[pos:pos + npix * 4]), head
        if head.depth == 24:
            return Image.frombytes("RGB", size, data[pos:pos + npix * 3]), head
        if head.depth == 8:
            return _palette_image(head, data[pos:pos + 1024], data[pos + 1024:]), head
        raise ValueError(f"CZ0 with {head.depth} bits per pixel")

    if head.kind == 1:
        palette = None
        if head.depth == 8:
            palette = data[pos:pos + 1024]
            pos += 1024
        raw = _lzw_blocks(data, pos)
        if head.depth == 32:
            return Image.frombytes("RGBA", size, raw[:npix * 4]), head
        if head.depth == 24:
            return Image.frombytes("RGB", size, raw[:npix * 3]), head
        if head.depth == 8:
            return _palette_image(head, palette, raw), head
        raise ValueError(f"CZ1 with {head.depth} bits per pixel")

    # the delta restarts every ceil(height / blocks) rows; a stored 0 means the default of 3
    block_height = -(-head.height // (head.block or 3))
    if head.kind == 3:
        raw = _lzw_blocks(data, pos)
        if head.depth == 32:
            image = Image.frombytes("RGBA", size, raw[:npix * 4])
        elif head.depth == 24:
            image = Image.frombytes("RGB", size, raw[:npix * 3])
        else:
            raise ValueError(f"CZ3 with {head.depth} bits per pixel")
        return _undelta(image, block_height), head

    if head.kind == 4:
        raw = _lzw_blocks(data, pos)
        rgb = _undelta(Image.frombytes("RGB", size, raw[:npix * 3]), block_height)
        alpha = _undelta(Image.frombytes("L", size, raw[npix * 3:npix * 4]), block_height)
        rgb.putalpha(alpha)
        return rgb, head

    raise ValueError(f"unsupported CZ{head.kind}")


# --------------------------------------------------------------------------- #
# audio
# --------------------------------------------------------------------------- #

def oggpak_extract(data: bytes) -> bytes:
    """Return the Ogg stream inside an OGGPAK entry (or the data itself if it is plain Ogg)."""
    start = data.find(b"OggS", 0, 64)
    if start < 0:
        raise ValueError("no Ogg stream found")
    return data[start:]


# --------------------------------------------------------------------------- #
# scripts
# --------------------------------------------------------------------------- #

# data/LB_EN/OPCODE.txt from LuckSystem: the opcode is the index in this list
LB_EN_OPCODES = """
EQU EQUN EQUV ADD SUB MUL DIV MOD AND OR RANDOM VARSTR SET FLAGCLR GOTO ONGOTO GOSUB IFY IFN
RETURN JUMP FARCALL FARRETURN JUMPPOINT END VARSTR_SET TALKNAME_SET ARFLAGSET COLORBG_SET
SPLINE_SET SHAKELIST_SET MESSAGE MESSAGE_CLEAR SELECT CLOSE_WINDOW LOG LOG_PAUSE LOG_END VOICE
WAIT_COUNT WAIT_TIME FFSTOP INIT STOP IMAGELOAD IMAGEUPADTE ARC MOVE MOVE2 ROT PEND FADE SCALE
SHAKE SHAKELIST BASE MCMOVE MCARC MCROT MCSHAKE MCFADE WAIT DRAW WIPE FRAMEON FRAMEOFF FW SCISSOR
DELAY RASTER TONE SCALECOSSIN BMODE SIZE SPLINE DISP MASK SG_QUAKE BGM BGM_WAITSTART BGM_WAITFADE
SE SE_STOP SE_WAIT VOLUME MOVIE SETCGFLAG EX TROPHY SETBGMFLAG TASK BTFUNC BATTLE KOEP
BT_ACCESSORY_SELECT UNDO_CLEAR PTFUNC PT GMFUNC GM DEL_CALLSTACK FULLQUAKE_ZOOM LBFUNC LBBG
HAIKEI_SET SAYAVOICETEXT UNKNOWN
""".split()


@dataclass
class Instruction:
    pos: int                  # byte offset in the script (jump targets use this)
    op: str
    opcode: int
    fixed: tuple[int, ...]    # 0-2 leading u16 values selected by the flag byte
    data: bytes               # remaining parameter bytes

    # -- parameter readers; each returns (value, next offset)
    def u8(self, at: int) -> tuple[int, int]:
        return self.data[at], at + 1

    def u16(self, at: int) -> tuple[int, int]:
        return struct.unpack_from("<H", self.data, at)[0], at + 2

    def s16(self, at: int) -> tuple[int, int]:
        return struct.unpack_from("<h", self.data, at)[0], at + 2

    def u32(self, at: int) -> tuple[int, int]:
        return struct.unpack_from("<I", self.data, at)[0], at + 4

    def sjis(self, at: int) -> tuple[str, int]:
        """NUL-terminated Shift-JIS string (expressions, file names)."""
        end = self.data.find(b"\x00", at)
        if end < 0:
            end = len(self.data)
        return self.data[at:end].decode("cp932", errors="replace"), end + 1

    def utf16(self, at: int) -> tuple[str, int]:
        """NUL-terminated UTF-16LE string (display text)."""
        end = at
        data = self.data
        while end + 1 < len(data) and (data[end] or data[end + 1]):
            end += 2
        return data[at:end].decode("utf-16-le", errors="replace"), end + 2

    def words(self, at: int = 0) -> list[int]:
        n = (len(self.data) - at) // 2
        return list(struct.unpack_from(f"<{n}H", self.data, at))


def disassemble(data: bytes, opcodes: list[str] = LB_EN_OPCODES) -> list[Instruction]:
    out: list[Instruction] = []
    pos, size = 0, len(data)
    while pos + 4 <= size:
        length, opcode, flag = struct.unpack_from("<HBB", data, pos)
        if length < 4:
            raise ValueError(f"bad instruction length {length} at {pos:#x}")
        raw = data[pos + 4:pos + length]
        nfixed = 2 if flag >= 2 else (1 if flag == 1 else 0)
        fixed = struct.unpack_from(f"<{nfixed}H", raw, 0) if nfixed else ()
        name = opcodes[opcode] if opcode < len(opcodes) else f"OP_{opcode:02X}"
        out.append(Instruction(pos, name, opcode, fixed, raw[nfixed * 2:]))
        pos += (length + 1) & ~1
    return out


# LuckSystem licence notice (the opcode list above and the file layouts were taken from that project):
#
# MIT License
#
# Copyright (c) 2026 WéΤοr
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
