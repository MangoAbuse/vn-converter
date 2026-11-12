"""Readers for the Ethornell / BGI engine (BURIKO General Interpreter).

  *.arc         "BURIKO ARC20": u32 count, then 0x80-byte entries (name[0x60], offset, size);
                the older "PackFile    " layout uses 0x20-byte entries (name[0x10], offset, size).
                Offsets count from the end of the entry table.
  DSC           "DSC FORMAT 1.00": Huffman coding plus back-references. Wraps scripts, system
                programs and system pictures.
  CompressedBG  "CompressedBG___": pictures.
                Version 1 is lossless: Huffman coding, zero runs, then an average predictor.
                Version 2 is JPEG data in a different wrapper: 8x8 DCT blocks, two quantiser
                tables, full-range YCbCr without subsampling, the JPEG zigzag order and the
                JPEG way of writing coefficient values. Only the Huffman coding and the order
                of the blocks differ, and transparency is stored as a separate stream. This
                reader therefore rewrites the coefficients as an ordinary JPEG in memory and
                lets Pillow decode that, which is far faster than doing the DCT in Python.
  audio         a 0x40-byte header tagged "bw  ", followed by an ordinary Ogg Vorbis stream
  raw picture   u16 width, u16 height, u32 bits per pixel, 8 zero bytes, then BGR(A) pixels

The DSC and CompressedBG layouts were learned from GARbro's Ethornell readers
(ArcFormats/Ethornell, Copyright (C) 2015-2018 by morkt, MIT licence, see the notice at
the end of this file) and from arc-reader; this is an independent Python implementation.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None


class BgiError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# archives
# --------------------------------------------------------------------------- #

class Arc:
    """One .arc file. `entries` maps the lower-case name to (name, offset, size)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, tuple[str, int, int]] = {}
        with self.path.open("rb") as f:
            magic = f.read(12)
            if magic == b"BURIKO ARC20":
                record, name_len = 0x80, 0x60
            elif magic == b"PackFile    ":
                record, name_len = 0x20, 0x10
            else:
                return                                  # not an archive (the game ships a blank one)
            (count,) = struct.unpack("<I", f.read(4))
            table = f.read(count * record)
            base = 16 + count * record
            for i in range(count):
                rec = table[i * record:(i + 1) * record]
                name = rec[:name_len].split(b"\0", 1)[0].decode("cp932", "replace")
                offset, size = struct.unpack_from("<II", rec, name_len)
                self.entries[name.lower()] = (name, base + offset, size)

    def read(self, name: str) -> bytes:
        _, offset, size = self.entries[name.lower()]
        with self.path.open("rb") as f:
            f.seek(offset)
            return f.read(size)


def read_part(path: str, offset: int, size: int) -> bytes:
    """Read a byte range (size < 0: the whole file). Used by worker processes."""
    with open(path, "rb") as f:
        if size < 0:
            return f.read()
        f.seek(offset)
        return f.read(size)


def sniff(head: bytes) -> str:
    """Rough type of a file from its first 32 bytes."""
    if head.startswith(b"DSC FORMAT 1.00"):
        return "dsc"
    if head.startswith(b"CompressedBG___"):
        return "cbg"
    if head.startswith(b"BurikoCompiledScriptVer1.00"):
        return "script"
    if head[4:8] == b"bw  ":
        return "audio"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"BF_Movie"):
        return "bfmovie"
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "mpeg"
    if len(head) >= 16:
        w, h, bpp = struct.unpack_from("<HHI", head)
        if 0 < w <= 8192 and 0 < h <= 8192 and bpp in (8, 24, 32) and head[8:16] == b"\0" * 8:
            return "raw"
    return "other"


# --------------------------------------------------------------------------- #
# DSC
# --------------------------------------------------------------------------- #

