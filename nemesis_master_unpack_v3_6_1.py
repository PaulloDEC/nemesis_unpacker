#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nemesis_master_unpack_v3_6_1.py — Master driver for Zork Nemesis tools

v3.6.1:
- Removed all Grand Inquisitor references; RAW always decodes as Nemesis.
- Stage divider lines used consistently throughout the run flow.

v3.6.0:
- UX overhaul:
  * ANSI colour output (cross-platform; replaces Windows-only `color` command).
    INFO=cyan, OK=green, WARN=yellow, ERROR=red, headers=gold.
  * Banner now includes version number and one-line description.
  * TYPES labels dynamically padded; no more manual space-padding.
  * Dependency scan (ffmpeg, quickbms, sub-scripts) runs BEFORE the type
    picker, so missing tools are flagged in the menu itself.
  * "0 = all types" shortcut added to pick_types().
  * Output folder defaults to <install>/EXTRACTED (user can override).
  * spinner_run buffers last 10 lines; on non-zero exit prints them so the
    user can see the actual child-process error.
  * Skipped-duplicate count only printed when > 0.
  * Per-task result counters accumulated; final summary table printed.
  * Run log written to <output_root>/nemesis_unpack_<timestamp>.log.

v3.5.3 (retained):
- RLF: handle decoder-created subfolders correctly.
  * Recursively collects PNGs; if subfolders exist, move whole folders (PNG
    mode) or mux each folder to AVI (AVI mode).
  * If flat layout, fallback to grouping by numeric suffix (as before).
  * Robust pad detection for %0Nd patterns; otherwise uses concat list.
