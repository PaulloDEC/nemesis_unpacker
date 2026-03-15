#!/usr/bin/env python3
"""
zfs_unpack_scummvm.py — Extract files from Z‑Vision .ZFS archives (ScummVM‑aligned behavior)

Differences vs earlier build:
- Reads exactly `files_per_block` entries per directory block.
- Traverses blocks via 4-byte LE next-block pointer; stops after the block with next==0.
- Skips zero-sized entries (padding in last block).
- Applies 4-byte repeating XOR iff ANY key byte is non-zero (sum(key) != 0).
- Keeps bounds checks and path safety.
"""

import argparse
import fnmatch
import io
import os
from dataclasses import dataclass
from glob import glob
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

def _u32le(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o+4], 'little')

@dataclass
class ZfsHeader:
    magic: int
    unknown1: int
    max_name_len: int
    files_per_block: int
    file_count: int
    xor_key: bytes  # 4 bytes
    data_offset: int

@dataclass
class ZfsEntry:
    name: str
    offset: int
    file_id: int
    size: int
    timestamp: int
    unknown: int

def parse_header(f) -> ZfsHeader:
    f.seek(0)
    hdr = f.read(28)
    if len(hdr) != 28:
        raise ValueError("File too small for ZFS header")
    magic        = _u32le(hdr, 0)
    unknown1     = _u32le(hdr, 4)
    max_name_len = _u32le(hdr, 8)
    files_per    = _u32le(hdr, 12)
    file_count   = _u32le(hdr, 16)
    xor_key      = hdr[20:24]
    data_off     = _u32le(hdr, 24)
    if files_per <= 0:
        raise ValueError(f"Invalid files_per_block={files_per}")
    return ZfsHeader(magic, unknown1, max_name_len, files_per, file_count, xor_key, data_off)

def iter_dir_entries(f, hdr: ZfsHeader) -> Iterator[ZfsEntry]:
    """
    Directory is a linked list of blocks starting at offset 28.
    Each block:
      u32le next_block_offset (absolute; 0 => this is the last block)
      then exactly hdr.files_per_block * 36 bytes of entries
    Last block may have zero-sized padding entries -> skip.
    """
    visited = set()
    cur = 28  # first block right after header
    total = 0
    while True:
        if cur in visited:
            raise ValueError(f"Loop detected in directory blocks at 0x{cur:X}")
        visited.add(cur)
        f.seek(cur)
        nb = f.read(4)
        if len(nb) < 4:
            raise ValueError(f"Unexpected EOF reading next_block_offset at 0x{cur:X}")
        next_block_off = _u32le(nb, 0)

        # Read exactly files_per_block entries
        for i in range(hdr.files_per_block):
            raw = f.read(36)
            if len(raw) < 36:
                raise ValueError(f"Unexpected EOF in directory entry at 0x{f.tell()-len(raw):X}")
            name_raw = raw[0:16]
            name = name_raw.split(b'\x00', 1)[0].decode('ascii', errors='ignore')
            off  = _u32le(raw, 16)
            fid  = _u32le(raw, 20)
            size = _u32le(raw, 24)
            ts   = _u32le(raw, 28)
            unk  = _u32le(raw, 32)

            # sanitize name: drop control chars including NUL, and Windows-illegal characters
            name = ''.join(ch for ch in name if 32 <= ord(ch) < 127 and ch not in '<>:\\\"/|?*')
            if not name:
                name = f'file_{fid:06d}'
            if size != 0:
                total += 1
                yield ZfsEntry(name, off, fid, size, ts, unk)

        if next_block_off == 0:
            break
        cur = next_block_off

def xor_decrypt(data: bytes, key: bytes) -> bytes:
    # ScummVM semantics: apply XOR iff ANY byte is non-zero
    if not key or sum(key) == 0:
        return data
    k0, k1, k2, k3 = key
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ (k0, k1, k2, k3)[i & 3]
    return bytes(out)

