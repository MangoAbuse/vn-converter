"""Reader for Nitroplus N2System .npa archives (the layout documented by nipa / libnpa).

Only the unencrypted variant is handled, which is what the JAST USA release of
Saya no Uta ships: file data is stored as-is (optionally zlib-compressed) and only
the file names are obfuscated with a key derived from the header.
"""
from __future__ import annotations

import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

HEADER_SIZE = 0x29
MAGIC = b"NPA\x01\x00\x00\x00"


@dataclass
class Entry:
    name: str          # forward-slash path inside the archive
    is_dir: bool
    offset: int        # absolute offset of the data in the archive file
    comp_size: int
    orig_size: int


class NpaArchive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: list[Entry] = []
        with open(self.path, "rb") as f:
            head = f.read(HEADER_SIZE)
            if head[:7] != MAGIC:
                raise ValueError(f"{self.path.name}: not an NPA archive")
            key1, key2 = struct.unpack_from("<II", head, 7)
            self.compressed = bool(head[15])
            self.encrypted = bool(head[16])
            total, _dirs, _files = struct.unpack_from("<III", head, 17)
            start = struct.unpack_from("<I", head, 37)[0]
            if self.encrypted:
                raise ValueError(f"{self.path.name}: encrypted NPA archives are not supported")

            table = f.read(start)
            seed = (key1 * key2) & 0xFFFFFFFF
            seed_sum = sum(seed.to_bytes(4, "little"))
            pos = 0
            for index in range(total):
                (name_len,) = struct.unpack_from("<I", table, pos)
                pos += 4
                raw = bytearray(table[pos:pos + name_len])
                pos += name_len
                base = seed_sum + sum(index.to_bytes(4, "little"))
                for i in range(name_len):
                    raw[i] = (raw[i] + 0xFC * i - base) & 0xFF
                kind, _file_id, offset, comp_size, orig_size = struct.unpack_from("<BIIII", table, pos)
                pos += 17
                self.entries.append(Entry(
                    name=raw.decode("cp932").replace("\\", "/"),
                    is_dir=(kind == 1),
                    offset=offset + start + HEADER_SIZE,
                    comp_size=comp_size,
                    orig_size=orig_size,
                ))

    def files(self) -> list[Entry]:
        return [e for e in self.entries if not e.is_dir]

    def read(self, entry: Entry) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            data = f.read(entry.comp_size)
        if self.compressed and entry.comp_size != entry.orig_size:
            data = zlib.decompress(data)
        return data

    def extract_all(self, out_dir: str | Path) -> int:
        out_dir = Path(out_dir)
        count = 0
        with open(self.path, "rb") as f:
            for entry in self.files():
                target = out_dir / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                f.seek(entry.offset)
                data = f.read(entry.comp_size)
                if self.compressed and entry.comp_size != entry.orig_size:
                    data = zlib.decompress(data)
                target.write_bytes(data)
                count += 1
        return count


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: npa.py <archive.npa> <output-folder>")
    archive = NpaArchive(sys.argv[1])
    n = archive.extract_all(sys.argv[2])
    print(f"{archive.path.name}: extracted {n} files")