def dsc_decompress(data: bytes) -> bytes:
    """Unpack a "DSC FORMAT 1.00" file; anything else is returned unchanged."""
    if not data.startswith(b"DSC FORMAT 1.00"):
        return data
    key, dst_size, dec_count, _ = struct.unpack_from("<IIII", data, 0x10)
    magic = struct.unpack_from("<H", data, 0)[0] << 16
    leaves = []
    for n in range(512):
        v0 = (20021 * (key & 0xFFFF)) & 0xFFFFFFFF
        v1 = (magic | (key >> 16)) & 0xFFFFFFFF
        v1 = (v1 * 20021 + key * 346) & 0xFFFFFFFF
        v1 = (v1 + (v0 >> 16)) & 0xFFFF
        key = ((v1 << 16) + (v0 & 0xFFFF) + 1) & 0xFFFFFFFF
        depth = (data[0x20 + n] - v1) & 0xFF
        if depth:
            leaves.append((depth << 16) + n)
    leaves.sort()

    is_parent = [False] * 1024
    code = [0] * 1024
    left = [0] * 1024
    right = [0] * 1024
    level = [0]
    n_nodes = 1
    cur = 0
    depth = 0
    while cur < len(leaves):
        following = []
        i = 0
        while cur < len(leaves) and depth == (leaves[cur] >> 16):
            code[level[i]] = leaves[cur] & 0x1FF
            cur += 1
            i += 1
        for node in level[i:]:
            is_parent[node] = True
            left[node] = n_nodes
            right[node] = n_nodes + 1
            following += [n_nodes, n_nodes + 1]
            n_nodes += 2
        level = following
        depth += 1

    out = bytearray()
    buf = data + bytes(8)
    pos = 0x220
    acc = 0
    bits = 0
    for _ in range(dec_count):
        node = 0
        while True:
            if bits == 0:
                acc = buf[pos]
                pos += 1
                bits = 8
            bits -= 1
            node = right[node] if (acc >> bits) & 1 else left[node]
            if not is_parent[node]:
                break
        value = code[node]
        if value >= 256:
            while bits < 12:
                acc = ((acc & ((1 << bits) - 1)) << 8) | buf[pos]
                pos += 1
                bits += 8
            bits -= 12
            offset = ((acc >> bits) & 0xFFF) + 2
            count = (value & 0xFF) + 2
            start = len(out) - offset
            if start < 0:
                raise BgiError("DSC back-reference before the start of the data")
            if offset >= count:
                out += out[start:start + count]
            else:
                for k in range(count):
                    out.append(out[start + k])
        else:
            out.append(value)
    if len(out) != dst_size:
        raise BgiError(f"DSC size mismatch: {len(out)} instead of {dst_size}")
    return bytes(out)


# --------------------------------------------------------------------------- #
# CompressedBG
# --------------------------------------------------------------------------- #

PEEK = 12
PEEK_MASK = (1 << PEEK) - 1
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)

# Huffman tables of the temporary JPEG: every code has the same length, which keeps the
# writer trivial. DC: 16 size classes as 5-bit codes. AC: end-of-block, 16-zero-run and
# every (run, size) pair as 8-bit codes, numbered in the order of AC_VALUES.
AC_VALUES = bytes([0x00, 0xF0] + [(run << 4) | size for size in range(1, 16) for run in range(16)])
AC_CODE = [0] * 256
for _index, _symbol in enumerate(AC_VALUES):
    AC_CODE[_symbol] = _index
AC_EOB, AC_ZRL = 0, 1


