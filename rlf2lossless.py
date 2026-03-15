#!/usr/bin/env python3
"""
rlf2lossless_scummvm_exact.py — Exact RLF decoder (Z-Vision), modeled on ScummVM's rlf_decoder.

- Validates 'FELR' magic
- Header fields read as in ScummVM:
    LE u32 size1, LE u32 unk1, LE u32 unk2, LE u32 frameCount
    skip 136 bytes
    LE u32 width, LE u32 height
    'EMIT' (BE) tag, LE u32 size4, LE u32 unknown11, LE u32 frameTimeDiv10  => frameTime_ms = frameTimeDiv10 / 10
- Each frame block:
    'MARF' (BE), LE u32 size, LE u32 unk1, LE u32 unk2, BE u32 type ('ELHD' or 'ELRH'),
    LE u32 headerSize (usually 28), LE u32 unk3, then payload of (size - headerSize) bytes
- Pixel format: RGB555 (little-endian words), converted to RGB24 for PNG/MKV

Outputs:
  - PNG sequence
  - Optional MKV (FFV1) if ffmpeg is available on the system PATH
"""

import argparse
import glob
import struct
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    print("This script requires Pillow. Install with:  pip install pillow")
    sys.exit(1)

TAG_FELR = b'FELR'
TAG_MARF = b'MARF'
TAG_EMIT = b'EMIT'
TYPE_ELRH = b'ELRH'
TYPE_ELHD = b'ELHD'

def u32le(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off+4], 'little', signed=False)

def u32be(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off+4], 'big', signed=False)

def rgb555_to_rgb24(word_le: int) -> Tuple[int,int,int]:
    v = word_le & 0xFFFF
    r5 = (v >> 10) & 0x1F
    g5 = (v >> 5)  & 0x1F
    b5 = (v >> 0)  & 0x1F
    # Scale to 8-bit
    r = (r5 * 255 + 15) // 31
    g = (g5 * 255 + 15) // 31
    b = (b5 * 255 + 15) // 31
    return r, g, b

def decode_simple_rle(src: bytes, dest16le: bytearray, total_pixels: int) -> None:
    """ELRH — Simple RLE: negative N => copy N literal pixels; non-negative N => repeat next pixel N+2 times."""
    src_off = 0
    dst_off = 0  # in bytes
    dest_size = total_pixels * 2
    mv = memoryview(src)
    while src_off < len(src) and dst_off < dest_size:
        c = struct.unpack_from('b', mv, src_off)[0]
        src_off += 1
        if c < 0:
            run = -c
            need = run * 2
            if src_off + need > len(src):
                # Truncated literal run; stop
                break
            # bounds check
            end_dst = dst_off + need
            if end_dst > dest_size:
                end_dst = dest_size
                need = end_dst - dst_off
            dest16le[dst_off:end_dst] = mv[src_off:src_off+need]
            src_off += need
            dst_off = end_dst
        else:
            # repeat next pixel c+2 times
            if src_off + 2 > len(src):
                break
            px = mv[src_off:src_off+2]
            src_off += 2
            run = c + 2
            need = run * 2
            end_dst = dst_off + need
            if end_dst > dest_size:
                need = dest_size - dst_off
                end_dst = dest_size
            # fill
            for j in range(0, need, 2):
                dest16le[dst_off + j: dst_off + j + 2] = px
            dst_off = end_dst

def decode_masked_rle(src: bytes, dest16le: bytearray, total_pixels: int, prev16le: Optional[bytearray]) -> None:
    """ELHD — Masked RLE: negative N => copy N literal pixels; non-negative N => skip N+2 pixels (leave previous)."""
    dest_size = total_pixels * 2
    if prev16le is not None:
        dest16le[:] = prev16le
    else:
        for i in range(dest_size):
            dest16le[i] = 0

    src_off = 0
    dst_off = 0
    mv = memoryview(src)

    while src_off < len(src) and dst_off < dest_size:
        c = struct.unpack_from('b', mv, src_off)[0]
        src_off += 1
        if c < 0:
            run = -c
            need = run * 2
            if src_off + need > len(src):
                break
            end_dst = dst_off + need
            if end_dst > dest_size:
                need = dest_size - dst_off
                end_dst = dest_size
            dest16le[dst_off:end_dst] = mv[src_off:src_off+need]
            src_off += need
            dst_off = end_dst
        else:
            skip = (c * 2) + 2
            dst_off += skip
            if dst_off > dest_size:
                dst_off = dest_size
                break