def _safe_join(out_dir: Path, name: str) -> Path:
    # flat names in archive; sanitize anyway
    name = name.replace('\\\\', '/').split('/')[-1]
    p = (out_dir / name).resolve()
    # constrain to out_dir
    if not str(p).startswith(str(out_dir.resolve())):
        p = out_dir / Path(name).name
        p = p.resolve()
    return p

def extract_one(f, hdr: ZfsHeader, ent: ZfsEntry, out_dir: Path) -> int:
    if ent.offset < hdr.data_offset or ent.size < 0:
        raise ValueError(f"Bad entry {ent.name}: off=0x{ent.offset:X}, size={ent.size}")
    f.seek(0, os.SEEK_END)
    file_len = f.tell()
    if ent.offset + ent.size > file_len:
        raise ValueError(f"Entry {ent.name} exceeds file length")
    f.seek(ent.offset)
    raw = f.read(ent.size)
    data = xor_decrypt(raw, hdr.xor_key)
    out_path = _safe_join(out_dir, ent.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'wb') as of:
        of.write(data)
    return len(data)

def main():
    import argparse
    import fnmatch
    ap = argparse.ArgumentParser(description="Extract Z‑Vision .ZFS archives (ScummVM‑aligned)")
    ap.add_argument("pattern", help=r"Glob path, e.g. 'DATA\\*.zfs'")
    ap.add_argument("--out", help="Output directory (required for extraction)")
    ap.add_argument("--list", action="store_true", help="List contents only (no extraction)")
    ap.add_argument("--name", help="Case-insensitive name glob to include (e.g. '*VID*.RLF')")
    ap.add_argument("--id", action="append", help="IDs to include, comma-separated or repeated")
    args = ap.parse_args()

    paths = [Path(p) for p in glob(args.pattern)]
    if not paths:
        print("[WARN] No files matched pattern")
        return

    # Parse id filters
    include_ids = set()
    if args.id:
        for grp in args.id:
            for tok in grp.split(','):
                tok = tok.strip()
                if tok:
                    try:
                        include_ids.add(int(tok, 0))
                    except ValueError:
                        print(f"[WARN] Ignoring non-integer id: {tok}")

    listing_only = args.list or not args.out
    out_base = Path(args.out) if args.out else None
    if not listing_only:
        out_base.mkdir(parents=True, exist_ok=True)

    for zfs_path in paths:
        print(f"[INFO] Reading {zfs_path.name}")
        with open(zfs_path, 'rb') as f:
            hdr = parse_header(f)
            key_hex = hdr.xor_key.hex().upper()
            print(f"       files={hdr.file_count}, per_block={hdr.files_per_block}, max_name={hdr.max_name_len}, data_off=0x{hdr.data_offset:X}, key={key_hex}")
            # Build entries
            entries = list(iter_dir_entries(f, hdr))
            if hdr.file_count and len(entries) != hdr.file_count:
                print(f"[WARN] Directory count mismatch: header says {hdr.file_count}, found {len(entries)}")
            # Filters
            def name_ok(n: str) -> bool:
                return True if not args.name else fnmatch.fnmatch(n.lower(), args.name.lower())
            def id_ok(i: int) -> bool:
                return True if not include_ids else (i in include_ids)
            selected = [e for e in entries if name_ok(e.name) and id_ok(e.file_id)]
            print(f"[OK]   {len(selected)} / {len(entries)} entries selected")
            if listing_only:
                for e in selected:
                    print(f"  - {e.name:16s}  id={e.file_id:6d}  off=0x{e.offset:08X}  size={e.size:8d}")
                continue
            out_dir = (out_base / zfs_path.stem).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            total_bytes = 0; extracted = 0
            for e in selected:
                try:
                    n = extract_one(f, hdr, e, out_dir)
                    extracted += 1; total_bytes += n
                except Exception as ex:
                    print(f"[ERROR] {e.name}: {ex}")
            print(f"[DONE] Extracted {extracted} files ({total_bytes} bytes) to {out_dir}")

if __name__ == "__main__":
    main()
