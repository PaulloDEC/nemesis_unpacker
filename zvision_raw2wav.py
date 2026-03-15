#!/usr/bin/env python3
"""
Batch-convert Z-Vision .raw audio (Zork Nemesis / Zork: Grand Inquisitor) to .wav.

- Nemesis: identifier = 7th character of filename (index 6)
- Grand Inquisitor: identifier = 8th character of filename (index 7)

Tables and ADPCM algorithm derived from ScummVM Z-Vision sources (your zork_raw.cpp/.h).
"""

import argparse
import glob
from pathlib import Path
import sys
import wave

# -----------------------
# Identifier tables (from ScummVM)
# Fields there: {identifier, rate(hex), stereo, packed, bits16}
# Here we map them to codec/params we need.
# Rates in hex converted to int Hz: 0x1F40=8000, 0x2B11=11025, 0x5622=22050, 0xAC44=44100
# -----------------------

def _hz(x):
    return int(x)

# Rates: 0x1F40=8000, 0x2B11=11025, 0x5622=22050, 0xAC44=44100

NEMESIS_ID_TABLE = {
    # packed = False (PCM)
    '0': {'codec':'pcm','rate':8000,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    '1': {'codec':'pcm','rate':8000,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    '2': {'codec':'pcm','rate':8000,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    '3': {'codec':'pcm','rate':8000,'channels':2,'sample_width':2,'endianness':'le','signed':True},
    '4': {'codec':'pcm','rate':11025,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    '5': {'codec':'pcm','rate':11025,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    '6': {'codec':'pcm','rate':11025,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    '7': {'codec':'pcm','rate':11025,'channels':2,'sample_width':2,'endianness':'le','signed':True},
    '8': {'codec':'pcm','rate':22050,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    '9': {'codec':'pcm','rate':22050,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    'a': {'codec':'pcm','rate':22050,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    'b': {'codec':'pcm','rate':22050,'channels':2,'sample_width':2,'endianness':'le','signed':True},
    'c': {'codec':'pcm','rate':44100,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    'd': {'codec':'pcm','rate':44100,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    'e': {'codec':'pcm','rate':44100,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    'f': {'codec':'pcm','rate':44100,'channels':2,'sample_width':2,'endianness':'le','signed':True},

    # packed = True (ADPCM) – sample_width/bits16 doesn’t matter for input; decoder outputs 16-bit
    'g': {'codec':'adpcm','rate':8000,'channels':1},
    'h': {'codec':'adpcm','rate':8000,'channels':2},
    'j': {'codec':'adpcm','rate':8000,'channels':1},
    'k': {'codec':'adpcm','rate':8000,'channels':2},
    'l': {'codec':'adpcm','rate':11025,'channels':1},
    'm': {'codec':'adpcm','rate':11025,'channels':2},
    'n': {'codec':'adpcm','rate':11025,'channels':1},
    'p': {'codec':'adpcm','rate':11025,'channels':2},
    'q': {'codec':'adpcm','rate':22050,'channels':1},
    'r': {'codec':'adpcm','rate':22050,'channels':2},
    's': {'codec':'adpcm','rate':22050,'channels':1},
    't': {'codec':'adpcm','rate':22050,'channels':2},
    'u': {'codec':'adpcm','rate':44100,'channels':1},
    'v': {'codec':'adpcm','rate':44100,'channels':2},
    'w': {'codec':'adpcm','rate':44100,'channels':1},
    'x': {'codec':'adpcm','rate':44100,'channels':2},
}

GI_ID_TABLE = {
    # packed = False (PCM)
    '4': {'codec':'pcm','rate':11025,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    '5': {'codec':'pcm','rate':11025,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    '6': {'codec':'pcm','rate':11025,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    '7': {'codec':'pcm','rate':11025,'channels':2,'sample_width':2,'endianness':'le','signed':True},
    '8': {'codec':'pcm','rate':22050,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    '9': {'codec':'pcm','rate':22050,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    'a': {'codec':'pcm','rate':22050,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    'b': {'codec':'pcm','rate':22050,'channels':2,'sample_width':2,'endianness':'le','signed':True},
    'c': {'codec':'pcm','rate':44100,'channels':1,'sample_width':1,'endianness':'le','signed':False},
    'd': {'codec':'pcm','rate':44100,'channels':2,'sample_width':1,'endianness':'le','signed':False},
    'e': {'codec':'pcm','rate':44100,'channels':1,'sample_width':2,'endianness':'le','signed':True},
    'f': {'codec':'pcm','rate':44100,'channels':2,'sample_width':2,'endianness':'le','signed':True},

    # packed = True (ADPCM)
    'g': {'codec':'adpcm','rate':11025,'channels':1},
    'h': {'codec':'adpcm','rate':11025,'channels':2},
    'j': {'codec':'adpcm','rate':11025,'channels':1},
    'k': {'codec':'adpcm','rate':11025,'channels':2},
    'm': {'codec':'adpcm','rate':22050,'channels':1},
    'n': {'codec':'adpcm','rate':22050,'channels':2},
    'p': {'codec':'adpcm','rate':22050,'channels':1},
    'q': {'codec':'adpcm','rate':22050,'channels':2},
    'r': {'codec':'adpcm','rate':44100,'channels':1},
    's': {'codec':'adpcm','rate':44100,'channels':2},
    't': {'codec':'adpcm','rate':44100,'channels':1},
    'u': {'codec':'adpcm','rate':44100,'channels':2},
}


# -----------------------
# ADPCM constants (from ScummVM)
# -----------------------

STEP_ADJ = [-1, -1, -1, 1, 4, 7, 10, 12]

AMP_TABLE = [
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
]

def _clip16(x):
    if x > 32767: return 32767
    if x < -32768: return -32768
    return x

def decode_zvision_adpcm(raw_bytes: bytes, channels: int) -> bytes:
    """
    Reproduces ScummVM RawChunkStream::readBuffer for Z-Vision ADPCM.
    One encoded byte -> one output 16-bit sample. Channels alternate if stereo.
    """
    # Per-channel state
    last_sample = [0, 0]   # previous output sample
    index       = [0, 0]   # amplitude index into AMP_TABLE (0..88)
    ch_mask = 1 if channels == 2 else 0
    out = bytearray(len(raw_bytes) * 2)

    # position in samples (not bytes)
    sidx = 0
    channel = 0

    for b in raw_bytes:
        # amplitude lookup
        amp = AMP_TABLE[index[channel]]

        # build magnitude from bit-weighted fractions
        sample = 0
        if b & 0x40: sample += amp
        if b & 0x20: sample += amp >> 1
        if b & 0x10: sample += amp >> 2
        if b & 0x08: sample += amp >> 3
        if b & 0x04: sample += amp >> 4
        if b & 0x02: sample += amp >> 5
        if b & 0x01: sample += amp >> 6
        if b & 0x80: sample = -sample  # sign

        # accumulate and clip
        sample = _clip16(last_sample[channel] + sample)

        # store little-endian 16-bit
        o = sidx * 2
        out[o] = sample & 0xFF
        out[o+1] = (sample >> 8) & 0xFF
        sidx += 1

        # adjust index
        step_idx = (b >> 4) & 0x7
        index[channel] += STEP_ADJ[step_idx]
        if index[channel] < 0: index[channel] = 0
        if index[channel] > 88: index[channel] = 88

        # save state
        last_sample[channel] = sample

        # flip channel if stereo
        channel = (channel + 1) & ch_mask

    return bytes(out)

# -----------------------
# PCM helpers
# -----------------------

def pcm_fix(raw: bytes, sample_width: int, endianness: str, signed_flag: bool) -> bytes:
    """
    Convert raw PCM to WAV conventions:
      - 8-bit: unsigned
      - 16-bit: signed little-endian
    """
    if sample_width == 1:
        # WAV wants unsigned 8-bit
        if signed_flag:
            return bytes(((x + 128) & 0xFF) for x in raw)
        return raw

    if sample_width != 2:
        raise ValueError(f"Unsupported PCM width: {sample_width}")

    out = bytearray(len(raw))
    for i in range(0, len(raw), 2):
        if i + 1 >= len(raw): break
        b0, b1 = raw[i], raw[i+1]
        if endianness == 'le':
            v = b0 | (b1 << 8)
        elif endianness == 'be':
            v = b1 | (b0 << 8)
        else:
            raise ValueError("endianness must be 'le' or 'be'")

        # interpret as signed/unsigned
        if signed_flag:
            if v >= 0x8000: v -= 0x10000
        else:
            v -= 0x8000  # center unsigned 16 at 0

        # output as signed little-endian
        if v < 0: v += 0x10000
        out[i] = v & 0xFF
        out[i+1] = (v >> 8) & 0xFF
    return bytes(out)

# -----------------------
# Driver
# -----------------------

def infer_params_from_name(stem: str, game: str):
    if game == 'nemesis':
        idx = 6  # 7th char
        table = NEMESIS_ID_TABLE
    else:
        idx = 7  # 8th char
        table = GI_ID_TABLE

    if len(stem) <= idx:
        raise ValueError(f"Filename '{stem}' too short for identifier at position {idx+1}")
    ident = stem[idx].lower()
    if ident not in table:
        raise KeyError(f"Identifier '{ident}' not found for {game.upper()} (from '{stem}')")
    return ident, table[ident]

def convert_file(src: Path, dst_dir: Path, game: str, dry: bool = False):
    ident, prm = infer_params_from_name(src.stem, game)
    codec = prm['codec']
    rate = int(prm['rate'])
    channels = int(prm['channels'])
    dst = dst_dir / (src.stem + ".wav")
    print(f"[INFO] {src.name} -> {dst.name} | id='{ident}' codec={codec} rate={rate} ch={channels}")
    if dry:
        return

    data = src.read_bytes()
    if codec == 'pcm':
        sw = int(prm['sample_width'])
        end = prm.get('endianness', 'le').lower()
        sgn = bool(prm.get('signed', (sw == 2)))
        pcm = pcm_fix(data, sw, end, sgn)
        # WAV: use 1 or 2 bytes per sample as given (if 1, it's 8-bit; if 2, it's 16-bit)
        sampwidth = 1 if sw == 1 else 2
        with wave.open(str(dst), 'wb') as w:
            w.setnchannels(channels)
            w.setsampwidth(sampwidth)
            w.setframerate(rate)
            w.writeframes(pcm)

    elif codec == 'adpcm':
        pcm16 = decode_zvision_adpcm(data, channels)
        with wave.open(str(dst), 'wb') as w:
            w.setnchannels(channels)
            w.setsampwidth(2)  # 16-bit PCM
            w.setframerate(rate)
            w.writeframes(pcm16)
    else:
        print(f"[SKIP] {src.name}: unsupported codec '{codec}'")

def main():
    ap = argparse.ArgumentParser(description="Convert Z-Vision .raw (Nemesis/ZGI) to .wav")
    ap.add_argument("pattern", help="Glob for input files, e.g. 'C:\\path\\to\\*.raw'")
    ap.add_argument("--game", choices=["nemesis", "gi"], required=True, help="Which game rules to use")
    ap.add_argument("--out", default="out_wav", help="Output directory")
    ap.add_argument("--dry-run", action="store_true", help="List actions without writing files")
    args = ap.parse_args()

    files = [Path(p) for p in glob.glob(args.pattern)]
    if not files:
        print("No input files matched.")
        sys.exit(1)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    for f in files:
        convert_file(f, outdir, args.game, args.dry_run)

if __name__ == "__main__":
    main()
