"""bbb_core.py — BigBlueButton URL extraction + threaded download pipeline.

This module wires together the upstream bbb-downloader workflow into a single
download thread that the GUI can launch and observe:

    Step 1. download_bbb_data.py  (the original Pedro Augusto repo script)
                • scrapes the BBB presentation page
                • pulls shapes.svg, metadata.xml, slides (PNG/JPG), thumbnails,
                  webcams.webm, deskshare.webm into <output_dir>/Videos/

    Step 2. (always) integrate audio + video into output.webm
                • if deskshare.webm exists  ->  -map 0:v (deskshare) + -map 1:a (webcam)
                • else                       ->  transcode webcam.webm as-is
                This is the critical step that fixes the "white video frame"
                bug: previously only webcams.webm was kept, which holds the
                tiny webcam picture rather than the shared-screen content.

    Step 3. (only when extension == 'mp4') webm_to_mp4 conversion
                • ffmpeg -i output.webm output.mp4

The script directory lives next to this file at ./script/, so the script
subprocess invocations work both in development and when packaged with
PyInstaller via the MEIPASS resource_path fallback.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import ffmpeg_tools
from ffmpeg_tools import (
    FFMPEG_PATH,
    detect_ffmpeg,
    get_ffmpeg_executable,
    probe_streams,
    run_ffmpeg,
)


# ---------------------------------------------------------------------------
# Resource resolution (PyInstaller-aware)
# ---------------------------------------------------------------------------

def resource_path(relative_path: str) -> str:
    """Resolve a path relative to the application bundle.

    Supports PyInstaller frozen builds (sys._MEIPASS) and source layout.
    """
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    base_path = Path(__file__).resolve().parent
    return str(base_path / relative_path)


SCRIPT_DIR = resource_path("script")
DOWNLOAD_BBB_SCRIPT = os.path.join(SCRIPT_DIR, "download_bbb_data.py")


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------

# Three playback URL shapes accepted across BBB deployments:
#   1) https://host/playback/presentation/2.3/playback.html?meetingId=<id>
#   2) https://host/playback/presentation/2.3/<id>
#   3) https://host/playback/presentation/2.3/<id>-<timestamp>      (path variant 3)
MEETING_ID_REGEX = re.compile(
    r"^(?P<website>https?://[^/]+)/playback/presentation/"
    r"(?P<version>[0-9.]+)/(?:playback\.html\?meetingId=)?"
    r"(?P<rid>[0-9a-fA-F]+(?:-[0-9]+)?).*"
)


def parse_bbb_url(url: str) -> tuple[str, str, str]:
    """Return (base_url, meeting_id, record_id) from a BBB playback URL.

    • meeting_id = the bare hexadecimal identifier (no timestamp suffix)
    • record_id  = the full identifier including any '-<timestamp>' suffix

    Raises ValueError when the URL doesn't look like a BBB playback link.
    """
    m = MEETING_ID_REGEX.match(url.strip())
    if not m:
        raise ValueError(
            f"URL does not look like a BBB playback page: {url}\n"
            "Expected something matching "
            "/playback/presentation/<version>/[playback.html?meetingId=]<recordId>"
        )

    website = m.group("website")
    rid = m.group("rid")
    # meeting_id is rid without the numeric timestamp suffix (if any).
    meeting_id = rid.split("-")[0] if "-" in rid else rid
    return website, meeting_id, rid


# ---------------------------------------------------------------------------
# DownloadThread
# ---------------------------------------------------------------------------

class DownloadThread(threading.Thread):
    """Background worker that drives the three-step BBB download pipeline.

    Emits one log_callback per human-readable progress line, and a single
    on_finish_callback with a boolean success flag at the end. The GUI is
    expected to marshal both callbacks back onto the Tk main loop.
    """

    def __init__(self,
                 url: str,
                 output_dir: str,
                 download_videos: bool,
                 download_slides: bool,
                 download_thumbs: bool,
                 format_opt: str,
                 log_callback: Callable[[str], None],
                 on_finish_callback: Callable[[bool], None],
                 full_compat: bool = False,
                 keep_raw: bool = False) -> None:
        """Construct a download task.

        Parameters
        ----------
        full_compat : bool
            True  -> re-encode VP8/Vorbis into H.264/AAC for maximum
                    compatibility with iOS/QuickTime/older players
                    (slow, slight quality loss, file may differ in size).
            False (default) -> container copy only: VP8/Vorbis stream
                    written directly into a .mp4 container — fast, no
                    quality loss, removed re-encode step. Most modern
                    players (VLC, mpv, browsers, MPlayer) handle this fine.
        keep_raw : bool
            True -> keep intermediate raw files (webcams.webm,
                   deskshare.webm, output.webm) after the final mp4 is
                   produced.
            False (default) -> remove those intermediates after success
                   so the output directory only contains the mp4.
        """
        super().__init__()
        self.url = url.strip()
        self.output_dir = output_dir or "./downloads"
        self.download_videos = download_videos
        self.download_slides = download_slides
        self.download_thumbs = download_thumbs
        self.format_opt = (format_opt or "mp4").lower()
        self.log_callback = log_callback
        self.on_finish_callback = on_finish_callback
        self.full_compat = full_compat
        self.keep_raw = keep_raw
        self._is_running = True

    # ---- logging --------------------------------------------------------

    def log(self, message: str) -> None:
        self.log_callback(f"[Downloader] {message}\n")

    # ---- main entry -----------------------------------------------------

    def run(self) -> None:
        try:
            self._validate_inputs()
            base_url, meeting_id, record_id = parse_bbb_url(self.url)
            self.log(f"Parsed BBB URL: base={base_url} meeting_id={meeting_id} record_id={record_id}")

            session_dir = os.path.join(self.output_dir, record_id)
            os.makedirs(session_dir, exist_ok=True)
            self.log(f"Save directory: {session_dir}")

            self._ensure_ffmpeg()

            # Step 1: download raw data via the upstream script.
            self._run_download_script(session_dir)

            # The upstream script writes the videos into ./Videos/ alongside
            # ./Slides/ and ./Thumbnails/ when those flags were passed.
            videos_dir = os.path.join(session_dir, "Videos")
            webcam_webm = os.path.join(videos_dir, "webcams.webm")
            deskshare_webm = os.path.join(videos_dir, "deskshare.webm")

            if self.download_videos:
                self._merge_and_export(videos_dir, webcam_webm, deskshare_webm)

            self.log("Download and processing successfully completed.")
            self.on_finish_callback(True)

        except Exception as e:  # noqa: BLE001 — surface any failure to the GUI
            self.log(f"Error during processing: {e}")
            self.on_finish_callback(False)

    # ---- validation & setup --------------------------------------------

    def _validate_inputs(self) -> None:
        if not self.url:
            raise ValueError("Download link cannot be empty.")
        if not detect_ffmpeg():
            # Don't crash — let the GUI know FFmpeg isn't installed.
            self.log("[FFmpeg] FFmpeg not found. The 'Check / Download FFmpeg' "
                     "button should be used before re-trying the download.")

    def _ensure_ffmpeg(self) -> None:
        # Re-detect in case the user installed FFmpeg after launching the app.
        ffmpeg_tools.detect_ffmpeg()

    # ---- step 1: upstream script --------------------------------------

    def _run_download_script(self, session_dir: str) -> None:
        self.log("Calling download_bbb_data.py (upstream BBB scraper)...")
        if not os.path.exists(DOWNLOAD_BBB_SCRIPT):
            raise FileNotFoundError(
                f"download_bbb_data.py not found at {DOWNLOAD_BBB_SCRIPT}. "
                "Ensure the script/ folder ships with this app."
            )

        cmd = [sys.executable, "-u", DOWNLOAD_BBB_SCRIPT, self.url, session_dir]
        if not self.download_videos and not self.download_slides and not self.download_thumbs:
            # Default: download everything when no flag is set.
            cmd.extend(["-V", "-s", "-t"])
        else:
            if self.download_videos    : cmd.append("-V")
            if self.download_slides    : cmd.append("-s")
            if self.download_thumbs    : cmd.append("-t")
            # Append the requested video format when available.
            cmd.extend(["-f", self.format_opt if self.format_opt in ("mp4", "webm") else "webm"])

        self.log(f"Invoking: {' '.join(shlex.quote(c) for c in cmd)}")
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=SCRIPT_DIR,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"Could not start Python interpreter: {e}")

        for line in process.stdout:
            line = line.rstrip()
            if line:
                self.log(f"[script] {line}")
        process.wait()

        if process.returncode != 0:
            raise RuntimeError(
                f"download_bbb_data.py exited with code {process.returncode}. "
                "See the log above for details."
            )
        self.log("download_bbb_data.py finished successfully.")

    # ---- step 2 & 3: merge and export ----------------------------------

    def _merge_and_export(self, videos_dir: str,
                          webcam_webm: str, deskshare_webm: str) -> None:
        """Combine deskshare video with webcam audio (fixing the white-frame
        bug that produced only webcam thumbnails previously), then export to
        the requested container format.
        """
        ffmpeg_exe = get_ffmpeg_executable()

        # Locate the best video source. deskshare holds the shared-screen /
        # slides content; webcams.webm only holds the tiny webcam thumbnail
        # plus the recorded audio track.
        has_deskshare = \
            deskshare_webm and os.path.exists(deskshare_webm) \
            and os.path.getsize(deskshare_webm) > 256
        has_webcam = \
            webcam_webm and os.path.exists(webcam_webm) \
            and os.path.getsize(webcam_webm) > 256

        if not has_webcam and not has_deskshare:
            self.log("Neither webcams.webm nor deskshare.webm was downloaded.")
            return

        out_ext = self.format_opt
        output_webm = os.path.join(videos_dir, "output.webm")
        output_file = os.path.join(videos_dir, f"output.{out_ext}")

        # Decide on the merge strategy first — produce a unified .webm.
        if has_deskshare and has_webcam:
            # Canonical path: take the deskshare picture and overlay
            # webcam audio (which is where the recorded voice lives).
            self.log("Merging deskshare video with webcam audio track...")
            cmd = [
                ffmpeg_exe, "-y",
                "-i", deskshare_webm,
                "-i", webcam_webm,
                "-map", "0:v",
                "-map", "1:a?",
                "-c:v", "copy",
                "-c:a", "libopus",       # Opus is the native WebM audio codec
                "-shortest",
                output_webm,
            ]
        elif has_deskshare:
            self.log("No webcam audio available — keeping deskshare only.")
            cmd = [
                ffmpeg_exe, "-y",
                "-i", deskshare_webm,
                "-c:v", "copy",
                "-c:a", "copy",
                output_webm,
            ]
        else:
            # Only webcams.webm exists — transcode it as-is. This recording
            # has no shared-screen content, so the output will be the webcam
            # thumbnail + its own embedded audio.
            self.log("Only webcams.webm available — transcoding it directly.")
            shutil.copyfile(webcam_webm, output_webm)
            cmd = None  # Skip step 2.

        if cmd is not None:
            rc = run_ffmpeg(cmd, self.log)
            if rc != 0:
                self.log(f"Warning: integration step returned code {rc}; "
                         "output may be incomplete. Attempting fall-back...")
                # Fallback: use webcam-only transcode.
                if os.path.exists(webcam_webm):
                    shutil.copyfile(webcam_webm, output_webm)

        # Step 3: convert to the requested container format if not webm.
        if out_ext == "webm":
            # Already webm — we're done.
            self.log(f"Final webm output written to {output_webm}")
            self._maybe_cleanup_intermediates(
                final_path=output_webm,
                kept=[output_webm],
                intermediates=[webcam_webm, deskshare_webm],
            )
            return

        # Build the conversion command. By default we use `-c copy` so the
        # VP8/Vorbis streams are just repackaged into an .mp4 container —
        # this is instantaneous and loses zero quality. When the user opts
        # into "Full Compatibility" mode (checkbox in the GUI), we re-encode
        # to H.264 + AAC so the file plays on iOS / QuickTime / older kit
        # where MP4-with-VP8 isn't supported.
        final_ext = out_ext if out_ext in ("mp4", "mkv") else "mp4"
        final_path = os.path.join(videos_dir, f"output.{final_ext}")

        if self.full_compat:
            self.log(f"Transcoding webm -> {final_ext} (Full Compatibility: re-encoding)...")
            video_codec = "libx264"
            audio_codec = "aac"
            extra_args = ["-strict", "experimental"]
        else:
            self.log(f"Remuxing webm -> {final_ext} (fast copy, no re-encode)...")
            video_codec = "copy"
            audio_codec = "copy"
            # VP8 video inside an .mp4 container needs -f mp4 + -movflags
            # +faststart; without -strict experimental some ffmpeg builds
            # reject non-H.264 in .mp4 even with -c copy.
            extra_args = ["-strict", "experimental", "-movflags", "+faststart"]

        convert_cmd = [
            ffmpeg_exe, "-y",
            "-i", output_webm,
            "-c:v", video_codec,
            "-c:a", audio_codec,
            *extra_args,
            final_path,
        ]
        rc = run_ffmpeg(convert_cmd, self.log)
        if rc != 0 or not os.path.exists(final_path):
            self.log(f"Conversion with -c copy returned code {rc}. "
                     "Falling back to full re-encode for compatibility...")
            # Automatic one-shot fallback: some ffmpeg builds are picky
            # about putting VP8 inside .mp4 even with -c copy. Re-encode
            # with H.264/AAC guarantees a universally playable file.
            convert_cmd = [
                ffmpeg_exe, "-y",
                "-i", output_webm,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-strict", "experimental",
                final_path,
            ]
            rc = run_ffmpeg(convert_cmd, self.log)
            if rc != 0 or not os.path.exists(final_path):
                self.log(f"Conversion step returned code {rc}; "
                         "the intermediate output.webm may still be usable.")
                return

        size_mb = os.path.getsize(final_path) / (1024 * 1024)
        self.log(f"Final {final_ext.upper()} output ready: "
                 f"{final_path} ({size_mb:.2f} MB)")

        # Probe the final file for a friendly summary.
        v, a = probe_streams(final_path)
        self.log(f"Final file stream summary: {v} video stream(s), {a} audio stream(s).")

        # Cleanup intermediate raw files unless the user asked to keep them.
        self._maybe_cleanup_intermediates(
            final_path=final_path,
            kept=[final_path],
            intermediates=[webcam_webm, deskshare_webm, output_webm],
        )

    def _maybe_cleanup_intermediates(self, final_path: str,
                                      kept: list[str],
                                      intermediates: list[str]) -> None:
        """Delete the intermediate .webm files produced during merge, unless
        the user asked to keep them. Only succeeds are cleaned up — if the
        final file is missing or smaller than 1 KB we leave the raw files
        alone so the user can recover something.
        """
        if self.keep_raw:
            self.log("Skipping cleanup — 'keep raw files' is enabled.")
            return

        if not os.path.exists(final_path) or os.path.getsize(final_path) < 1024:
            self.log("Final file is missing or too small — keeping "
                     "intermediate .webm files as a safety net.")
            return

        removed = []
        for path in intermediates:
            # Defensive paranoia: never delete files that are in `kept`.
            if path in kept:
                continue
            try:
                if path and os.path.exists(path):
                    sz = os.path.getsize(path)
                    os.remove(path)
                    removed.append(f"{os.path.basename(path)} ({sz / (1024 * 1024):.2f} MB)")
            except OSError as e:
                self.log(f"Could not remove intermediate file {path}: {e}")
        if removed:
            self.log("Cleanup: removed intermediate raw files — " + ", ".join(removed))
        else:
            self.log("Cleanup: no intermediate raw files to remove.")


# ---------------------------------------------------------------------------
# Convenience: shared batch runner for the List tab
# ---------------------------------------------------------------------------

def run_batch(links: list[str],
              output_dir: str,
              download_videos: bool,
              download_slides: bool,
              download_thumbs: bool,
              format_opt: str,
              log_callback: Callable[[str], None],
              full_compat: bool = False,
              keep_raw: bool = False,
              should_continue: Callable[[], bool] = lambda: True) -> None:
    """Sequentially download each link in `links`. Skips further links if
    `should_continue()` returns False (used to let the GUI abort a batch).
    """
    total = len(links)
    for index, url in enumerate(links, 1):
        if not should_continue():
            log_callback(f"[Batch] Aborted by user before link {index}/{total}.\n")
            return
        log_callback(f"\n[Batch {index}/{total}] Downloading: {url}\n")

        done_event = threading.Event()
        success_status = [False]

        def _finish(success: bool) -> None:
            success_status[0] = success
            done_event.set()

        dt = DownloadThread(
            url=url,
            output_dir=output_dir,
            download_videos=download_videos,
            download_slides=download_slides,
            download_thumbs=download_thumbs,
            format_opt=format_opt,
            log_callback=log_callback,
            on_finish_callback=_finish,
            full_compat=full_compat,
            keep_raw=keep_raw,
        )
        dt.daemon = True
        dt.start()
        done_event.wait()

        if success_status[0]:
            log_callback(f"[Batch {index}/{total}] Success.\n")
        else:
            log_callback(f"[Batch {index}/{total}] Failed — see log above.\n")

    log_callback("\n[Batch] All links processed.\n")
