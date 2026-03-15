#!/usr/bin/env python3
"""
avi2lossless_multi_safe.py — Robust Z‑Vision AVI → MKV/PNG/AVI converter
- Fixes potential infinite loops when scanning 'movi' (clamps sizes, guards zero-size chunks).
- Detects DUCK/TM20 via video 'strf' biCompression and auto-FFV1 unless --copy.
- Decodes Zork ADPCM (fmt tag 17) per-chunk and cleans temp WAV after mux.
"""

import argparse
import io
import struct
import subprocess
from dataclasses import dataclass
from glob import glob
from pathlib import Path
from typing import List, Tuple

def resolve_ffmpeg() -> str:
    from shutil import which
    exe = "ffmpeg"
    if which(exe) is not None:
        return exe
    local = Path(__file__).with_name("ffmpeg.exe")
    if local.exists():
        return str(local)
    return exe

_STEP_ADJ = (-1, -1, -1, 1, 4, 7, 10, 12)
_AMP_LUT = (
    0x0007,0x0008,0x0009,0x000A,0x000B,0x000C,0x000D,0x000E,
    0x0010,0x0011,0x0013,0x0015,0x0017,0x0019,0x001C,0x001F,
    0x0022,0x0025,0x0029,0x002D,0x0032,0x0037,0x003C,0x0042,
    0x0049,0x0050,0x0058,0x0061,0x006B,0x0076,0x0082,0x008F,
    0x009D,0x00AD,0x00BE,0x00D1,0x00E6,0x00FD,0x0117,0x0133,
    0x0151,0x0173,0x0198,0x01C1,0x01EE,0x0220,0x0256,0x0292,
    0x02D4,0x031C,0x036C,0x03C3,0x0424,0x048E,0x0502,0x0583,
    0x0610,0x06AB,0x0756,0x0812,0x08E0,0x09C3,0x0ABD,0x0BD0,
    0x0CFF,0x0E4C,0x0FBA,0x114C,0x1307,0x14EE,0x1706,0x1954,
    0x1BDC,0x1EA5,0x21B6,0x2515,0x28CA,0x2CDF,0x315B,0x364B,
    0x3BB9,0x41B2,0x4844,0x4F7E,0x5771,0x602F,0x69CE,0x7462,0x7FFF
)

@dataclass
class _ADPCMState:
    sample: int = 0
    index: int = 0

class ZorkADPCM:
    def __init__(self, stereo: bool):
        self.stereo = stereo
        self.state = [_ADPCMState(), _ADPCMState()]
        self.channel = 0
    def init(self):
        self.state = [_ADPCMState(), _ADPCMState()]
        self.channel = 0
    def decode_chunk(self, data: bytes) -> bytes:
        out = io.BytesIO()
        for b in data:
            st = self.state[self.channel]
            idx = 0 if st.index < 0 else 88 if st.index > 88 else st.index
            amp = _AMP_LUT[idx]
            diff = 0
            if b & 0x40: diff += amp
            if b & 0x20: diff += amp >> 1
            if b & 0x10: diff += amp >> 2
            if b & 0x08: diff += amp >> 3
            if b & 0x04: diff += amp >> 4
            if b & 0x02: diff += amp >> 5
            if b & 0x01: diff += amp >> 6
            if b & 0x80: diff = -diff
            s = st.sample + diff
            if s > 32767: s = 32767
            if s < -32768: s = -32768
            out.write(struct.pack('<h', s))
            step = (b >> 4) & 7
            idx += _STEP_ADJ[step]
            if idx < 0: idx = 0
            if idx > 88: idx = 88
            st.sample = s
            st.index = idx
            if self.stereo:
                self.channel ^= 1
        return out.getvalue()

FOURCC = lambda s: s.encode("ascii")

@dataclass
class AVIStreamInfo:
    stream_id: int
    fcc_type: bytes
    handler: bytes
    wf_tag: int = 0
    wf_channels: int = 0
    wf_rate: int = 0
    wf_blockalign: int = 0
    wf_bits: int = 0

@dataclass
class AVIParsed:
    width: int
    height: int
    fps_num: int
    fps_den: int
    streams: List[AVIStreamInfo]
    movi_off: int
    movi_size: int

def _read_u32le(b, off): return int.from_bytes(b[off:off+4], "little")
def _read_u16le(b, off): return int.from_bytes(b[off:off+2], "little")

