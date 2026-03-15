#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nemesis_master_unpack_v3_5_3.py — Master driver for Zork Nemesis tools

v3.5.3:
- RLF: handle decoder-created subfolders correctly.
  * Recursively collects PNGs; if subfolders exist, move whole folders (PNG mode) or
    mux each folder to AVI (AVI mode).
  * If flat layout, fallback to grouping by numeric suffix (as before).
  * Robust pad detection for %0Nd patterns; otherwise uses concat list.
- Keeps v3.5.2 improvements (AVI fallbacks, TGA v2 path, temp cleanup, etc.)
"""

import os, sys, shutil, subprocess, tempfile, time, re
from pathlib import Path
from typing import List, Dict, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REQUIRED_DIRS = ["DATA1", "DATA2", "DATA3", "ZNEMMX", "ZNEMSCR"]

TYPES = {
    1: ("ZFS archives  → Extract", [".zfs"]),
    2: ("AVI videos    → MKV/PNG/AVI", [".avi"]),
    3: ("RLF videos    → MKV/PNG/AVI", [".rlf"]),
    4: ("RAW audio     → WAV", [".raw"]),
    5: ("TGA images    → BMP", [".tga"]),
}

# -------- console look --------
def clear_screen():
    try: os.system("cls" if os.name == "nt" else "clear")
    except Exception: pass

def set_colors():
    if os.name == "nt":
        try: os.system("color 5E")  # plum bg / lemon fg
        except Exception: pass

def reset_colors():
    if os.name == "nt":
        try: os.system("color 07")
        except Exception: pass

ASCII = r"""
   ╔═══════════════════════════════════════════════════════════════════╗
   ║                  Z-VISION DECOMPRESSION TOOLKIT                   ║
   ║          ✶ For Zork: Nemesis and Zork: Grand Inquisitor ✶         ║
   ╚═══════════════════════════════════════════════════════════════════╝
"""

# -------- helpers --------
def ask_path(prompt: str) -> Path:
    while True:
        p = input(prompt).strip().strip('"')
        if not p: continue
        path = Path(p).expanduser().resolve()
        if path.exists(): return path
        print(f"Path does not exist: {path}")

def verify_nemesis_root(root: Path) -> bool:
    miss = [d for d in REQUIRED_DIRS if not (root / d).exists()]
    if miss:
        print("\n[ERROR] Not a valid Zork Nemesis install (missing: " + ", ".join(miss) + ")\n")
        return False
    print("[OK]   Valid Zork Nemesis install confirmed.")
    return True

def which_exe(names: List[str]) -> Path:
    from shutil import which
    for n in names:
        w = which(n)
        if w: return Path(w)
        local = SCRIPT_DIR / n
        if local.exists(): return local
    return Path("")

def find_script(keywords: List[str]) -> Path|None:
    # find a .py whose filename contains ALL keywords (case-insensitive)
    for p in SCRIPT_DIR.glob("*.py"):
        name = p.name.lower()
        if all(k in name for k in keywords):
            return p
    # fallback: first .py with ANY keyword
    for p in SCRIPT_DIR.glob("*.py"):
        name = p.name.lower()
        if any(k in name for k in keywords):
            return p
    return None

def probe_help(script: Path) -> str:
    try:
        out = subprocess.run([sys.executable, str(script), "--help"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, check=False)
        return out.stdout.lower()
    except Exception:
        return ""

def pick_types() -> List[int]:
    print("\nSelect which asset types to process (comma-separated numbers).")
    for i in sorted(TYPES.keys()):
        print(f"  {i}. {TYPES[i][0]}")
    while True:
        resp = input("\nYour selection (e.g. 1,3,5): ").strip()
        if not resp: continue
        try:
            nums = sorted({int(x) for x in resp.replace(" ", "").split(",") if x})
            bad = [n for n in nums if n not in TYPES]
            if bad: print(f"Invalid choice(s): {bad}"); continue
            return nums
        except ValueError:
            print("Please enter numbers like: 1,2,5")

def stage_files(src_roots: List[Path], exts: List[str], stage_dir: Path) -> Tuple[int,int]:
    """Stage files; skip true duplicates (same name+size), rename different-size collisions with _dup."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    seen: Dict[str, int] = {}
    staged = skipped = 0
    for root in src_roots:
        for ext in exts:
            for p in root.rglob(f"*{ext}"):
                try: size = p.stat().st_size
                except Exception: continue
                name = p.name; stem, suffix = os.path.splitext(name); key = name.lower()
                if key in seen:
                    if seen[key] == size: skipped += 1; continue
                    while key in seen:
                        stem += "_dup"; name = stem + suffix; key = name.lower()
                seen[key] = size
                try:
                    shutil.copy2(p, stage_dir / name); staged += 1
                except Exception as e:
                    print(f"[WARN] Could not stage {p}: {e}")
    return staged, skipped