def write_png_rgb24(path: Path, fb16: bytearray, w: int, h: int) -> None:
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        lo = fb16[2*i]
        hi = fb16[2*i + 1]
        word = lo | (hi << 8)
        r, g, b = rgb555_to_rgb24(word)
        j = 3 * i
        rgb[j]   = r
        rgb[j+1] = g
        rgb[j+2] = b
    img = Image.frombytes("RGB", (w, h), bytes(rgb))
    img.save(path)

def parse_header_exact(data: bytes) -> Tuple[int,int,int,int,int,int]:
    """
    Returns (offset_after_header, frame_count, width, height, frame_time_ms, first_marf_off)
    """
    if not data.startswith(TAG_FELR):
        raise ValueError("Missing FELR magic at file start")

    off = 4
    size1   = u32le(data, off); off += 4
    unk1    = u32le(data, off); off += 4
    unk2    = u32le(data, off); off += 4
    frame_count = u32le(data, off); off += 4

    # Skip 136 bytes
    off += 136

    # width, height
    width  = u32le(data, off); off += 4
    height = u32le(data, off); off += 4

    # EMIT
    if data[off:off+4] != TAG_EMIT:
        # be tolerant: search forward a bit
        search = data.find(TAG_EMIT, off, off+128)
        if search == -1:
            raise ValueError("EMIT tag not found after width/height")
        off = search
    off += 4
    size4 = u32le(data, off); off += 4
    unknown11 = u32le(data, off); off += 4
    frameTimeDiv10 = u32le(data, off); off += 4
    frame_time_ms = frameTimeDiv10 // 10  # ScummVM divides by 10; their _frameTime is milliseconds

    # First MARF
    first_marf = data.find(TAG_MARF, off)
    if first_marf == -1:
        raise ValueError("No MARF frames found")

    return off, frame_count, width, height, frame_time_ms, first_marf

def parse_frames_exact(data: bytes, start_off: int, frame_count: int) -> List[Tuple[bytes, bytes]]:
    """
    Parse exactly frame_count frames starting at/beyond start_off.
    Returns a list of (type4cc, payload) where type4cc is b'ELRH' or b'ELHD'.
    """
    frames: List[Tuple[bytes, bytes]] = []
    off = data.find(TAG_MARF, start_off)
    for i in range(frame_count):
        if off == -1 or off + 4 + 4*6 > len(data):
            raise ValueError(f"Unexpected end of file before frame {i}")
        off += 4  # past 'MARF'
        size = u32le(data, off); off += 4
        unk1 = u32le(data, off); off += 4
        unk2 = u32le(data, off); off += 4
        type_be = data[off:off+4]; off += 4
        headerSize = u32le(data, off); off += 4
        unk3 = u32le(data, off); off += 4

        # Payload starts at current off; encodedSize = size - headerSize
        encoded_size = size - headerSize
        if encoded_size < 0:
            raise ValueError(f"Negative encoded size on frame {i}")
        end = off + encoded_size
        if end > len(data):
            raise ValueError(f"Encoded data exceeds file on frame {i}")
        payload = data[off:end]
        frames.append((type_be, payload))

        # Next MARF
        off = data.find(TAG_MARF, end)
    return frames