def parse_avi_header(data: bytes, verbose: bool=False) -> AVIParsed:
    if data[0:4] != FOURCC("RIFF") or data[8:12] != FOURCC("AVI "):
        raise ValueError("Not an AVI RIFF")
    off = 12
    width = height = 0
    fps_num, fps_den = 0, 1
    streams: List[AVIStreamInfo] = []
    movi_off = movi_size = 0
    align2 = lambda x: (x + 1) & ~1
    file_len = len(data)

    while off + 8 <= file_len:
        ckid = data[off:off+4]; cksz = _read_u32le(data, off+4)
        cdata_off = off + 8; cdata_end = cdata_off + cksz
        if cdata_end > file_len:
            cdata_end = file_len
        if ckid == FOURCC("LIST"):
            if cdata_off + 4 > file_len: break
            list_type = data[cdata_off:cdata_off+4]
            if list_type == FOURCC("hdrl"):
                p = cdata_off + 4
                while p + 8 <= cdata_end:
                    sid = data[p:p+4]; ssz = _read_u32le(data, p+4); sd = p+8; se = sd+ssz
                    if se > cdata_end: se = cdata_end
                    if sid == FOURCC("avih"):
                        if sd + 40 <= se:
                            microsec_per_frame = _read_u32le(data, sd)
                            width = _read_u32le(data, sd+32)
                            height = _read_u32le(data, sd+36)
                            if microsec_per_frame:
                                fps_num, fps_den = 1000000, microsec_per_frame
                    elif sid == FOURCC("LIST") and sd + 4 <= se and data[sd:sd+4] == FOURCC("strl"):
                        sp = sd + 4
                        info = AVIStreamInfo(len(streams), b"", b"")
                        while sp + 8 <= se:
                            cid = data[sp:sp+4]; csz = _read_u32le(data, sp+4); cd = sp+8; ce = cd+csz
                            if ce > se: ce = se
                            if cid == FOURCC("strh"):
                                if cd + 48 <= ce:
                                    info.fcc_type = data[cd:cd+4]
                                    info.handler = data[cd+4:cd+8]
                                    rate = _read_u32le(data, cd+20)
                                    scale = _read_u32le(data, cd+24)
                                    if info.fcc_type == FOURCC("vids") and rate and scale:
                                        fps_num, fps_den = rate, scale
                            elif cid == FOURCC("strf"):
                                if info.fcc_type == FOURCC("auds"):
                                    if cd + 16 <= ce:
                                        info.wf_tag = _read_u16le(data, cd)
                                        info.wf_channels = _read_u16le(data, cd+2)
                                        info.wf_rate = _read_u32le(data, cd+4)
                                        info.wf_blockalign = _read_u16le(data, cd+12)
                                        info.wf_bits = _read_u16le(data, cd+14)
                                elif info.fcc_type == FOURCC("vids"):
                                    # BITMAPINFOHEADER biCompression at offset 16
                                    if cd + 20 <= ce:
                                        bi_size = _read_u32le(data, cd)
                                        if bi_size >= 40 and cd + 20 <= ce:
                                            comp = data[cd+16:cd+20]
                                            if comp and comp != b"\x00\x00\x00\x00":
                                                info.handler = comp
                            sp = align2(ce)
                        streams.append(info)
                    p = align2(se)
            elif list_type == FOURCC("movi"):
                movi_off = cdata_off + 4
                movi_size = cksz - 4 if cksz >= 4 else (file_len - movi_off)
        off = align2(cdata_end)

    if width == 0 or height == 0 or movi_off == 0:
        raise ValueError("Malformed AVI")
    if fps_num == 0:
        fps_num, fps_den = 1000, 66
    # Clamp movi_end to file
    if movi_off + movi_size > file_len:
        movi_size = file_len - movi_off
    return AVIParsed(width, height, fps_num, fps_den, streams, movi_off, movi_size)

def iter_movi_chunks(data: bytes, movi_off: int, movi_end: int, verbose: bool=False):
    """Yield (chunk_id, payload) inside 'movi' LIST. Robust against zero-size and overruns."""
    align2 = lambda x: (x + 1) & ~1
    off = movi_off
    file_len = len(data)
    limit = 50_000_000  # hard safety cap on iterations
    count = 0
    while off + 8 <= movi_end and off + 8 <= file_len:
        count += 1
        if count > limit:
            raise RuntimeError("Aborting: excessive chunk count (possible corruption)")
        ckid = data[off:off+4]
        cksz = _read_u32le(data, off+4)
        cd = off + 8
        ce = cd + cksz
        if ce > movi_end or ce > file_len:
            # clamp to remaining file to avoid hanging
            ce = min(movi_end, file_len)
        if ckid == FOURCC("LIST") and cd + 4 <= ce and data[cd:cd+4] == FOURCC("rec "):
            sub = cd + 4
            while sub + 8 <= ce:
                sid = data[sub:sub+4]
                ssz = _read_u32le(data, sub+4)
                sd = sub + 8
                se = sd + ssz
                if se > ce:
                    se = ce
                yield sid, data[sd:se]
                # progress guard
                if ssz == 0 and sd == sub + 8:
                    sub += 8
                else:
                    sub = align2(se)
        else:
            yield ckid, data[cd:ce]
        # progress guard when cksz==0
        new_off = align2(ce)
        if new_off <= off:
            new_off = off + 8
        off = new_off