def spinner_run(cmd, cwd: Path|None=None, msg: str="Working", env=None) -> int:
    """Run a subprocess with a spinner; print last line seen so user knows it's alive."""
    spin = ["|", "/", "-", "\\"]
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, universal_newlines=True,
                                env=env)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"[ERROR] Launch failed: {e}")
        return 1
    i = 0; last = ""
    while True:
        line = proc.stdout.readline()
        if line: last = line.strip()
        if proc.poll() is not None: break
        sys.stdout.write(f"\r{msg} {spin[i%4]}  {last[:80]:80s}")
        sys.stdout.flush()
        i += 1; time.sleep(0.15)
    sys.stdout.write("\r" + " " * 100 + "\r")
    return proc.wait()

def run(cmd: List[str], cwd: Path|None=None, long_msg: str|None=None, env=None) -> int:
    if long_msg:
        return spinner_run(cmd, cwd=cwd, msg=long_msg, env=env)
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=False, env=env).returncode
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"[ERROR] Launch failed: {e}")
        return 1

def pick_formats(selected_types: List[int], ffmpeg_ok: bool) -> Dict[int, str]:
    choices: Dict[int,str] = {}
    for t in selected_types:
        if t == 2:  # AVI
            modes = {"mkv"} if not ffmpeg_ok else {"mkv","png","avi"}
            print("\nAVI output format:")
            print("  Options:", ", ".join(sorted(modes)))
            while True:
                sel = (input(f"Choose for AVI [{sorted(modes)[0]}]: ").strip().lower()
                       or sorted(modes)[0])
                if sel in modes: choices[t] = sel; break
                print("Invalid choice.")
        elif t == 3:  # RLF
            modes = {"png"} if not ffmpeg_ok else {"mkv","png","avi"}
            print("\nRLF output format:")
            print("  Options:", ", ".join(sorted(modes)))
            while True:
                sel = (input(f"Choose for RLF [{sorted(modes)[0]}]: ").strip().lower()
                       or sorted(modes)[0])
                if sel in modes: choices[t] = sel; break
                print("Invalid choice.")
        elif t == 4:  # RAW
            print("\nRAW decode mode:")
            print("  Options: nemesis, gi")
            while True:
                sel = (input("Choose for RAW [nemesis]: ").strip().lower() or "nemesis")
                if sel in ("nemesis","gi"): choices[t] = sel; break
                print("Invalid choice.")
        else:
            choices[t] = "default"
    return choices

def confirm_summary(root: Path, out_root: Path, selected_types: List[int], fmt_map: Dict[int,str]) -> bool:
    print("\nSummary of choices:")
    print(f"  Game install: {root}")
    print(f"  Output root : {out_root}")
    for t in selected_types:
        label = TYPES[t][0]; fmt = fmt_map.get(t, "default")
        print(f"  - {label}   → {fmt}")
    while True:
        ans = input("\nProceed with these choices? [Y/N]: ").strip().lower()
        if ans in ("y","yes"): return True
        if ans in ("n","no"): return False
        print("Please answer Y or N.")

def cleanup_stale_temp(prefix: str = "nemesis_stage_"):
    """Best-effort cleanup of abandoned temp workdirs from previous runs."""
    td = Path(tempfile.gettempdir())
    for p in td.glob(prefix + "*"):
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass

