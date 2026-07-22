"""ffmpeg_tools.py — FFmpeg discovery, download, and invocation helpers.

This module centralises everything related to the FFmpeg binary so the GUI
and the download pipeline can share a single source of truth:

    • detect_ffmpeg(custom_path)  -> bool   # locate ffmpeg on disk or PATH
    • download_ffmpeg_thread(...)           # background installer
    • run_ffmpeg(cmd, log_cb)     -> int    # streaming subprocess runner
    • probe_streams(path)         -> (v,a)  # ffprobe-based stream counter
    • get_ffmpeg_executable()     -> str    # resolved path / "ffmpeg"

The resolved binary path is stored in the module-level FFMPEG_PATH global so
other modules import it directly without re-running detection.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Tuple, Optional, List

# ---------------------------------------------------------------------------
# Constants and globals
# ---------------------------------------------------------------------------

# Directory used to store a bundled FFmpeg if we download one. Sits next to
# this file so PyInstaller / source distributions both work transparently.
BIN_DIR: Path = Path(__file__).resolve().parent / "bin"

# Populated by detect_ffmpeg(). Empty string means "not resolved yet".
FFMPEG_PATH: str = ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_ffmpeg(custom_path: str = "") -> bool:
    """Locate an FFmpeg executable and cache its absolute path in FFMPEG_PATH.

    Resolution order (first hit wins):
      1. An explicit custom path supplied by the user (file or directory).
      2. A binary inside the local ./bin directory (created by download_ffmpeg_thread).
      3. The system PATH (shutil.which).

    Returns True if FFmpeg was found, False otherwise.
    """
    global FFMPEG_PATH
    system = platform.system().lower()

    # Priority 1: explicit user path.
    if custom_path:
        # User supplied a directory -> look for the binary inside it.
        if os.path.isdir(custom_path):
            exe_name = "ffmpeg.exe" if system == "windows" else "ffmpeg"
            full_path = os.path.join(custom_path, exe_name)
            if os.path.exists(full_path):
                FFMPEG_PATH = full_path
                return True
        elif os.path.exists(custom_path):
            # User supplied the exact path to the executable.
            FFMPEG_PATH = custom_path
            return True

    # Priority 2: bundled ./bin/ffmpeg[.exe]
    local_name = "ffmpeg.exe" if system == "windows" else "ffmpeg"
    local_path = BIN_DIR / local_name
    if local_path.exists():
        FFMPEG_PATH = str(local_path)
        return True

    # Priority 3: system PATH (Homebrew, apt, etc.)
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        FFMPEG_PATH = system_ffmpeg
        return True

    return False


def get_ffmpeg_executable() -> str:
    """Return the resolved FFmpeg path, or the bare 'ffmpeg' fallback."""
    if FFMPEG_PATH:
        return FFMPEG_PATH
    if detect_ffmpeg():
        return FFMPEG_PATH
    return "ffmpeg"


def get_ffprobe_executable() -> Optional[str]:
    """Return the resolved ffprobe path next to ffmpeg, or None."""
    ffmpeg = get_ffmpeg_executable()
    if not ffmpeg or ffmpeg == "ffmpeg":
        return shutil.which("ffprobe")
    candidate = ffmpeg.replace("ffmpeg", "ffprobe")
    return candidate if os.path.exists(candidate) else shutil.which("ffprobe")


# ---------------------------------------------------------------------------
# Download (background installer)
# ---------------------------------------------------------------------------

def download_ffmpeg_thread(status_callback: Callable[[str], None],
                           finish_callback: Callable[[bool], None]) -> None:
    """Download and install FFmpeg into BIN_DIR in a background-safe way.

    Designed to be spawned inside a threading.Thread by the GUI. The two
    callbacks let the UI receive progress updates and a final success flag
    without touching the worker thread directly.
    """
    try:
        if not BIN_DIR.exists():
            BIN_DIR.mkdir(parents=True, exist_ok=True)

        system = platform.system().lower()
        status_callback("Detecting OS and retrieving download link...")

        if system == "windows":
            url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
            archive_name = "ffmpeg.zip"
        elif system == "darwin":
            url = "https://evermeet.cx/ffmpeg/getrelease/zip"
            archive_name = "ffmpeg.zip"
        else:  # Linux x86_64
            url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
            archive_name = "ffmpeg.tar.xz"

        archive_path = BIN_DIR / archive_name
        status_callback("Downloading FFmpeg (this may take a few minutes)...")

        def reporthook(blocknum: int, blocksize: int, totalsize: int) -> None:
            readsofar = blocknum * blocksize
            if totalsize > 0:
                percent = min(100, int(readsofar * 100 / totalsize))
                status_callback(f"Downloading FFmpeg... {percent}%")

        urllib.request.urlretrieve(url, archive_path, reporthook)
        status_callback("Download complete. Extracting file...")

        # Flat extraction: pull just the ffmpeg binary out of the archive.
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                for member in zip_ref.namelist():
                    if member.endswith("ffmpeg.exe") or member.endswith("ffmpeg"):
                        filename = os.path.basename(member)
                        with zip_ref.open(member) as source, \
                             open(BIN_DIR / filename, "wb") as target:
                            shutil.copyfileobj(source, target)
                        break
        elif archive_name.endswith(".tar.xz"):
            with tarfile.open(archive_path, "r:xz") as tar_ref:
                for member in tar_ref.getmembers():
                    if member.name.endswith("ffmpeg"):
                        member.name = "ffmpeg"
                        tar_ref.extract(member, path=BIN_DIR)
                        break

        if archive_path.exists():
            os.remove(archive_path)

        if system != "windows":
            ffmpeg_executable = BIN_DIR / "ffmpeg"
            if ffmpeg_executable.exists():
                ffmpeg_executable.chmod(0o755)

        detect_ffmpeg()
        status_callback("FFmpeg successfully installed and ready!")
        finish_callback(True)

    except Exception as e:  # noqa: BLE001 — fail loud but recover
        status_callback(f"Error downloading/installing FFmpeg: {e}")
        finish_callback(False)


# ---------------------------------------------------------------------------
# ffprobe helper
# ---------------------------------------------------------------------------

def probe_streams(path: str, timeout: int = 30) -> Tuple[int, int]:
    """Count the video and audio streams inside a media file via ffprobe.

    Returns (video_count, audio_count). On any failure (ffprobe missing,
    file unreadable, parse error) returns (0, 0) so callers can treat a
    missing probe as "unknown / try anyway" rather than crashing.
    """
    ffprobe = get_ffprobe_executable()
    if not ffprobe or not os.path.exists(ffprobe):
        return (0, 0)

    def _count(stream_selector: str) -> int:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", stream_selector,
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=timeout,
            )
            return len([ln for ln in result.stdout.splitlines() if ln.strip()])
        except Exception:  # noqa: BLE001
            return 0

    return (_count("v"), _count("a"))


# ---------------------------------------------------------------------------
# Streaming subprocess runner
# ---------------------------------------------------------------------------

def run_ffmpeg(cmd: List[str], log_callback: Callable[[str], None],
               timeout: Optional[int] = None) -> int:
    """Run an FFmpeg command, streaming remux/transcode progress to log_callback.

    Only FFmpeg progress lines (containing 'time=' or 'frame=') are forwarded
    so the log box stays readable. Returns the process exit code.
    """
    log_callback(f"Running: {' '.join(cmd)}")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        log_callback("Error: FFmpeg executable not found. "
                     "Use the 'Check / Download FFmpeg' button to install it.")
        return 127

    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            # Filter out noisy ffmpeg chatter — keep progress lines.
            if "time=" in line or "frame=" in line or line.startswith("Error") \
               or line.startswith("Stream mapping") or "Output #" in line:
                log_callback(f"[FFmpeg] {line}")
        process.wait(timeout=timeout)
        return process.returncode
    except subprocess.TimeoutExpired:
        process.kill()
        log_callback("[FFmpeg] Process timed out and was killed.")
        return 124