- Keeps v3.5.2 improvements (AVI fallbacks, TGA v2 path, temp cleanup, etc.)
"""

import collections
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION     = "3.6.1"
SCRIPT_DIR  = Path(__file__).resolve().parent
REQUIRED_DIRS = ["DATA1", "DATA2", "DATA3", "ZNEMMX", "ZNEMSCR"]

TYPES = {
    1: ("ZFS archives → Extract",      [".zfs"]),
    2: ("AVI videos  → MKV / PNG / AVI", [".avi"]),
    3: ("RLF videos  → MKV / PNG / AVI", [".rlf"]),
    4: ("RAW audio   → WAV",            [".raw"]),
    5: ("TGA images  → BMP",            [".tga"]),
}

# ── ANSI colour helpers ───────────────────────────────────────────────────────

def _windows_ansi_ok() -> bool:
    """Enable VT processing on Windows 10+; return True if it worked."""
    try:
        import ctypes
        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
        mode   = ctypes.c_ulong(0)
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)
            return True
    except Exception:
        pass
    return False

_ANSI = os.name != "nt" or _windows_ansi_ok()

_RESET  = "\033[0m"  if _ANSI else ""
_GOLD   = "\033[33m" if _ANSI else ""   # banner / headers
_CYAN   = "\033[36m" if _ANSI else ""   # INFO
_GREEN  = "\033[32m" if _ANSI else ""   # OK
_YELLOW = "\033[93m" if _ANSI else ""   # WARN
_RED    = "\033[91m" if _ANSI else ""   # ERROR
_DIM    = "\033[2m"  if _ANSI else ""   # dimmed / unavailable

def _tag(colour: str, label: str, msg: str) -> str:
    return f"{colour}[{label}]{_RESET} {msg}"

def info (msg: str) -> None: _log_and_print(_tag(_CYAN,   "INFO",  msg))
def ok   (msg: str) -> None: _log_and_print(_tag(_GREEN,  "OK",    msg))
def warn (msg: str) -> None: _log_and_print(_tag(_YELLOW, "WARN",  msg))
def error(msg: str) -> None: _log_and_print(_tag(_RED,    "ERROR", msg))

def _log_and_print(line: str) -> None:
    """Print to stdout and mirror to the run log (strip ANSI for the file)."""
    print(line)
    logging.info(_strip_ansi(line))

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)

def divider(title: str) -> None:
    """Print a gold stage-divider line, e.g.  ── ZFS ──────────────────"""
    pad = "─" * max(0, 54 - len(title))
    print(f"\n{_GOLD}  ── {title} {pad}{_RESET}")

# ── Banner ────────────────────────────────────────────────────────────────────

def _banner() -> str:
    ver_line = f"v{VERSION} — extract and convert Zork Nemesis assets"
    width    = 67
    return (
        f"{_GOLD}"
        f"   ╔{'═'*width}╗\n"
        f"   ║{'Z-VISION DECOMPRESSION TOOLKIT'.center(width)}║\n"
        f"   ║{'✶  For Zork: Nemesis  ✶'.center(width)}║\n"
        f"   ║{ver_line.center(width)}║\n"
        f"   ╚{'═'*width}╝"
        f"{_RESET}"
    )

# ── Console helpers ───────────────────────────────────────────────────────────

def clear_screen() -> None:
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass

# ── Path / tool helpers ───────────────────────────────────────────────────────

def ask_path(prompt: str, default: Optional[Path] = None) -> Path:
    """Prompt for a path; accepts empty input to use *default* if supplied."""
    while True:
        suffix = f" [{default}]" if default else ""
        raw    = input(f"{prompt}{suffix}: ").strip().strip('"')
        if not raw:
            if default is not None:
                return default
            continue
        path = Path(raw).expanduser().resolve()
        if path.exists():
            return path
        # For the output path the directory may not exist yet — create it.
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except Exception:
            print(f"  Path does not exist and could not be created: {path}")

def verify_nemesis_root(root: Path) -> bool:
    miss = [d for d in REQUIRED_DIRS if not (root / d).exists()]
    if miss:
        error("Not a valid Zork Nemesis install (missing: " + ", ".join(miss) + ")")
        return False
    ok("Valid Zork Nemesis install confirmed.")
    return True

def which_exe(names: List[str]) -> Path:
    for n in names:
        w = shutil.which(n)
        if w:
            return Path(w)
        local = SCRIPT_DIR / n
        if local.exists():
            return local
    return Path("")

def find_script(keywords: List[str]) -> Optional[Path]:
    """Find a .py whose filename contains ALL keywords (case-insensitive)."""
    for p in SCRIPT_DIR.glob("*.py"):
        name = p.name.lower()
        if all(k in name for k in keywords):
            return p
    # Fallback: first .py with ANY keyword.
    for p in SCRIPT_DIR.glob("*.py"):
        name = p.name.lower()
        if any(k in name for k in keywords):
            return p
    return None

def probe_help(script: Path) -> str:
    try:
        out = subprocess.run(
            [sys.executable, str(script), "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False,
        )
        return out.stdout.lower()
    except Exception:
        return ""

# ── Dependency discovery (runs before the type picker) ───────────────────────

class Deps:
    """Holds discovered tool paths and availability flags."""
    def __init__(self) -> None:
        self.ffmpeg:     Path = Path("")
        self.quickbms:   Path = Path("")
        self.zfs_script: Optional[Path] = None
        self.avi_script: Optional[Path] = None
        self.rlf_script: Optional[Path] = None
        self.raw_script: Optional[Path] = None
        self.tga_script: Optional[Path] = None

    @property
    def ffmpeg_ok(self)   -> bool: return bool(self.ffmpeg)
    @property
    def quickbms_ok(self) -> bool: return bool(self.quickbms)


def discover_deps() -> Deps:
    d = Deps()
    d.ffmpeg   = which_exe(["ffmpeg", "ffmpeg.exe"])
    d.quickbms = which_exe(["quickbms", "quickbms.exe"])

    avi_exact = SCRIPT_DIR / "avi2lossless.py"
    d.avi_script = (
        avi_exact if avi_exact.exists()
        else find_script(["avi", "lossless", "multi"])
           or find_script(["avi", "lossless"])
           or find_script(["avi"])
    )
    d.zfs_script = find_script(["zfs", "unpack"]) or find_script(["zfs"])
    d.rlf_script = (
        find_script(["rlf", "lossless"])
        or find_script(["rlf", "decoder"])
        or find_script(["rlf"])
    )
    d.raw_script = find_script(["raw", "zvision"]) or find_script(["raw"])
    tga_exact = SCRIPT_DIR / "unpacker.py"
    d.tga_script = (
        tga_exact if tga_exact.exists()
        else find_script(["unpacker", "tga"]) or find_script(["tga"])
    )
    return d


def _found(path: Optional[Path]) -> bool:
    return bool(path) and Path(path).exists()  # type: ignore[arg-type]


def print_dep_report(d: Deps) -> None:
    """Print a dependency status table so users know what's available."""
    divider("Dependency check")

    def row(label: str, found: bool, detail: str = "") -> None:
        if found:
            print(f"  {_GREEN}✔{_RESET}  {label:<22} {_DIM}{detail}{_RESET}")
        else:
            print(f"  {_YELLOW}✘{_RESET}  {label:<22} {_YELLOW}NOT FOUND{_RESET}")

    row("ffmpeg",   d.ffmpeg_ok,   str(d.ffmpeg)   if d.ffmpeg_ok   else "")
    row("quickbms", d.quickbms_ok, str(d.quickbms) if d.quickbms_ok else "")
    print()
    row("ZFS sub-script", _found(d.zfs_script), d.zfs_script.name if _found(d.zfs_script) else "")
    row("AVI sub-script", _found(d.avi_script), d.avi_script.name if _found(d.avi_script) else "")
    row("RLF sub-script", _found(d.rlf_script), d.rlf_script.name if _found(d.rlf_script) else "")
    row("RAW sub-script", _found(d.raw_script), d.raw_script.name if _found(d.raw_script) else "")
    row("TGA sub-script", _found(d.tga_script), d.tga_script.name if _found(d.tga_script) else "")
    print()