# --- RLF helpers ---
def find_rlf_groups(png_root: Path) -> Dict[str, List[Path]]:
    """
    Return mapping: clip_name -> list of PNG paths.
    If decoder created subfolders, use those folder names as clip names.
    Otherwise, group by common prefix before trailing digits.
    """
    groups: Dict[str, List[Path]] = {}

    # Case 1: subfolders exist with PNGs
    subfolders = [d for d in png_root.iterdir() if d.is_dir()]
    has_subfolder_pngs = any(list(d.glob("*.png")) for d in subfolders)
    if has_subfolder_pngs:
        for d in sorted(subfolders):
            frames = sorted(d.glob("*.png"))
            if frames:
                groups[d.name] = frames
        if groups:
            return groups  # done

    # Case 2: flat layout — group by numeric suffix
    for p in sorted(png_root.glob("*.png")):
        stem = p.stem
        m = re.match(r"^(.*?)(\d+)$", stem)
        clip = m.group(1) if m else stem
        groups.setdefault(clip, []).append(p)
    return groups

def detect_zero_pad(frames: List[Path]) -> Tuple[str, int] | None:
    """
    Given a list of PNG filenames for one clip, try to detect a uniform
    prefix + zero-padded numeric suffix length. Return (prefix, pad) or None.
    """
    pads = []
    prefixes = []
    for f in frames:
        m = re.match(r"^(.*?)(\d+)\.png$", f.name)
        if not m:
            return None
        prefixes.append(m.group(1))
        pads.append(len(m.group(2)))
    if len(set(prefixes)) == 1 and len(set(pads)) == 1:
        return prefixes[0], pads[0]
    return None