def write_wav_pcm16(path: Path, samples: bytes, rate: int, channels: int):
    data_size = len(samples)
    byte_rate = rate * channels * 2
    block_align = channels * 2
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36+data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))
        f.write(struct.pack("<H", channels))
        f.write(struct.pack("<I", rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", 16))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(samples)

def main():
    ap = argparse.ArgumentParser(description="Convert Z‑Vision AVI → MKV/PNG/AVI with robust demux and Zork audio decode.")
    ap.add_argument("pattern", help=r"Glob path, e.g. 'AVI\*.avi'")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--outmode", choices=["mkv","png","avi"], default="mkv", help="Choose MKV (default), PNG sequence, or AVI")
    ap.add_argument("--ffv1", action="store_true", help="Force re-encode video losslessly to FFV1")
    ap.add_argument("--copy", action="store_true", help="Force stream-copy video (not recommended for DUCK/TM20)")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg()

    for in_path in map(Path, glob(args.pattern)):
        print(f"[INFO] Processing {in_path.name}")
        data = in_path.read_bytes()
        try:
            meta = parse_avi_header(data, verbose=args.verbose)
        except Exception as e:
            print(f"[ERROR] {in_path.name}: {e}")
            continue

        aud = next((s for s in meta.streams if s.fcc_type == FOURCC("auds")), None)
        wav_path = None
        if args.outmode in ("mkv","avi") and aud and aud.wf_tag == 17:
            dec = ZorkADPCM(stereo=(aud.wf_channels==2)); dec.init()
            stream_tag = f"{aud.stream_id:02d}wb".encode("ascii")
            movi_end = meta.movi_off + meta.movi_size
            pcm = bytearray()
            got_chunks = 0
            for ckid, payload in iter_movi_chunks(data, meta.movi_off, movi_end, verbose=args.verbose):
                if ckid == stream_tag:
                    pcm.extend(dec.decode_chunk(payload)); got_chunks += 1
            if got_chunks == 0:
                print(f"[WARN] {in_path.name}: no '{stream_tag.decode()}' audio chunks found; skipping audio decode.")
            else:
                wav_path = out_dir / f"{in_path.stem}_audio.wav"
                write_wav_pcm16(wav_path, bytes(pcm), aud.wf_rate, aud.wf_channels)
                print(f"[OK]   Wrote decoded WAV: {wav_path} ({got_chunks} chunks)")

        vid = next((s for s in meta.streams if s.fcc_type == FOURCC("vids")), None)
        # Ensure handler is set via video 'strf' too
        force_ffv1 = vid and (vid.handler in (FOURCC("DUCK"), FOURCC("TM20")))
        reencode = args.ffv1 or (force_ffv1 and not args.copy)

        if args.outmode == "mkv":
            mkv_path = out_dir / f"{in_path.stem}.mkv"
            cmd = [ffmpeg, "-y", "-i", str(in_path)]
            if wav_path:
                cmd += ["-i", str(wav_path), "-map", "0:v:0", "-map", "1:a:0"]
            if reencode:
                cmd += ["-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "rgb24", "-c:a", "pcm_s16le"]
            else:
                cmd += ["-c", "copy"]
            cmd += ["-shortest", str(mkv_path)]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(f"[OK]   Wrote MKV: {mkv_path}")
                if wav_path and Path(wav_path).exists(): Path(wav_path).unlink()
            except Exception as e:
                print(f"[ERROR] ffmpeg MKV mux failed: {e}")
            continue

        if args.outmode == "png":
            png_dir = out_dir / in_path.stem; png_dir.mkdir(parents=True, exist_ok=True)
            pattern = str(png_dir / f"{in_path.stem}_%05d.png")
            cmd = [ffmpeg, "-y", "-i", str(in_path), "-vsync", "0", "-pix_fmt", "rgb24", pattern]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(f"[OK]   Extracted PNGs -> {png_dir}")
            except Exception as e:
                print(f"[ERROR] ffmpeg PNG extraction failed: {e}")
            continue

        if args.outmode == "avi":
            avi_path = out_dir / f"{in_path.stem}.avi"
            cmd = [ffmpeg, "-y", "-i", str(in_path)]
            if wav_path:
                cmd += ["-i", str(wav_path), "-map", "0:v:0", "-map", "1:a:0"]
            if reencode:
                cmd += ["-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "rgb24", "-c:a", "pcm_s16le"]
            else:
                cmd += ["-c", "copy"]
            cmd += ["-shortest", str(avi_path)]
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                print(f"[OK]   Wrote AVI: {avi_path}")
                if wav_path and Path(wav_path).exists(): Path(wav_path).unlink()
            except Exception as e:
                print(f"[ERROR] ffmpeg AVI mux failed: {e}")

if __name__ == "__main__":
    main()