def _varints(data: bytes, pos: int, count: int) -> tuple[list[int], int]:
    out = []
    for _ in range(count):
        value = 0
        shift = 0
        while True:
            byte = data[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        out.append(value)
    return out, pos


def _huffman(weights: list[int], v2: bool):
    """Build the engine's Huffman tree. Returns (left, right, is_parent, root)."""
    weight = list(weights)
    valid = [w != 0 for w in weights]
    left = [-1] * len(weights)
    right = [-1] * len(weights)
    parent = [False] * len(weights)
    total = sum(weights)
    while True:
        pair = [-1, -1]
        combined = 0
        for i in range(2):
            best = 0xFFFFFFFF
            n = 0
            if v2:
                while n < len(weight):
                    if valid[n]:
                        best = weight[n]
                        pair[i] = n
                        n += 1
                        break
                    n += 1
                n = max(n, i + 1)
            while n < len(weight):
                if valid[n] and weight[n] < best:
                    best = weight[n]
                    pair[i] = n
                n += 1
            if pair[i] == -1:
                continue
            valid[pair[i]] = False
            combined += weight[pair[i]]
        weight.append(combined)
        valid.append(True)
        parent.append(True)
        left.append(pair[0])
        right.append(pair[1])
        if combined >= total:
            break
    return left, right, parent, len(weight) - 1


def _peek_table(tree) -> list[tuple[int, int]]:
    """table[next PEEK bits] = (symbol, code length), or (node, 0) when the code is longer."""
    left, right, parent, root = tree
    table: list[tuple[int, int]] = [(root, 0)] * (1 << PEEK)
    stack = [(root, 0, 0)]
    while stack:
        node, code, length = stack.pop()
        if node < 0:
            continue
        if not parent[node]:
            if length == 0:
                continue
            first = code << (PEEK - length)
            for k in range(first, first + (1 << (PEEK - length))):
                table[k] = (node, length)
        elif length == PEEK:
            table[code] = (node, 0)
        else:
            stack.append((left[node], code << 1, length + 1))
            stack.append((right[node], (code << 1) | 1, length + 1))
    return table


def decode_cbg(data: bytes) -> Image.Image:
    if not data.startswith(b"CompressedBG___"):
        raise BgiError("not a CompressedBG picture")
    width, height, bpp = struct.unpack_from("<HHI", data, 0x10)
    inter_len, key, enc_len = struct.unpack_from("<IIi", data, 0x20)
    check_sum, check_xor, version = struct.unpack_from("<BBH", data, 0x2C)
    enc = bytearray(data[0x30:0x30 + enc_len])
    total = 0
    parity = 0
    for i in range(len(enc)):
        v0 = (20021 * (key & 0xFFFF)) & 0xFFFFFFFF
        v1 = ((key >> 16) * 20021 + key * 346) & 0xFFFFFFFF
        v1 = (v1 + (v0 >> 16)) & 0xFFFF
        key = ((v1 << 16) + (v0 & 0xFFFF) + 1) & 0xFFFFFFFF
        byte = (enc[i] - v1) & 0xFF
        enc[i] = byte
        total = (total + byte) & 0xFF
        parity ^= byte
    if total != check_sum or parity != check_xor:
        raise BgiError("CompressedBG checksum mismatch")
    pos = 0x30 + enc_len
    if version < 2:
        return _cbg_v1(data, pos, bytes(enc), width, height, bpp, inter_len)
    if version == 2:
        return _cbg_v2(data, pos, bytes(enc), width, height, bpp)
    raise BgiError(f"CompressedBG version {version} is not supported")


def _cbg_v1(data: bytes, pos: int, enc: bytes, width: int, height: int, bpp: int, inter_len: int) -> Image.Image:
    weights, _ = _varints(enc + bytes(8), 0, 256)
    tree = _huffman(weights, False)
    left, right, parent, _root = tree
    table = _peek_table(tree)
    buf = data + bytes(16)
    packed = bytearray(inter_len)
    acc = 0
    nbits = 0
    for i in range(inter_len):
        if nbits < 32:
            acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(buf[pos:pos + 4], "big")
            pos += 4
            nbits += 32
        symbol, length = table[(acc >> (nbits - PEEK)) & PEEK_MASK]
        if length:
            nbits -= length
        else:
            nbits -= PEEK
            while parent[symbol]:
                if nbits == 0:
                    acc = int.from_bytes(buf[pos:pos + 4], "big")
                    pos += 4
                    nbits = 32
                nbits -= 1
                symbol = right[symbol] if (acc >> nbits) & 1 else left[symbol]
        packed[i] = symbol

    pixel = bpp // 8
    stride = width * pixel
    out = bytearray(stride * height)
    dst = 0
    src = 0
    zeros = False
    while dst < len(out) and src < inter_len:
        count = 0
        shift = 0
        complete = False
        while src < inter_len:
            byte = packed[src]
            src += 1
            count |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                complete = True
                break
        if not complete or dst + count > len(out):
            break
        if not zeros:                            # runs alternate: literal bytes, then zeros
            if src + count > inter_len:
                break
            out[dst:dst + count] = packed[src:src + count]
            src += count
        zeros = not zeros
        dst += count

    # every byte was stored as the difference from the average of its left and upper neighbours
    for x in range(pixel, stride):
        out[x] = (out[x] + out[x - pixel]) & 0xFF
    for y in range(1, height):
        line = y * stride
        above = line - stride
        for p in range(pixel):
            out[line + p] = (out[line + p] + out[above + p]) & 0xFF
        for x in range(line + pixel, line + stride):
            out[x] = (out[x] + ((out[x - pixel] + out[x - stride]) >> 1)) & 0xFF

    if bpp == 32:
        return Image.frombytes("RGBA", (width, height), bytes(out), "raw", "BGRA")
    if bpp == 24:
        return Image.frombytes("RGB", (width, height), bytes(out), "raw", "BGR")
    if bpp == 8:
        return Image.frombytes("L", (width, height), bytes(out))
    if bpp == 16:
        return Image.frombytes("RGB", (width, height), bytes(out), "raw", "BGR;16")
    raise BgiError(f"CompressedBG with {bpp} bits per pixel is not supported")


def _jpeg_header(width: int, height: int, components: int, quant: bytes) -> bytes:
    out = bytearray(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    tables = 1 if components == 1 else 2
    out += b"\xFF\xDB" + struct.pack(">H", 2 + 65 * tables)
    for t in range(tables):
        out.append(t)
        out += bytes(max(1, quant[t * 64 + ZIGZAG[k]]) for k in range(64))
    out += b"\xFF\xC0" + struct.pack(">HBHHB", 8 + 3 * components, 8, height, width, components)
    for c in range(components):
        out += bytes([c + 1, 0x11, 0 if c == 0 else 1])
    dc_bits = bytes([0, 0, 0, 0, 16] + [0] * 11)
    ac_bits = bytes([0] * 7 + [len(AC_VALUES)] + [0] * 8)
    out += b"\xFF\xC4" + struct.pack(">H", 2 + 17 + 16 + 17 + len(AC_VALUES))
    out += b"\x00" + dc_bits + bytes(range(16))
    out += b"\x10" + ac_bits + AC_VALUES
    out += b"\xFF\xDA" + struct.pack(">HB", 6 + 2 * components, components)
    for c in range(components):
        out += bytes([c + 1, 0x00])
    out += b"\x00\x3F\x00"
    return bytes(out)


def _cbg_v2(data: bytes, pos: int, enc: bytes, width: int, height: int, bpp: int) -> Image.Image:
    if len(enc) < 0x80:
        raise BgiError("CompressedBG version 2 without quantiser tables")
    if bpp not in (8, 24, 32):
        raise BgiError(f"CompressedBG version 2 with {bpp} bits per pixel is not supported")
    base = pos
    buf = data + bytes(16)
    weights_dc, pos = _varints(buf, pos, 0x10)
    weights_ac, pos = _varints(buf, pos, 0xB0)
    tree_dc = _huffman(weights_dc, True)
    tree_ac = _huffman(weights_ac, True)
    table_dc = _peek_table(tree_dc)
    table_ac = _peek_table(tree_ac)
    dl, dr, dp, _ = tree_dc
    al, ar, ap, _ = tree_ac

    padded_w = (width + 7) & ~7
    padded_h = (height + 7) & ~7
    bands = padded_h // 8
    offsets = struct.unpack_from(f"<{bands + 1}i", buf, pos)
    pad_skip = ((padded_w >> 3) + 7) >> 3
    components = 1 if bpp == 8 else 3
    per_row = padded_w // 8
    ac_code = AC_CODE

    chunks = [_jpeg_header(width, height, components, enc)]
    entropy = []
    out_acc = 0
    out_n = 0
    pred = [0] * components

    for band in range(bands):
        p = base + offsets[band] + pad_skip
        block_size = 0
        shift = 0
        while True:
            byte = buf[p]
            p += 1
            block_size |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        blocks = block_size // 64
        acc = 0
        nbits = 0

        # pass 1: the DC value of every block, each stored as the difference from the previous one
        dc = [0] * blocks
        running = 0
        for b in range(blocks):
            if nbits < 32:
                acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(buf[p:p + 4], "big")
                p += 4
                nbits += 32
            size, length = table_dc[(acc >> (nbits - PEEK)) & PEEK_MASK]
            if length:
                nbits -= length
            else:
                nbits -= PEEK
                while dp[size]:
                    nbits -= 1
                    size = dr[size] if (acc >> nbits) & 1 else dl[size]
            if size:
                if nbits < size:
                    acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(buf[p:p + 4], "big")
                    p += 4
                    nbits += 32
                nbits -= size
                value = (acc >> nbits) & ((1 << size) - 1)
                if not value >> (size - 1):
                    value -= (1 << size) - 1
                running += value
            dc[b] = running
        nbits -= nbits & 7                       # the AC data starts on a byte boundary

        # pass 2: the AC coefficients of every block, re-coded straight into JPEG symbols
        ac = [(AC_EOB, 8)] * blocks
        for b in range(blocks):
            index = 1
            last = 0
            bits = 0
            count = 0
            while index < 64:
                if nbits < 32:
                    acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(buf[p:p + 4], "big")
                    p += 4
                    nbits += 32
                code, length = table_ac[(acc >> (nbits - PEEK)) & PEEK_MASK]
                if length:
                    nbits -= length
                else:
                    nbits -= PEEK
                    while ap[code]:
                        nbits -= 1
                        code = ar[code] if (acc >> nbits) & 1 else al[code]
                if code == 0:
                    break
                if code == 0xF:
                    index += 16
                    continue
                index += code & 0xF
                if index >= 64:
                    break
                size = code >> 4
                if size:
                    if nbits < size:
                        acc = ((acc & ((1 << nbits) - 1)) << 32) | int.from_bytes(buf[p:p + 4], "big")
                        p += 4
                        nbits += 32
                    nbits -= size
                    value = (acc >> nbits) & ((1 << size) - 1)
                    run = index - last - 1
                    while run > 15:
                        bits = (bits << 8) | AC_ZRL
                        count += 8
                        run -= 16
                    bits = (((bits << 8) | ac_code[(run << 4) | size]) << size) | value
                    count += 8 + size
                    last = index
                index += 1
            if last != 63:
                bits = (bits << 8) | AC_EOB
                count += 8
            ac[b] = (bits, count)

        # write the band in JPEG order: block by block, the components interleaved
        for i in range(per_row):
            for c in range(components):
                b = c * per_row + i
                if b < blocks:
                    value = dc[b]
                    bits, count = ac[b]
                else:
                    value = pred[c]
                    bits, count = AC_EOB, 8
                diff = value - pred[c]
                pred[c] = value
                if diff == 0:
                    out_acc <<= 5
                    out_n += 5
                else:
                    size = (diff if diff > 0 else -diff).bit_length()
                    raw = diff if diff > 0 else diff + (1 << size) - 1
                    out_acc = (out_acc << (5 + size)) | (size << size) | raw
                    out_n += 5 + size
                out_acc = (out_acc << count) | bits
                out_n += count
            if out_n > 4096:
                whole = out_n >> 3
                rest = out_n & 7
                entropy.append((out_acc >> rest).to_bytes(whole, "big"))
                out_acc &= (1 << rest) - 1
                out_n = rest

    if out_n & 7:
        fill = 8 - (out_n & 7)
        out_acc = (out_acc << fill) | ((1 << fill) - 1)
        out_n += fill
    if out_n:
        entropy.append(out_acc.to_bytes(out_n >> 3, "big"))
    chunks.append(b"".join(entropy).replace(b"\xFF", b"\xFF\x00"))
    chunks.append(b"\xFF\xD9")

    image = Image.open(io.BytesIO(b"".join(chunks)))
    image.load()
    if bpp == 32:
        alpha = _cbg_alpha(buf, base + offsets[bands], padded_w, padded_h, len(data))
        if alpha is not None:
            mask = Image.frombytes("L", (padded_w, padded_h), alpha).crop((0, 0, width, height))
            image = image.convert("RGB")
            image.putalpha(mask)
    return image


def _cbg_alpha(buf: bytes, pos: int, width: int, height: int, end: int) -> bytes | None:
    """Transparency of a 32-bit picture: literal bytes and copies of earlier runs."""
    if pos + 4 > end or struct.unpack_from("<i", buf, pos)[0] != 1:
        return None
    pos += 4
    total = width * height
    out = bytearray(total)
    dst = 0
    ctl = 2
    while dst < total:
        ctl >>= 1
        if ctl == 1:
            if pos >= end:
                break
            ctl = buf[pos] | 0x100
            pos += 1
        if ctl & 1:
            v = buf[pos] | (buf[pos + 1] << 8)
            pos += 2
            x = v & 0x3F
            if x > 0x1F:
                x -= 0x40
            y = (v >> 6) & 7
            if y:
                y -= 8
            count = min(((v >> 9) & 0x7F) + 3, total - dst)
            src = dst + x + y * width
            if src < 0 or src >= dst:
                return None
            if src + count <= dst:
                out[dst:dst + count] = out[src:src + count]
            else:
                for k in range(count):
                    out[dst + k] = out[src + k]
            dst += count
        else:
            out[dst] = buf[pos]
            pos += 1
            dst += 1
    return bytes(out)


# --------------------------------------------------------------------------- #
# pictures and sound in general
# --------------------------------------------------------------------------- #

def decode_image(data: bytes) -> Image.Image:
    """Any picture the engine can load: CompressedBG, DSC-wrapped raw pixels, BMP, PNG."""
    data = dsc_decompress(data)
    kind = sniff(data[:32])
    if kind == "cbg":
        return decode_cbg(data)
    if kind == "raw":
        width, height, bpp = struct.unpack_from("<HHI", data)
        pixels = data[16:16 + width * height * bpp // 8]
        if bpp == 32:
            return Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA")
        if bpp == 24:
            return Image.frombytes("RGB", (width, height), pixels, "raw", "BGR")
        return Image.frombytes("L", (width, height), pixels)
    if kind in ("bmp", "png"):
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    raise BgiError(f"not a picture ({kind})")


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height without decoding (None if the header does not say)."""
    if data.startswith(b"CompressedBG___"):
        return struct.unpack_from("<HH", data, 0x10)
    if data.startswith(b"BM") and len(data) >= 26:
        w, h = struct.unpack_from("<ii", data, 18)
        return w, abs(h)
    if sniff(data[:32]) == "raw":
        return struct.unpack_from("<HH", data)
    return None


def audio_payload(data: bytes) -> bytes:
    """Strip the engine's header; what is left is an Ogg Vorbis file."""
    if data[4:8] == b"bw  ":
        (header,) = struct.unpack_from("<I", data)
        return data[header:]
    return data


def audio_info(data: bytes) -> dict:
    """Fields of the "bw  " header: sample count, rate, channels and the loop marker."""
    if data[4:8] != b"bw  ":
        return {}
    size, samples, rate, channels = struct.unpack_from("<IIII", data, 8)
    words = struct.unpack_from("<8I", data, 0x18)
    return {"size": size, "samples": samples, "rate": rate, "channels": channels, "extra": words}


# GARbro licence notice (the DSC and CompressedBG layouts above were learned from its source):
#
# Copyright (C) 2015-2018 by morkt
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
# to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