# -------- main --------
def main():
    try:
        set_colors(); clear_screen(); print(ASCII)
        print("Welcome! This helper will extract/convert Zork Nemesis assets.\n")

        while True:
            root = ask_path("Enter the path to your Zork Nemesis install folder: ")
            if verify_nemesis_root(root): break

        out_root = ask_path("Enter an OUTPUT folder (will be created if missing): ")
        out_root.mkdir(parents=True, exist_ok=True)

        ffmpeg = which_exe(["ffmpeg","ffmpeg.exe"])
        if not ffmpeg:
            print("[INFO] ffmpeg not detected — AVI/RLF menus will only offer MKV/PNG accordingly.\n")
        quickbms = which_exe(["quickbms","quickbms.exe"])

        # find sub-tools (flexible names)
        zfs_script = find_script(["zfs","unpack"]) or find_script(["zfs"])

        # Prefer exact avi2lossless.py; fallback to heuristics
        avi_script = SCRIPT_DIR / "avi2lossless.py"
        if not avi_script.exists():
            avi_script = find_script(["avi","lossless","multi"]) or find_script(["avi","lossless"]) or find_script(["avi"])
        print(f"[INFO] Using AVI tool: {avi_script}")

        rlf_script = find_script(["rlf","lossless"]) or find_script(["rlf","decoder"]) or find_script(["rlf"])
        raw_script = find_script(["raw","zvision"]) or find_script(["raw"])
        # TGA unpacker is named 'unpacker.py' in v2; prefer exact match first
        tga_script = SCRIPT_DIR / "unpacker.py" if (SCRIPT_DIR / "unpacker.py").exists() else find_script(["unpacker","tga"]) or find_script(["tga"])
        tga_bms   = SCRIPT_DIR / "tga.bms"

        # choose types & formats
        while True:
            selected_types = pick_types()
            fmt_map = pick_formats(selected_types, bool(ffmpeg))
            if confirm_summary(root, out_root, selected_types, fmt_map): break
            print("\nOkay, let's choose file types again.")

        if any(t in selected_types for t in (2,3)) and not ffmpeg:
            print("\n[WARN] ffmpeg not found (PATH or next to this script). Download: https://ffmpeg.org/download.html\n")
        if 5 in selected_types and not quickbms:
            print("\n[WARN] quickbms.exe not found (PATH or next to this script). Download: http://aluigi.altervista.org/quickbms.htm\n")

        data_roots = [root / d for d in REQUIRED_DIRS]
        tmp_root = Path(tempfile.mkdtemp(prefix="nemesis_stage_"))
        try:
            # ZFS
            if 1 in selected_types and zfs_script:
                stage = tmp_root / "ZFS"
                staged, skipped = stage_files(data_roots, TYPES[1][1], stage)
                print(f"[INFO] Staged {staged} ZFS files (skipped {skipped} identical duplicates).")
                if staged:
                    out_dir = out_root / "ZFS_EXTRACT"; out_dir.mkdir(parents=True, exist_ok=True)
                    run([sys.executable, str(zfs_script), str(stage / "*.zfs"), "--out", str(out_dir)],
                        long_msg="Extracting ZFS archives")

            # AVI (robust PNG/AVI fallbacks)
            if 2 in selected_types and avi_script:
                stage = tmp_root / "AVI"
                staged, skipped = stage_files(data_roots, TYPES[2][1], stage)
                print(f"[INFO] Staged {staged} AVI files (skipped {skipped} identical duplicates).")
                if staged:
                    choice = fmt_map[2]  # mkv/png/avi
                    out_dir = out_root / f"AVI_{'PNG' if choice=='png' else choice.upper()}"
                    out_dir.mkdir(parents=True, exist_ok=True)

                    help_txt = probe_help(avi_script)

                    def run_to_mkv(temp_mkv_dir: Path) -> int:
                        temp_mkv_dir.mkdir(parents=True, exist_ok=True)
                        if "--outmode" in help_txt and ffmpeg:
                            cmd = [sys.executable, str(avi_script), str(stage / "*.avi"),
                                   "--out", str(temp_mkv_dir), "--outmode", "mkv"]
                            if "--ffv1" in help_txt:
                                cmd += ["--ffv1"]
                        else:
                            cmd = [sys.executable, str(avi_script), str(stage / "*.avi"),
                                   "--out", str(temp_mkv_dir)]
                        return run(cmd, long_msg="Converting AVI → MKV")

                    if choice == "mkv":
                        if "--outmode" in help_txt and ffmpeg:
                            cmd = [sys.executable, str(avi_script), str(stage / "*.avi"),
                                   "--out", str(out_dir), "--outmode", "mkv"]
                            if "--ffv1" in help_txt:
                                cmd += ["--ffv1"]
                            run(cmd, long_msg="Converting AVI → MKV")
                        else:
                            run([sys.executable, str(avi_script), str(stage / "*.avi"), "--out", str(out_dir)],
                                long_msg="Converting AVI → MKV")

                    elif choice == "png":
                        tried_direct = False
                        if "--outmode" in help_txt and ffmpeg:
                            tried_direct = True
                            rc = run([sys.executable, str(avi_script), str(stage / "*.avi"),
                                      "--out", str(out_dir), "--outmode", "png"],
                                     long_msg="Decoding AVI → PNG")
                            if rc != 0:
                                print("[INFO] Direct PNG failed; using MKV→PNG fallback.")
                                tried_direct = False
                        if not tried_direct:
                            if not ffmpeg:
                                print("[WARN] ffmpeg not found; cannot do MKV→PNG fallback. Output will be MKV instead.")
                                run_to_mkv(out_dir)
                            else:
                                tmp_mkv = tmp_root / "AVI_MKV_TMP"
                                rc = run_to_mkv(tmp_mkv)
                                if rc == 0:
                                    for mkv in sorted(tmp_mkv.glob("*.mkv")):
                                        stem = mkv.stem
                                        seq_dir = out_dir / stem
                                        seq_dir.mkdir(parents=True, exist_ok=True)
                                        pattern = str(seq_dir / f"{stem}_%06d.png")
                                        cmd = [str(ffmpeg), "-y", "-i", str(mkv), "-vsync", "0", pattern]
                                        run(cmd, long_msg=f"Extracting PNGs from {mkv.name}")
                                else:
                                    print("[ERROR] Could not create MKVs for PNG fallback.")
                                try: shutil.rmtree(tmp_mkv, ignore_errors=True)
                                except Exception: pass

                    elif choice == "avi":
                        did_direct = False
                        if "--outmode" in help_txt and ffmpeg:
                            cmd = [sys.executable, str(avi_script), str(stage / "*.avi"),
                                   "--out", str(out_dir), "--outmode", "avi"]
                            if "--ffv1" in help_txt:
                                cmd += ["--ffv1"]
                            rc = run(cmd, long_msg="Converting AVI → FFV1 AVI")
                            did_direct = (rc == 0)
                        if not did_direct:
                            if not ffmpeg:
                                print("[WARN] ffmpeg not found; cannot produce FFV1 AVI. Falling back to MKV.")
                                run_to_mkv(out_dir)
                            else:
                                tmp_mkv = tmp_root / "AVI_MKV_TMP"
                                rc = run_to_mkv(tmp_mkv)
                                if rc == 0:
                                    for mkv in sorted(tmp_mkv.glob("*.mkv")):
                                        out_name = out_dir / (mkv.stem + ".avi")
                                        cmd = [str(ffmpeg), "-y", "-i", str(mkv),
                                               "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
                                               "-c:a", "copy",
                                               str(out_name)]
                                        run(cmd, long_msg=f"Remuxing {mkv.name} → FFV1 AVI")
                                else:
                                    print("[ERROR] Could not create MKVs for AVI fallback.")
                                try: shutil.rmtree(tmp_mkv, ignore_errors=True)
                                except Exception: pass

            # RLF (fixed: support decoder-created subfolders and flat layouts)
            if 3 in selected_types and rlf_script:
                stage = tmp_root / "RLF"
                staged, skipped = stage_files(data_roots, TYPES[3][1], stage)
                print(f"[INFO] Staged {staged} RLF files (skipped {skipped} identical duplicates).")
                if staged:
                    choice = fmt_map[3]  # mkv/png/avi
                    png_tmp = tmp_root / "RLF_PNG_TMP"; png_tmp.mkdir(parents=True, exist_ok=True)
                    help_txt = probe_help(rlf_script)

                    cmd = [sys.executable, str(rlf_script), str(stage / "*.rlf"), "--out", str(png_tmp)]
                    out_mkv = None
                    if choice == "mkv" and ffmpeg and "--mkv" in help_txt:
                        out_mkv = out_root / "RLF_MKV"; out_mkv.mkdir(parents=True, exist_ok=True)
                        cmd += ["--mkv", str(out_mkv)]
                    run(cmd, long_msg=f"Decoding RLF → PNG{' + MKV' if out_mkv else ''}")

                    groups = find_rlf_groups(png_tmp)

                    if choice == "png":
                        out_png = out_root / "RLF_PNG"; out_png.mkdir(parents=True, exist_ok=True)
                        subfolders = [d for d in png_tmp.iterdir() if d.is_dir() and list(d.glob("*.png"))]
                        if subfolders:
                            # Move entire subfolders as-is (preserve decoder’s layout)
                            for d in subfolders:
                                dest = out_png / d.name
                                if dest.exists():
                                    shutil.rmtree(dest, ignore_errors=True)
                                shutil.move(str(d), str(dest))
                        else:
                            # Flat → create per-clip folders and move frames
                            for clip, frames in groups.items():
                                clip_dir = out_png / clip
                                clip_dir.mkdir(parents=True, exist_ok=True)
                                for f in frames:
                                    shutil.move(str(f), str(clip_dir / f.name))

                    elif choice == "avi":
                        if not ffmpeg:
                            print("[WARN] ffmpeg not found; cannot produce AVI. Keeping PNGs instead.")
                            out_png = out_root / "RLF_PNG"; out_png.mkdir(parents=True, exist_ok=True)
                            # Move folders if present, else group into folders
                            subfolders = [d for d in png_tmp.iterdir() if d.is_dir() and list(d.glob("*.png"))]
                            if subfolders:
                                for d in subfolders:
                                    dest = out_png / d.name
                                    if dest.exists():
                                        shutil.rmtree(dest, ignore_errors=True)
                                    shutil.move(str(d), str(dest))
                            else:
                                for clip, frames in groups.items():
                                    clip_dir = out_png / clip
                                    clip_dir.mkdir(parents=True, exist_ok=True)
                                    for f in frames:
                                        shutil.move(str(f), str(clip_dir / f.name))
                        else:
                            out_avi = out_root / "RLF_AVI"; out_avi.mkdir(parents=True, exist_ok=True)
                            # For each group (folder or prefix), build an FFmpeg input
                            for clip, frames in groups.items():
                                # Prefer using the folder if frames live in one
                                folder = frames[0].parent
                                use_folder = folder != png_tmp
                                padinfo = detect_zero_pad(frames)
                                out_name = out_avi / f"{clip}.avi"

                                if use_folder and padinfo:
                                    prefix, pad = padinfo
                                    pattern = str((folder / f"{prefix}%0{pad}d.png"))
                                    cmd = [str(ffmpeg), "-y", "-r", "15", "-i", pattern,
                                           "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
                                           str(out_name)]
                                elif padinfo:
                                    prefix, pad = padinfo
                                    pattern = str(png_tmp / f"{prefix}%0{pad}d.png")
                                    cmd = [str(ffmpeg), "-y", "-r", "15", "-i", pattern,
                                           "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
                                           str(out_name)]
                                else:
                                    # Fallback: concat list (robust to odd names & gaps)
                                    listfile = png_tmp / f"{clip}_list.txt"
                                    listfile.write_text("".join(f"file '{f.as_posix()}'\n" for f in frames), encoding="utf-8")
                                    cmd = [str(ffmpeg), "-y", "-r", "15", "-f", "concat", "-safe", "0",
                                           "-i", str(listfile), "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
                                           str(out_name)]

                                run(cmd, long_msg=f"Muxing RLF {clip} → FFV1 AVI")

                    # Clean the temporary PNG workspace for RLF
                    try: shutil.rmtree(png_tmp, ignore_errors=True)
                    except Exception: pass

            # RAW
            if 4 in selected_types and raw_script:
                stage = tmp_root / "RAW"
                staged, skipped = stage_files(data_roots, TYPES[4][1], stage)
                print(f"[INFO] Staged {staged} RAW files (skipped {skipped} identical duplicates).")
                if staged:
                    mode = fmt_map.get(4, "nemesis")
                    out_wav = out_root / "RAW_WAV"; out_wav.mkdir(parents=True, exist_ok=True)
                    run([sys.executable, str(raw_script), str(stage / "*.raw"), "--game", mode, "--out", str(out_wav)],
                        long_msg=f"Decoding RAW ({mode}) → WAV")

            # TGA — v2-proven approach (plus cleanup and UTF-8)
            if 5 in selected_types and tga_script:
                stage = tmp_root / "TGA_STAGE"
                staged, skipped = stage_files(data_roots, TYPES[5][1], stage)
                print(f"[INFO] Staged {staged} TGA files (skipped {skipped} identical duplicates).")
                if staged:
                    tga_env = SCRIPT_DIR / "__tga_env"
                    (tga_env / "TGA").mkdir(parents=True, exist_ok=True)
                    # copy staged TGAs exactly like v2
                    for p in stage.glob("*.tga"):
                        shutil.copy2(p, tga_env / "TGA" / p.name)
                    # place script & helpers
                    shutil.copy2(tga_script, tga_env / "unpacker.py")
                    qb = which_exe(["quickbms","quickbms.exe"])
                    if qb and qb.exists():
                        shutil.copy2(qb, tga_env / "quickbms.exe")
                    else:
                        print("[WARN] quickbms.exe not found; TGA decompression may fail.")
                    tga_bms = SCRIPT_DIR / "tga.bms"
                    if tga_bms.exists():
                        shutil.copy2(tga_bms, tga_env / "tga.bms")
                    else:
                        print("[WARN] tga.bms not found next to this script; required by unpacker.py.")
                    # run unpacker.py in that env; force UTF-8 so banners don't crash
                    out_bmp = out_root / "TGA_BMP"; out_bmp.mkdir(parents=True, exist_ok=True)
                    print("[INFO] Decompressing TGAs… This step can take several minutes.")
                    child_env = os.environ.copy()
                    child_env["PYTHONIOENCODING"] = "utf-8"
                    run([sys.executable, str(tga_env / "unpacker.py")], cwd=tga_env,
                        long_msg="Decompressing TGA images", env=child_env)
                    # copy BMPs out (same as v2)
                    local_bmp = tga_env / "BMP"
                    if local_bmp.exists():
                        moved = 0
                        for p in local_bmp.glob("*.bmp"):
                            shutil.copy2(p, out_bmp / p.name)
                            moved += 1
                        if moved == 0:
                            print("[WARN] No BMPs were produced by the TGA unpacker.")
                        else:
                            # clean env only if success
                            try: shutil.rmtree(tga_env, ignore_errors=True)
                            except Exception: pass
                    else:
                        print("[WARN] TGA unpacker did not produce a BMP folder.")

            print("\nAll requested tasks finished. Output root:\n  ", out_root)

        finally:
            # clean this run's staging area
            try: shutil.rmtree(tmp_root, ignore_errors=True)
            except Exception: pass
            # sweep any stale staging dirs from previous runs
            cleanup_stale_temp()

    finally:
        reset_colors()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        reset_colors()
        print("\nAborted by user.")