# ── Type / format pickers ─────────────────────────────────────────────────────

# Maps type number → which sub-script and external tool it needs.
_TYPE_NEEDS: Dict[int, Tuple[str, str]] = {
    1: ("zfs_script",  ""),
    2: ("avi_script",  ""),          # ffmpeg optional but noted
    3: ("rlf_script",  ""),
    4: ("raw_script",  ""),
    5: ("tga_script",  "quickbms"),
}


def pick_types(d: Deps) -> List[int]:
    """
    Present the type menu with inline availability notes.
    Accepts comma-separated numbers or '0' for all available types.
    """
    available: List[int] = []

    divider("Select asset types")
    print(f"  {_DIM}(comma-separated numbers, or 0 = all available){_RESET}\n")

    label_w = max(len(v[0]) for v in TYPES.values()) + 2

    for i in sorted(TYPES.keys()):
        label, _ = TYPES[i]
        script_attr, ext_attr = _TYPE_NEEDS[i]
        script_ok = _found(getattr(d, script_attr))
        ext_ok    = (not ext_attr) or _found(getattr(d, ext_attr, Path("")))

        notes: List[str] = []
        if not script_ok:
            notes.append(f"sub-script not found")
        if ext_attr and not ext_ok:
            notes.append(f"{ext_attr} not found")
        if i in (2, 3) and not d.ffmpeg_ok:
            notes.append("ffmpeg not found — MKV/AVI output unavailable")

        usable = script_ok and ext_ok
        if usable:
            available.append(i)
            note_str = f"  {_DIM}{'; '.join(notes)}{_RESET}" if notes else ""
            print(f"  {i}.  {_GREEN}{label:<{label_w}}{_RESET}{note_str}")
        else:
            note_str = f"  {_YELLOW}({'; '.join(notes)}){_RESET}"
            print(f"  {_DIM}{i}.  {label:<{label_w}}{_RESET}{note_str}")

    print(f"  {_CYAN}0.  All available types{_RESET}")

    while True:
        resp = input("\n  Your selection: ").strip()
        if not resp:
            continue
        if resp.strip() == "0":
            if not available:
                warn("No types are available — check missing sub-scripts above.")
                continue
            info(f"Selecting all available: {', '.join(str(n) for n in available)}")
            return available
        try:
            nums = sorted({int(x) for x in resp.replace(" ", "").split(",") if x})
            bad  = [n for n in nums if n not in TYPES]
            if bad:
                warn(f"Invalid choice(s): {bad}")
                continue
            unavail = [n for n in nums if n not in available]
            if unavail:
                warn(f"Type(s) {unavail} cannot be used (missing dependencies — see above).")
                continue
            return nums
        except ValueError:
            warn("Please enter numbers like: 1,2,5  (or 0 for all)")


