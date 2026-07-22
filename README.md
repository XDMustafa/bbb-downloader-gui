# BBB Downloader GUI

A desktop app (Persian + English) that downloads BigBlueButton recordings
and converts them to MP4 — no terminal needed after initial setup.

## Quick start

1. Download Python 3.8+ from [python.org](https://www.python.org/downloads/).
   Tick **Add Python to PATH** during install.

2. Click the green **Code** button at the top of this page → **Download ZIP**.
   Unzip anywhere you like.

3. Open a terminal inside the folder:
   - Windows: right‑click → "Open in Terminal" or type `cmd` in the folder's address bar.
   - macOS: right‑click → "New Terminal at Folder".

4. Install dependencies (one‑time):
   ```bash
   pip install -r python-requirements.txt
   ```

5. Launch:
   ```bash
   python main.py
   ```
   Afterwards you can just double‑click `main.py`.

**FFmpeg?** Don't worry — the app has a built‑in **Check / Download FFmpeg**
button that fetches it for you. No manual download, no PATH setup.

---

## Features

- **Single Download** — one URL, straight to video.
- **List Download** — paste multiple URLs, process them all in a batch.
- **Paste from clipboard** — the Paste button grabs the URL directly.
- **Stream checkboxes** — webcam, deskshare, and slides each have a toggle.
- **Save path remembered** — last folder sticks between sessions.
- **Format options**
  - Default `‑c copy` — fast, lossless, a few seconds.
  - **Full Compatibility** — re‑encode (libx264 + AAC) for iOS / older players.
  - **Keep raw files** — leave intermediate `.webm` downloads untouched.
- **Progress label** — live three‑step status at the bottom of the window.
- **Auto light/dark mode** — follows your system style.

---

## Files

```
main.py                 – entry point
bbb_gui.py              – GUI (CustomTkinter)
bbb_core.py             – download logic, URL parsing, ffmpeg merge
ffmpeg_tools.py         – ffmpeg detection / download helper
script/                 – upstream CLI tools (download_bbb_data.py, etc.)
gui-assets/images/      – Banner + theme screenshots
gui-assets/icons/       – app icons (.ico, .icns, .png)
```

---

## Credits

Based on [bbb-downloader](https://github.com/soulgalore/bbb-downloader) by
soulgalore. The GUI wraps the upstream scripts (`download_bbb_data.py`,
`bbb.py` — slightly patched to recognise alternate BBB URL path variants,
`webm_to_mp4.sh`, `integrate_soundtrack.sh`).

Die GUI is built with [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter).

## License

MIT — same as the upstream project.