def try_ffmpeg_ffv1_from_pngs(png_dir: Path, stem: str, frames_count: int, out_mkv: Path, fps: Optional[float]) -> bool:
    # Build ffmpeg command; try system PATH first, else fallback to local ffmpeg.exe
    ffmpeg = 'ffmpeg'
    from shutil import which
    if which(ffmpeg) is None:
        local_ffmpeg = Path(__file__).with_name('ffmpeg.exe')
        if local_ffmpeg.exists():
            ffmpeg = str(local_ffmpeg)
    pad = max(3, len(str(frames_count)))
    pattern = str((png_dir / f"{stem}_%0{pad}d.png").resolve())
    cmd = [
        ffmpeg,
        '-y',
        '-framerate', str(fps or 15),
        '-i', pattern,
        '-c:v', 'ffv1',
        '-level', '3',
        '-g', '1',
        '-pix_fmt', 'rgb24',
        '-map_metadata', '-1',
        str(out_mkv)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False

def main():
    ap = argparse.ArgumentParser(description="Exact Z-Vision RLF -> PNG/MKV (FFV1) converter (based on ScummVM layout)")
    ap.add_argument("pattern", help="Glob for input files, e.g. 'RLF\\*.rlf'")
    ap.add_argument("--out", help="Directory for PNG sequences")
    ap.add_argument("--mkv", help="Directory for MKV (FFV1) output (requires ffmpeg)")
    ap.add_argument("--fps", type=float, help="Override FPS; if omitted, use header-derived FPS when available")
    ap.add_argument("--dry-run", action="store_true", help="Parse only; do not write outputs")
    args = ap.parse_args()

    files = [Path(p) for p in glob.glob(args.pattern)]
    if not files:
        print("No input files matched.")
        sys.exit(1)

    out_png = Path(args.out) if args.out else None
    out_mkv = Path(args.mkv) if args.mkv else None

    if not args.dry_run and not (out_png or out_mkv):
        print("Nothing to write. Provide --out for PNG and/or --mkv for MKV.")
        sys.exit(1)

    for src in files:
        print(f"[INFO] Decoding {src.name}")
        data = src.read_bytes()
        try:
            off_after_hdr, frame_count, w, h, frame_time_ms, first_marf = parse_header_exact(data)
        except Exception as e:
            print(f"[ERROR] {src.name}: {e}")
            continue

        # FPS: ScummVM exposes frame rate as Rational(1000, _frameTimeMs)
        header_fps = None
        if frame_time_ms and frame_time_ms > 0:
            header_fps = 1000.0 / frame_time_ms

        try:
            frames_meta = parse_frames_exact(data, first_marf, frame_count)
        except Exception as e:
            print(f"[ERROR] {src.name}: {e}")
            continue

        print(f"[OK]   Parsed header: {frame_count} frames @ {w}x{h}, frame_time_ms={frame_time_ms} (~{header_fps or 0:.3f} fps)")

        # Decode to 16bpp buffers
        total_pixels = w * h
        prev_fb = None
        decoded = []
        for idx, (typ, payload) in enumerate(frames_meta):
            fb = bytearray(total_pixels * 2)
            if typ == TYPE_ELRH:
                # Keyframe — simple RLE from a blank buffer
                for i in range(len(fb)):
                    fb[i] = 0
                decode_simple_rle(payload, fb, total_pixels)
                prev_fb = fb
            elif typ == TYPE_ELHD:
                # Delta — masked RLE on top of prev
                if prev_fb is None:
                    # If the stream starts with a delta, treat prev as zeros
                    prev_fb = bytearray(total_pixels * 2)
                decode_masked_rle(payload, fb, total_pixels, prev_fb)
                prev_fb = fb
            else:
                print(f"[WARN] {src.name}: frame {idx} has unknown type {typ!r}, skipping.")
                continue
            decoded.append(fb)

        print(f"[OK]   Decoded {len(decoded)} frames")

        if args.dry_run:
            continue

        stem = src.stem

        # PNGs
        png_dir = None
        if out_png:
            png_dir = out_png / stem
            png_dir.mkdir(parents=True, exist_ok=True)
            pad = max(3, len(str(len(decoded))))
            for i, fb in enumerate(decoded, 1):
                fn = png_dir / f"{stem}_{i:0{pad}d}.png"
                write_png_rgb24(fn, fb, w, h)
            print(f"[OK]   Wrote PNG sequence to {png_dir}")

        # MKV (FFV1)
        if out_mkv and decoded:
            out_mkv.mkdir(parents=True, exist_ok=True)
            mkv_path = out_mkv / f"{stem}.mkv"
            # If we didn't create PNGs, write temporary PNGs for ffmpeg
            cleanup = False
            if png_dir is None:
                tmp = Path(".") / f"__rlf_png_{stem}"
                tmp.mkdir(parents=True, exist_ok=True)
                pad = max(3, len(str(len(decoded))))
                for i, fb in enumerate(decoded, 1):
                    fn = tmp / f"{stem}_{i:0{pad}d}.png"
                    write_png_rgb24(fn, fb, w, h)
                png_dir = tmp
                cleanup = True

            fps = args.fps or header_fps or 15.0
            ok = try_ffmpeg_ffv1_from_pngs(png_dir, stem, len(decoded), mkv_path, fps)
            if ok:
                print(f"[OK]   Wrote lossless MKV (FFV1) -> {mkv_path}")
                if cleanup:
                    # Clean temporary PNGs
                    for p in png_dir.glob("*.png"):
                        try: p.unlink()
                        except Exception: pass
                    try: png_dir.rmdir()
                    except Exception: pass
            else:
                print("[WARN] ffmpeg not available or failed; MKV not created. PNGs remain available.")

if __name__ == "__main__":
    main()