def pick_formats(selected_types: List[int], d: Deps) -> Dict[int, str]:
    choices: Dict[int, str] = {}

    for t in selected_types:
        if t == 2:  # AVI
            modes = {"mkv"} if not d.ffmpeg_ok else {"mkv", "png", "avi"}
            _print_format_prompt("AVI", modes)
            choices[t] = _read_format(modes)

        elif t == 3:  # RLF
            modes = {"png"} if not d.ffmpeg_ok else {"mkv", "png", "avi"}
            _print_format_prompt("RLF", modes)
            choices[t] = _read_format(modes)

        elif t == 4:  # RAW — always Nemesis
            choices[t] = "nemesis"
        else:
            choices[t] = "default"

    return choices


def _print_format_prompt(label: str, modes: set) -> None:
    divider(f"{label} output format")
    print(f"  {_DIM}Options: {', '.join(sorted(modes))}{_RESET}")


def _read_format(modes: set) -> str:
    default = sorted(modes)[0]
    while True:
        sel = (input(f"  Choose [{default}]: ").strip().lower() or default)
        if sel in modes:
            return sel
        warn("Invalid choice.")


def confirm_summary(root: Path, out_root: Path,
                    selected_types: List[int], fmt_map: Dict[int, str]) -> bool:
    divider("Summary")
    print(f"  Game install : {root}")
    print(f"  Output root  : {out_root}")
    for t in selected_types:
        label = TYPES[t][0]
        fmt   = fmt_map.get(t, "default")
        print(f"  {_CYAN}•{_RESET} {label:<38} → {fmt}")
    print()
    while True:
        ans = input("  Proceed? [Y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        warn("Please answer Y or N.")

# ── File staging ──────────────────────────────────────────────────────────────

def stage_files(src_roots: List[Path], exts: List[str],
                stage_dir: Path) -> Tuple[int, int]:
    """Stage files; skip true duplicates (same name+size), rename collisions with _dup."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    seen:    Dict[str, int] = {}
    staged = skipped = 0
    for root in src_roots:
        for ext in exts:
            for p in root.rglob(f"*{ext}"):
                try:
                    size = p.stat().st_size
                except Exception:
                    continue
                name   = p.name
                stem, suffix = os.path.splitext(name)
                key    = name.lower()
                if key in seen:
                    if seen[key] == size:
                        skipped += 1
                        continue
                    while key in seen:
                        stem += "_dup"
                        name  = stem + suffix
                        key   = name.lower()
                seen[key] = size
                try:
                    shutil.copy2(p, stage_dir / name)
                    staged += 1
                except Exception as e:
                    warn(f"Could not stage {p}: {e}")
    return staged, skipped

# ── Subprocess runners ────────────────────────────────────────────────────────

def spinner_run(cmd, cwd: Optional[Path] = None,
                msg: str = "Working", env=None) -> int:
    """
    Run a subprocess with a spinner.  Shows the last output line while
    running.  On non-zero exit, dumps the last ≤10 lines so the user can
    see the actual error from the child process.
    """
    spin     = ["|", "/", "-", "\\"]
    tail_buf: collections.deque = collections.deque(maxlen=10)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True,
            env=env,
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        error(f"Launch failed: {e}")
        return 1

    i = 0
    last = ""
    while True:
        line = proc.stdout.readline()
        if line:
            last = line.strip()
            tail_buf.append(last)
        if proc.poll() is not None:
            # Drain any remaining output.
            for remaining in proc.stdout:
                s = remaining.strip()
                if s:
                    last = s
                    tail_buf.append(s)
            break
        sys.stdout.write(f"\r{msg} {spin[i % 4]}  {last[:80]:80s}")
        sys.stdout.flush()
        i += 1
        time.sleep(0.15)

    sys.stdout.write("\r" + " " * 100 + "\r")
    rc = proc.wait()

    if rc != 0 and tail_buf:
        warn(f"Sub-process exited with code {rc}. Last output:")
        for ln in tail_buf:
            print(f"    {_DIM}{ln}{_RESET}")
        logging.warning("Sub-process tail:\n" + "\n".join(tail_buf))

    return rc


def run(cmd: List[str], cwd: Optional[Path] = None,
        long_msg: Optional[str] = None, env=None) -> int:
    if long_msg:
        return spinner_run(cmd, cwd=cwd, msg=long_msg, env=env)
    try:
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            check=False, env=env,
        ).returncode
    except KeyboardInterrupt:
        raise
    except Exception as e:
        error(f"Launch failed: {e}")
        return 1

# ── RLF helpers ───────────────────────────────────────────────────────────────

def find_rlf_groups(png_root: Path) -> Dict[str, List[Path]]:
    """Return mapping: clip_name → list of PNG paths."""
    groups: Dict[str, List[Path]] = {}
    subfolders = [d for d in png_root.iterdir() if d.is_dir()]
    has_subfolder_pngs = any(list(d.glob("*.png")) for d in subfolders)
    if has_subfolder_pngs:
        for d in sorted(subfolders):
            frames = sorted(d.glob("*.png"))
            if frames:
                groups[d.name] = frames
        if groups:
            return groups
    for p in sorted(png_root.glob("*.png")):
        stem = p.stem
        m    = re.match(r"^(.*?)(\d+)$", stem)
        clip = m.group(1) if m else stem
        groups.setdefault(clip, []).append(p)
    return groups


def detect_zero_pad(frames: List[Path]) -> Optional[Tuple[str, int]]:
    """Detect uniform prefix + zero-padded suffix length. Return (prefix, pad) or None."""
    pads:     List[int] = []
    prefixes: List[str] = []
    for f in frames:
        m = re.match(r"^(.*?)(\d+)\.png$", f.name)
        if not m:
            return None
        prefixes.append(m.group(1))
        pads.append(len(m.group(2)))
    if len(set(prefixes)) == 1 and len(set(pads)) == 1:
        return prefixes[0], pads[0]
    return None

# ── Stale-temp cleanup ────────────────────────────────────────────────────────

def cleanup_stale_temp(prefix: str = "nemesis_stage_") -> None:
    td = Path(tempfile.gettempdir())
    for p in td.glob(prefix + "*"):
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass

# ── Final summary table ───────────────────────────────────────────────────────

class RunStats:
    """Accumulates per-task counts for the final summary."""
    def __init__(self) -> None:
        self._rows: List[Tuple[str, int, List[str]]] = []   # (label, count, warnings)

    def record(self, label: str, count: int, warnings: Optional[List[str]] = None) -> None:
        self._rows.append((label, count, warnings or []))

    def print_summary(self, out_root: Path) -> None:
        divider("Run complete")
        print(f"  Output root: {out_root}\n")
        for label, count, warnings in self._rows:
            icon = _GREEN + "✔" + _RESET if (count > 0 and not warnings) else (
                   _YELLOW + "!" + _RESET if warnings else _DIM + "–" + _RESET)
            print(f"  {icon}  {label:<38}  {count:>5} file(s)")
            for w in warnings:
                print(f"       {_YELLOW}↳ {w}{_RESET}")
        print()
        logging.info("Run complete. Output: %s", out_root)

# ── Main ──────────────────────────────────────────────────────────────────────

def _setup_logging(out_root: Path) -> None:
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path   = out_root / f"nemesis_unpack_{timestamp}.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    info(f"Run log: {log_path}")


def main() -> None:
    clear_screen()
    print(_banner())
    print()

    # ── Locate game install ───────────────────────────────────────────────────
    divider("Game install")
    while True:
        root = ask_path("  Game install folder")
        if verify_nemesis_root(root):
            break

    # ── Discover dependencies before the user picks types ────────────────────
    d = discover_deps()
    print_dep_report(d)

    # ── Output path (default = <install>/EXTRACTED) ───────────────────────────
    divider("Output folder")
    default_out = root / "EXTRACTED"
    out_root    = ask_path("  Output folder", default=default_out)
    out_root.mkdir(parents=True, exist_ok=True)

    # ── Start logging now that we have an output path ─────────────────────────
    _setup_logging(out_root)
    logging.info("Game install: %s", root)
    logging.info("Output root:  %s", out_root)

    # ── Type/format selection loop ────────────────────────────────────────────
    while True:
        selected_types = pick_types(d)
        fmt_map        = pick_formats(selected_types, d)
        if confirm_summary(root, out_root, selected_types, fmt_map):
            break
        print("  Okay, let's choose again.\n")

    logging.info("Selected types: %s", selected_types)
    logging.info("Format map:     %s", fmt_map)

    # ── Convenience aliases ───────────────────────────────────────────────────
    ffmpeg     = d.ffmpeg
    zfs_script = d.zfs_script
    avi_script = d.avi_script
    rlf_script = d.rlf_script
    raw_script = d.raw_script
    tga_script = d.tga_script

    data_roots = [root / dd for dd in REQUIRED_DIRS]
    stats      = RunStats()
    tmp_root   = Path(tempfile.mkdtemp(prefix="nemesis_stage_"))

    try:
        # ── ZFS ───────────────────────────────────────────────────────────────
        if 1 in selected_types and zfs_script:
            divider("ZFS archives")
            stage = tmp_root / "ZFS"
            staged, skipped = stage_files(data_roots, TYPES[1][1], stage)
            info(f"Staged {staged} ZFS file(s)." +
                 (f" Skipped {skipped} identical duplicate(s)." if skipped else ""))
            if staged:
                out_dir = out_root / "ZFS_EXTRACT"
                out_dir.mkdir(parents=True, exist_ok=True)
                rc = run(
                    [sys.executable, str(zfs_script), str(stage / "*.zfs"), "--out", str(out_dir)],
                    long_msg="Extracting ZFS archives",
                )
                produced = sum(1 for _ in out_dir.rglob("*") if _.is_file())
                ws = [f"sub-process exited {rc}"] if rc != 0 else []
                stats.record("ZFS → extracted", produced, ws)

        # ── AVI ───────────────────────────────────────────────────────────────
        if 2 in selected_types and avi_script:
            divider("AVI videos")
            stage = tmp_root / "AVI"
            staged, skipped = stage_files(data_roots, TYPES[2][1], stage)
            info(f"Staged {staged} AVI file(s)." +
                 (f" Skipped {skipped} identical duplicate(s)." if skipped else ""))
            if staged:
                choice  = fmt_map[2]
                out_dir = out_root / f"AVI_{'PNG' if choice == 'png' else choice.upper()}"
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
                        run([sys.executable, str(avi_script), str(stage / "*.avi"),
                             "--out", str(out_dir)],
                            long_msg="Converting AVI → MKV")

                elif choice == "png":
                    tried_direct = False
                    if "--outmode" in help_txt and ffmpeg:
                        tried_direct = True
                        rc = run([sys.executable, str(avi_script), str(stage / "*.avi"),
                                  "--out", str(out_dir), "--outmode", "png"],
                                 long_msg="Decoding AVI → PNG")
                        if rc != 0:
                            info("Direct PNG failed; using MKV→PNG fallback.")
                            tried_direct = False
                    if not tried_direct:
                        if not ffmpeg:
                            warn("ffmpeg not found; cannot do MKV→PNG fallback. Output will be MKV.")
                            run_to_mkv(out_dir)
                        else:
                            tmp_mkv = tmp_root / "AVI_MKV_TMP"
                            rc = run_to_mkv(tmp_mkv)
                            if rc == 0:
                                for mkv in sorted(tmp_mkv.glob("*.mkv")):
                                    stem    = mkv.stem
                                    seq_dir = out_dir / stem
                                    seq_dir.mkdir(parents=True, exist_ok=True)
                                    pattern = str(seq_dir / f"{stem}_%06d.png")
                                    run([str(ffmpeg), "-y", "-i", str(mkv), "-vsync", "0", pattern],
                                        long_msg=f"Extracting PNGs from {mkv.name}")
                            else:
                                error("Could not create MKVs for PNG fallback.")
                            try:
                                shutil.rmtree(tmp_mkv, ignore_errors=True)
                            except Exception:
                                pass

                elif choice == "avi":
                    did_direct = False
                    if "--outmode" in help_txt and ffmpeg:
                        cmd = [sys.executable, str(avi_script), str(stage / "*.avi"),
                               "--out", str(out_dir), "--outmode", "avi"]
                        if "--ffv1" in help_txt:
                            cmd += ["--ffv1"]
                        rc         = run(cmd, long_msg="Converting AVI → FFV1 AVI")
                        did_direct = (rc == 0)
                    if not did_direct:
                        if not ffmpeg:
                            warn("ffmpeg not found; cannot produce FFV1 AVI. Falling back to MKV.")
                            run_to_mkv(out_dir)
                        else:
                            tmp_mkv = tmp_root / "AVI_MKV_TMP"
                            rc      = run_to_mkv(tmp_mkv)
                            if rc == 0:
                                for mkv in sorted(tmp_mkv.glob("*.mkv")):
                                    out_name = out_dir / (mkv.stem + ".avi")
                                    run([str(ffmpeg), "-y", "-i", str(mkv),
                                         "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
                                         "-c:a", "copy", str(out_name)],
                                        long_msg=f"Remuxing {mkv.name} → FFV1 AVI")
                            else:
                                error("Could not create MKVs for AVI fallback.")
                            try:
                                shutil.rmtree(tmp_mkv, ignore_errors=True)
                            except Exception:
                                pass

                produced = sum(1 for _ in out_dir.rglob("*") if _.is_file())
                stats.record(f"AVI → {choice.upper()}", produced)

        # ── RLF ───────────────────────────────────────────────────────────────
        if 3 in selected_types and rlf_script:
            divider("RLF videos")
            stage = tmp_root / "RLF"
            staged, skipped = stage_files(data_roots, TYPES[3][1], stage)
            info(f"Staged {staged} RLF file(s)." +
                 (f" Skipped {skipped} identical duplicate(s)." if skipped else ""))
            if staged:
                choice   = fmt_map[3]
                png_tmp  = tmp_root / "RLF_PNG_TMP"
                png_tmp.mkdir(parents=True, exist_ok=True)
                help_txt = probe_help(rlf_script)

                cmd     = [sys.executable, str(rlf_script), str(stage / "*.rlf"), "--out", str(png_tmp)]
                out_mkv = None
                if choice == "mkv" and ffmpeg and "--mkv" in help_txt:
                    out_mkv = out_root / "RLF_MKV"
                    out_mkv.mkdir(parents=True, exist_ok=True)
                    cmd += ["--mkv", str(out_mkv)]
                run(cmd, long_msg=f"Decoding RLF → PNG{' + MKV' if out_mkv else ''}")

                groups = find_rlf_groups(png_tmp)

                if choice == "png":
                    out_png = out_root / "RLF_PNG"
                    out_png.mkdir(parents=True, exist_ok=True)
                    subfolders = [dd for dd in png_tmp.iterdir()
                                  if dd.is_dir() and list(dd.glob("*.png"))]
                    if subfolders:
                        for dd in subfolders:
                            dest = out_png / dd.name
                            if dest.exists():
                                shutil.rmtree(dest, ignore_errors=True)
                            shutil.move(str(dd), str(dest))
                    else:
                        for clip, frames in groups.items():
                            clip_dir = out_png / clip
                            clip_dir.mkdir(parents=True, exist_ok=True)
                            for f in frames:
                                shutil.move(str(f), str(clip_dir / f.name))

                elif choice == "avi":
                    if not ffmpeg:
                        warn("ffmpeg not found; cannot produce AVI. Keeping PNGs instead.")
                        out_png = out_root / "RLF_PNG"
                        out_png.mkdir(parents=True, exist_ok=True)
                        subfolders = [dd for dd in png_tmp.iterdir()
                                      if dd.is_dir() and list(dd.glob("*.png"))]
                        if subfolders:
                            for dd in subfolders:
                                dest = out_png / dd.name
                                if dest.exists():
                                    shutil.rmtree(dest, ignore_errors=True)
                                shutil.move(str(dd), str(dest))
                        else:
                            for clip, frames in groups.items():
                                clip_dir = out_png / clip
                                clip_dir.mkdir(parents=True, exist_ok=True)
                                for f in frames:
                                    shutil.move(str(f), str(clip_dir / f.name))
                    else:
                        out_avi = out_root / "RLF_AVI"
                        out_avi.mkdir(parents=True, exist_ok=True)
                        for clip, frames in groups.items():
                            folder     = frames[0].parent
                            use_folder = folder != png_tmp
                            padinfo    = detect_zero_pad(frames)
                            out_name   = out_avi / f"{clip}.avi"
                            if use_folder and padinfo:
                                prefix, pad = padinfo
                                pattern = str(folder / f"{prefix}%0{pad}d.png")
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
                                listfile = png_tmp / f"{clip}_list.txt"
                                listfile.write_text(
                                    "".join(f"file '{f.as_posix()}'\n" for f in frames),
                                    encoding="utf-8",
                                )
                                cmd = [str(ffmpeg), "-y", "-r", "15",
                                       "-f", "concat", "-safe", "0", "-i", str(listfile),
                                       "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
                                       str(out_name)]
                            run(cmd, long_msg=f"Muxing RLF {clip} → FFV1 AVI")

                try:
                    shutil.rmtree(png_tmp, ignore_errors=True)
                except Exception:
                    pass

                out_rlf_dir = out_root / ("RLF_PNG" if choice == "png" else
                                          "RLF_MKV" if choice == "mkv" else "RLF_AVI")
                produced = sum(1 for _ in out_rlf_dir.rglob("*") if _.is_file()) if out_rlf_dir.exists() else 0
                stats.record(f"RLF → {choice.upper()}", produced)

        # ── RAW ───────────────────────────────────────────────────────────────
        if 4 in selected_types and raw_script:
            divider("RAW audio")
            stage = tmp_root / "RAW"
            staged, skipped = stage_files(data_roots, TYPES[4][1], stage)
            info(f"Staged {staged} RAW file(s)." +
                 (f" Skipped {skipped} identical duplicate(s)." if skipped else ""))
            if staged:
                mode    = fmt_map.get(4, "nemesis")
                out_wav = out_root / "RAW_WAV"
                out_wav.mkdir(parents=True, exist_ok=True)
                rc = run(
                    [sys.executable, str(raw_script), str(stage / "*.raw"),
                     "--game", mode, "--out", str(out_wav)],
                    long_msg=f"Decoding RAW ({mode}) → WAV",
                )
                produced = sum(1 for _ in out_wav.glob("*.wav"))
                ws = [f"sub-process exited {rc}"] if rc != 0 else []
                stats.record("RAW → WAV", produced, ws)

        # ── TGA ───────────────────────────────────────────────────────────────
        if 5 in selected_types and tga_script:
            divider("TGA images")
            stage = tmp_root / "TGA_STAGE"
            staged, skipped = stage_files(data_roots, TYPES[5][1], stage)
            info(f"Staged {staged} TGA file(s)." +
                 (f" Skipped {skipped} identical duplicate(s)." if skipped else ""))
            if staged:
                tga_env = SCRIPT_DIR / "__tga_env"
                (tga_env / "TGA").mkdir(parents=True, exist_ok=True)
                for p in stage.glob("*.tga"):
                    shutil.copy2(p, tga_env / "TGA" / p.name)
                shutil.copy2(tga_script, tga_env / "unpacker.py")
                qb = which_exe(["quickbms", "quickbms.exe"])
                if qb and qb.exists():
                    shutil.copy2(qb, tga_env / "quickbms.exe")
                else:
                    warn("quickbms.exe not found; TGA decompression may fail.")
                tga_bms = SCRIPT_DIR / "tga.bms"
                if tga_bms.exists():
                    shutil.copy2(tga_bms, tga_env / "tga.bms")
                else:
                    warn("tga.bms not found next to this script; required by unpacker.py.")
                out_bmp   = out_root / "TGA_BMP"
                out_bmp.mkdir(parents=True, exist_ok=True)
                child_env = os.environ.copy()
                child_env["PYTHONIOENCODING"] = "utf-8"
                run([sys.executable, str(tga_env / "unpacker.py")],
                    cwd=tga_env, long_msg="Decompressing TGA images", env=child_env)
                local_bmp = tga_env / "BMP"
                tga_warns: List[str] = []
                moved = 0
                if local_bmp.exists():
                    for p in local_bmp.glob("*.bmp"):
                        shutil.copy2(p, out_bmp / p.name)
                        moved += 1
                    if moved == 0:
                        tga_warns.append("TGA unpacker produced no BMPs")
                    else:
                        try:
                            shutil.rmtree(tga_env, ignore_errors=True)
                        except Exception:
                            pass
                else:
                    tga_warns.append("TGA unpacker did not produce a BMP folder")
                stats.record("TGA → BMP", moved, tga_warns)

        stats.print_summary(out_root)

    finally:
        try:
            shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception:
            pass
        cleanup_stale_temp()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Aborted by user.")
