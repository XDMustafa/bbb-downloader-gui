"""bbb_gui.py — CustomTkinter front-end for the BBB Downloader.

This module owns only the user interface (CTk windows, tabs, widgets,
clipboard/file dialogs) and delegates all work to:

    • bbb_core     — BBB URL parsing + threaded download pipeline
    • ffmpeg_tools — FFmpeg discovery, download, ffprobe, run helpers

UI layout follows Information.md (the project specification):

  Top bar:
    "BBB Downloader" title on the left, theme switcher on the right.

  Bottom bar (outside the tabs):
    FFmpeg path entry + Check/Download FFmpeg button.

  Single Download tab:
    1. Location to save Video  (entry + Save as button, remembers last dir)
    2. Get link and Download    (link entry + Paste button + Download button,
                                 checkboxes (Videos / Slides / Thumbnails)
                                 placed just below in a compact row, Format menu)
    3. Get the Video            (status label cycling through three steps)
    Output Logs                (text box + Copy button)

  List Download tab:
    Identical to Single Download, but the link area is a tall textbox that
    accepts one link per line, and replaces the Paste button row with a
    right-aligned row holding Paste / Import txt / Download buttons.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

# Third-party clipboard helper (optional — imported lazily).
try:
    import pyperclip  # type: ignore
except Exception:  # pragma: no cover — pyperclip is optional
    pyperclip = None

# Local modules — keep these sibling imports simple so PyInstaller bundling
# works the same as source layout.
import bbb_core
import ffmpeg_tools
from ffmpeg_tools import detect_ffmpeg, download_ffmpeg_thread


# ---------------------------------------------------------------------------
# Persistence helpers (last-used folder)
# ---------------------------------------------------------------------------

CONFIG_PATH: Path = Path.home() / ".bbb_downloader_gui.json"


def load_last_folder() -> str:
    """Return the last-used save folder, or '' if none is recorded."""
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            folder = data.get("last_save_folder", "")
            if folder and os.path.isdir(folder):
                return folder
    except Exception:
        pass
    return ""


def save_last_folder(folder: str) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps({"last_save_folder": folder}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # Best-effort — never crash on settings.


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    """The top-level BBB Downloader GUI window."""

    # -------- construction ------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self.title("BBB Downloader")
        self.geometry("800x720")
        ctk.set_appearance_mode("Dark")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)   # tabs expand
        self.grid_rowconfigure(2, weight=0)  # ffmpeg row fixed

        self._last_folder: str = load_last_folder()

        self._build_top_bar()
        self._build_tabs()
        self._build_bottom_bar()

        # NOTE: we intentionally do NOT auto-check FFmpeg on startup — the
        # user clicks the "Check / Download FFmpeg" button when they want.

    # -------- top bar -----------------------------------------------------

    def _build_top_bar(self) -> None:
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.grid(row=0, column=0, padx=20, pady=(10, 0), sticky="ew")
        top_frame.grid_columnconfigure(0, weight=1)
        top_frame.grid_columnconfigure(1, weight=0)

        title = ctk.CTkLabel(
            top_frame, text="BBB Downloader",
            text_color="#A07AFF",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        theme_menu = ctk.CTkOptionMenu(
            top_frame,
            values=["Dark", "Light"],
            button_color=("#A07AFF", "#1F1A38"),
            fg_color=("#A07AFF", "#1F1A38"),
            text_color=("black", "white"),
            command=self.change_theme,
        )
        theme_menu.grid(row=0, column=1, sticky="e")

    # -------- tabs --------------------------------------------------------

    def _build_tabs(self) -> None:
        self.tab_view = ctk.CTkTabview(
            self, fg_color="transparent",
            segmented_button_selected_color="#A07AFF",
            segmented_button_selected_hover_color="#644D9D",
            text_color="#FFFFFF",
        )
        self.tab_view.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.tab_view.add("Single Download")
        self.tab_view.add("List Download")
        self._build_single_tab(self.tab_view.tab("Single Download"))
        self._build_list_tab(self.tab_view.tab("List Download"))

    # -------- bottom bar (FFmpeg) ----------------------------------------

    def _build_bottom_bar(self) -> None:
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=2, column=0, padx=20, pady=(0, 20), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)

        self.ffmpeg_path_entry = ctk.CTkEntry(
            bottom_frame,
            placeholder_text="Custom FFmpeg path (leave empty for auto-detect)",
            height=32,
        )
        self.ffmpeg_path_entry.grid(row=0, column=0, padx=(0, 10), sticky="ew")

        self.ffmpeg_btn = ctk.CTkButton(
            bottom_frame,
            text="Check / Download FFmpeg",
            text_color="#000000",          # black text in both themes & states
            fg_color=("#EBEBEB", "#EBEBEB"),   # off-white solid in both themes
            hover_color=("#D6D6D6", "#D6D6D6"), # slightly darker on hover
            border_width=0,
            command=self.check_or_download_ffmpeg,
            height=32,
        )
        self.ffmpeg_btn.grid(row=0, column=1, padx=0, pady=0)

    # -------- Single Download tab ----------------------------------------

    def _build_single_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(8, weight=1)  # log area expands vertically

        # 1. Location to save Video --------------------------------------
        self._build_location_row(
            tab, label_text="1. Location to save Video",
            row_label=0, row_widget=1,
            entry_attr="location_entry_single",
            button_command=lambda: self.select_save_location(self.location_entry_single),
        )

        # 2. Get link and Download ---------------------------------------
        link_label = ctk.CTkLabel(
            tab, text="2. Get link and Download",
            font=ctk.CTkFont(weight="bold"),
        )
        link_label.grid(row=2, column=0, padx=10, pady=(20, 5), sticky="w")

        link_frame = ctk.CTkFrame(tab, fg_color="transparent")
        link_frame.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        link_frame.grid_columnconfigure(0, weight=1)

        self.link_entry_single = ctk.CTkEntry(
            link_frame, placeholder_text="Paste link address",
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#A0A0A0", "#3E3E3E"), border_width=1,
        )
        self.link_entry_single.grid(row=0, column=0, sticky="ew")

        self.paste_button_single = ctk.CTkButton(
            link_frame, text="Paste", width=100,
            text_color=("black", "#A07AFF"),
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#B9B9B9", "#FFFFFF"),
            hover_color=("#DDDDDD", "#222222"),
            command=lambda: self.paste_from_clipboard(
                self.link_entry_single, self.paste_button_single),
        )
        self.paste_button_single.grid(row=0, column=1, padx=(10, 5))

        self.download_button = ctk.CTkButton(
            link_frame, text="Download", width=100,
            text_color="black",
            fg_color="#A07AFF", hover_color="#644D9D",
            command=self.start_download_single,
        )
        self.download_button.grid(row=0, column=2, padx=(0, 0))

        # Options row: checkboxes + format dropdown (compact, packed left)
        self._build_options_row(
            tab, row=4,
            video_var_attr="download_videos_var_1",
            slides_var_attr="download_slides_var_1",
            thumbs_var_attr="download_thumbs_var_1",
            format_var_attr="format_var_1",
            optmenu_attr="opt_format_1",
        )

        # 3. Get the Video (status banner) -------------------------------
        video_label = ctk.CTkLabel(
            tab, text="3. Get the Video",
            font=ctk.CTkFont(weight="bold"),
        )
        video_label.grid(row=5, column=0, padx=10, pady=(20, 5), sticky="nw")

        video_placeholder = ctk.CTkFrame(tab, height=80, border_width=1)
        video_placeholder.grid(row=6, column=0, padx=10, pady=5, sticky="ew")
        # Lock the height so resizing the window vertically never collapses
        # or grows this banner — only its width follows the tab frame.
        video_placeholder.grid_propagate(False)

        self.single_status_label = ctk.CTkLabel(
            video_placeholder, text="Waiting to start...",
            cursor="arrow", text_color="gray",
        )
        self.single_status_label.place(relx=0.5, rely=0.5, anchor="center")
        # Clicking the final "File ready as …" text opens the save folder.
        self.single_status_label.bind(
            "<Button-1>", lambda _e: self.open_folder(self._last_video_folder_single))

        # Output Logs -----------------------------------------------------
        self._build_logs_row(
            tab, label_text="Output Logs",
            logs_label_row=7, logbox_row=8,
            logbox_attr="log_box_single",
            initial_text="",
        )

        # Variables used during the 3-step status banner cycle.
        self._last_video_folder_single: Optional[str] = None
        self._single_download_step: float = 0.0  # 0..1 progress numeric state

    # -------- List Download tab ------------------------------------------

    def _build_list_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        # NOTE: we deliberately configure only the log row (row 10) with
        # weight=1 — the link textbox (row 3) and the Get-the-Video banner
        # (row 8) stay at their fixed pixel heights so changing the window
        # height only stretches the log area and never distorts section 2
        # or hides section 3 the way the bug report described.
        tab.grid_rowconfigure(10, weight=1)

        # 1. Location
        self._build_location_row(
            tab, label_text="1. Location to save Videos",
            row_label=0, row_widget=1,
            entry_attr="location_entry_list",
            button_command=lambda: self.select_save_location(self.location_entry_list),
        )

        # 2. Get links and Download
        list_label = ctk.CTkLabel(
            tab, text="2. Get links and Download",
            font=ctk.CTkFont(weight="bold"),
        )
        list_label.grid(row=2, column=0, padx=10, pady=(20, 5), sticky="w")

        self.links_box_list = ctk.CTkTextbox(
            tab, height=220,
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#A0A0A0", "#3E3E3E"), border_width=1,
        )
        self.links_box_list.grid(row=3, column=0, padx=10, pady=5, sticky="new")

        buttons_frame = ctk.CTkFrame(tab, fg_color="transparent")
        buttons_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        buttons_frame.grid_columnconfigure(0, weight=1)

        right_buttons = ctk.CTkFrame(buttons_frame, fg_color="transparent")
        right_buttons.grid(row=0, column=0, sticky="e")

        self.list_paste_button = ctk.CTkButton(
            right_buttons, text="Paste", width=100,
            text_color=("black", "#A07AFF"),
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#B9B9B9", "#FFFFFF"),
            hover_color=("#DDDDDD", "#222222"),
            command=lambda: self.paste_from_clipboard(
                self.links_box_list, self.list_paste_button),
        )
        self.list_paste_button.pack(side="left", padx=(0, 5))

        self.list_import_button = ctk.CTkButton(
            right_buttons, text="Import txt", width=100,
            text_color=("black", "#A07AFF"),
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#B9B9B9", "#FFFFFF"),
            hover_color=("#DDDDDD", "#222222"),
            command=lambda: self.import_text_file(self.links_box_list),
        )
        self.list_import_button.pack(side="left", padx=(0, 5))

        self.list_download_button = ctk.CTkButton(
            right_buttons, text="Download", width=100,
            text_color="black",
            fg_color="#A07AFF", hover_color="#644D9D",
            command=self.start_download_list,
        )
        self.list_download_button.pack(side="left", padx=(0, 0))

        # Options row
        self._build_options_row(
            tab, row=4,  # NOTE: intentionally stays on row 4 below the textbox
            video_var_attr="download_videos_var_2",
            slides_var_attr="download_slides_var_2",
            thumbs_var_attr="download_thumbs_var_2",
            format_var_attr="format_var_2",
            optmenu_attr="opt_format_2",
            shift_down_rows=2,  # Push subsequent rows down to avoid clashes.
        )

        # 3. Get the Video (status banner)
        video_label = ctk.CTkLabel(
            tab, text="3. Get the Video",
            font=ctk.CTkFont(weight="bold"),
        )
        video_label.grid(row=7, column=0, padx=10, pady=(20, 5), sticky="nw")

        video_placeholder = ctk.CTkFrame(tab, height=80, border_width=1)
        video_placeholder.grid(row=8, column=0, padx=10, pady=5, sticky="ew")
        # Lock the height so resizing the window vertically never collapses
        # or grows this banner — only its width follows the tab frame.
        video_placeholder.grid_propagate(False)

        self.list_status_label = ctk.CTkLabel(
            video_placeholder, text="Waiting to start...",
            cursor="arrow", text_color="gray",
        )
        self.list_status_label.place(relx=0.5, rely=0.5, anchor="center")
        self.list_status_label.bind(
            "<Button-1>", lambda _e: self.open_folder(self._last_video_folder_list))

        # Output Logs
        self._build_logs_row(
            tab, label_text="Logs",
            logs_label_row=9, logbox_row=10,
            logbox_attr="log_box_list",
            initial_text="",
        )

        self._last_video_folder_list: Optional[str] = None

    # -------- shared sub-builders ----------------------------------------

    def _build_location_row(self, parent: ctk.CTkFrame, *, label_text: str,
                            row_label: int, row_widget: int,
                            entry_attr: str, button_command) -> None:
        label = ctk.CTkLabel(
            parent, text=label_text, font=ctk.CTkFont(weight="bold"))
        label.grid(row=row_label, column=0, padx=10, pady=(10, 5), sticky="w")

        location_frame = ctk.CTkFrame(parent, fg_color="transparent")
        location_frame.grid(row=row_widget, column=0, padx=10, pady=5, sticky="ew")
        location_frame.grid_columnconfigure(0, weight=1)

        entry = ctk.CTkEntry(
            location_frame, placeholder_text="Location folder",
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#A0A0A0", "#3E3E3E"), border_width=1,
        )
        entry.grid(row=0, column=0, sticky="ew")
        # Seed the entry with the last-used folder, if any.
        if self._last_folder:
            entry.insert(0, self._last_folder)
        setattr(self, entry_attr, entry)

        save_as_button = ctk.CTkButton(
            location_frame, text="Save as", width=100,
            text_color=("black", "#A07AFF"),
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#B9B9B9", "#FFFFFF"),
            hover_color=("#DDDDDD", "#222222"),
            command=button_command,
        )
        save_as_button.grid(row=0, column=1, padx=(10, 0))

    def _build_options_row(self, parent: ctk.CTkFrame, *, row: int,
                           video_var_attr: str, slides_var_attr: str,
                           thumbs_var_attr: str, format_var_attr: str,
                           optmenu_attr: str,
                           compat_var_attr: Optional[str] = None,
                           keep_raw_var_attr: Optional[str] = None,
                           shift_down_rows: int = 0) -> None:
        """Compact row of three checkboxes (Videos / Slides / Thumbnails) +
        a Format dropdown, plus optional "Full Compatibility" and
        "Keep raw files" checkboxes. All packed horizontally with tight
        spacing.

        When `compat_var_attr` / `keep_raw_var_attr` are provided, extra
        CTkBooleanVars are created and stored on `self` under those names
        so the download triggers can pick them up.
        """
        offset = shift_down_rows
        row = row + offset

        options_frame = ctk.CTkFrame(parent, fg_color="transparent")
        options_frame.grid(row=row, column=0, padx=20, pady=(10, 5), sticky="ew")
        # We grow the last column so everything hugs the left edge.
        # Column layout:
        #   0 Videos       1 Slides      2 Thumbs
        #   3 spacer (grows)
        #   4 Full Compat  5 Keep raw    6 Format:    7 format dropdown
        options_frame.grid_columnconfigure(3, weight=1)

        video_var = ctk.BooleanVar(value=True)
        slides_var = ctk.BooleanVar(value=False)
        thumbs_var = ctk.BooleanVar(value=False)
        format_var = ctk.StringVar(value="MP4")
        setattr(self, video_var_attr, video_var)
        setattr(self, slides_var_attr, slides_var)
        setattr(self, thumbs_var_attr, thumbs_var)
        setattr(self, format_var_attr, format_var)

        chk_videos = ctk.CTkCheckBox(
            options_frame, text="Videos", variable=video_var,
            fg_color="#A07AFF", hover_color="#644D9D",
            text_color=("black", "white"),
        )
        chk_videos.grid(row=0, column=0, padx=(0, 10), pady=5, sticky="w")

        chk_slides = ctk.CTkCheckBox(
            options_frame, text="Slides", variable=slides_var,
            fg_color="#A07AFF", hover_color="#644D9D",
            text_color=("black", "white"),
        )
        chk_slides.grid(row=0, column=1, padx=(0, 10), pady=5, sticky="w")

        chk_thumbs = ctk.CTkCheckBox(
            options_frame, text="Thumbnails", variable=thumbs_var,
            fg_color="#A07AFF", hover_color="#644D9D",
            text_color=("black", "white"),
        )
        chk_thumbs.grid(row=0, column=2, padx=(0, 10), pady=5, sticky="w")

        # Optional checkboxes placed in the right-hand cluster.
        if compat_var_attr:
            compat_var = ctk.BooleanVar(value=False)
            setattr(self, compat_var_attr, compat_var)
            chk_compat = ctk.CTkCheckBox(
                options_frame,
                text="Full Compatibility (re-encode)",
                variable=compat_var,
                fg_color="#A07AFF", hover_color="#644D9D",
                text_color=("black", "white"),
            )
            chk_compat.grid(row=0, column=4, padx=(20, 10), pady=5, sticky="e")

        if keep_raw_var_attr:
            keep_raw_var = ctk.BooleanVar(value=False)
            setattr(self, keep_raw_var_attr, keep_raw_var)
            chk_keep = ctk.CTkCheckBox(
                options_frame,
                text="Keep raw files",
                variable=keep_raw_var,
                fg_color="#A07AFF", hover_color="#644D9D",
                text_color=("black", "white"),
            )
            chk_keep.grid(row=0, column=5, padx=(0, 10), pady=5, sticky="e")

        format_label = ctk.CTkLabel(
            options_frame, text="Format:",
            text_color=("black", "white"),
        )
        format_label.grid(row=0, column=6, padx=(5, 5), pady=5, sticky="e")

        opt_menu = ctk.CTkOptionMenu(
            options_frame, values=["MP4", "WebM", "MP3"],
            variable=format_var,
            button_color="#A07AFF", button_hover_color="#644D9D",
            fg_color=("#FFFFFF", "#1F1A38"),
            text_color=("black", "white"), width=90,
        )
        opt_menu.grid(row=0, column=7, padx=(0, 0), pady=5, sticky="e")
        setattr(self, optmenu_attr, opt_menu)

    def _build_logs_row(self, parent: ctk.CTkFrame, *, label_text: str,
                        logs_label_row: int, logbox_row: int,
                        logbox_attr: str, initial_text: str = "") -> None:
        logs_frame = ctk.CTkFrame(parent, fg_color="transparent")
        logs_frame.grid(row=logs_label_row, column=0, padx=10, pady=(20, 5), sticky="ew")

        logs_label = ctk.CTkLabel(
            logs_frame, text=label_text, font=ctk.CTkFont(weight="bold"))
        logs_label.pack(side="left")

        log_box = ctk.CTkTextbox(
            parent,
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#A0A0A0", "#3E3E3E"), border_width=1,
        )
        log_box.grid(row=logbox_row, column=0, padx=10, pady=(0, 10), sticky="nsew")
        if initial_text:
            log_box.insert("1.0", initial_text)
        setattr(self, logbox_attr, log_box)

        copy_button = ctk.CTkButton(
            logs_frame, text="Copy", width=80,
            text_color=("black", "white"),
            fg_color=("#FFFFFF", "#1B1B1B"),
            border_color=("#B9B9B9", "#FFFFFF"),
            hover_color=("#DDDDDD", "#222222"),
            command=lambda: self.copy_to_clipboard(getattr(self, logbox_attr)),
        )
        copy_button.pack(side="right")

    # -------- theme ------------------------------------------------------

    def change_theme(self, new_theme: str) -> None:
        ctk.set_appearance_mode(new_theme)

    # -------- FFmpeg management ------------------------------------------

    def check_or_download_ffmpeg(self) -> None:
        custom_path = self.ffmpeg_path_entry.get().strip()
        if detect_ffmpeg(custom_path):
            self.write_to_log(f"[FFmpeg] FFmpeg found at: {ffmpeg_tools.FFMPEG_PATH}\n")
            self.update_ffmpeg_button_status(True)
            return

        self.write_to_log("[FFmpeg] FFmpeg not found. Starting download...\n")
        self.update_ffmpeg_button_status(False)
        self.ffmpeg_btn.configure(
            state="disabled", text="Downloading...",
            text_color="#000000",
            fg_color=("#EBEBEB", "#EBEBEB"),
            hover_color=("#D6D6D6", "#D6D6D6"),
        )

        def status_cb(msg: str) -> None:
            # Filter percentage-flush loops for log readability.
            if "Downloading FFmpeg..." not in msg:
                self.write_to_log(f"[FFmpeg] {msg}\n")

        def finish_cb(success: bool) -> None:
            self.update_ffmpeg_button_status(success)
            # Always restore the off-white/black palette so the button keeps
            # the same look whether the install succeeded or failed.
            self.ffmpeg_btn.configure(
                state="normal",
                text=("FFmpeg Installed" if success else "Retry Download FFmpeg"),
                fg_color=("#EBEBEB", "#EBEBEB"),
                hover_color=("#D6D6D6", "#D6D6D6"),
                text_color="#000000",
            )

        threading.Thread(
            target=download_ffmpeg_thread,
            args=(status_cb, finish_cb),
            daemon=True,
        ).start()

    def update_ffmpeg_button_status(self, installed: bool) -> None:
        if not hasattr(self, "ffmpeg_btn") or not self.ffmpeg_btn.winfo_exists():
            return
        # We keep the button's off-white/black palette stable on purpose
        # (per spec); only the label changes to reflect status.
        if installed:
            self.ffmpeg_btn.configure(text="FFmpeg Installed")
        else:
            self.ffmpeg_btn.configure(text="Check / Download FFmpeg")

    # -------- clipboard / file dialogs -----------------------------------

    def select_save_location(self, entry_widget: ctk.CTkEntry) -> None:
        # Start the dialog in the previously-used folder if available.
        initial_dir = self._last_folder if self._last_folder else None
        folder_path = filedialog.askdirectory(initialdir=initial_dir)
        if folder_path:
            entry_widget.delete(0, "end")
            entry_widget.insert(0, folder_path)
            self._last_folder = folder_path
            save_last_folder(folder_path)

    def paste_from_clipboard(self, widget, button: Optional[ctk.CTkButton] = None) -> None:
        """Insert clipboard contents into `widget`.

        When `button` is provided (the Paste button that was clicked), the
        label briefly flips to 'Pasted' for 0.5 s as visual feedback, exactly
        as described in Information.md.

        Robust against the crash reported when clicking the Paste button:
        any failure in the clipboard read or the widget insertion is caught
        and reported through the log box, never propagated to Tk's main loop
        (which on macOS can terminate the process silently if it gets an
        unhandled exception out of a button command).
        """
        # Read the clipboard — wrap this in its own defensive layer so we
        # can fall back to Tk's clipboard if pyperclip is unavailable or
        # misbehaves (pyperclip shells out to `pbpaste`/`xclip`/`win32clip`,
        # and that subprocess can raise in some headless / sandboxed setups).
        try:
            clipboard_content = ""
            if pyperclip is not None:
                clipboard_content = pyperclip.paste()
            elif hasattr(self, "clipboard_get"):
                clipboard_content = self.clipboard_get()
        except Exception as e:
            # Last-resort: try Tk's built-in clipboard accessor.
            try:
                clipboard_content = self.clipboard_get()
            except Exception as e2:
                self.write_to_log(f"[Clipboard] Could not paste: {e} / {e2}\n")
                return

        if not clipboard_content:
            return

        # The actual insert must also be defensively wrapped. Some widgets
        # in "disabled" state raise on insert(); we temporarily enable them,
        # do the insert, then restore the previous state.
        try:
            if isinstance(widget, ctk.CTkEntry):
                widget.delete(0, "end")
                widget.insert(0, clipboard_content)
            elif isinstance(widget, ctk.CTkTextbox):
                current_text = widget.get("1.0", "end-1c")
                if current_text.strip():
                    widget.insert("end", "\n" + clipboard_content)
                else:
                    widget.insert("end", clipboard_content)
        except Exception as e:
            self.write_to_log(f"[Clipboard] Insertion failed: {e}\n")
            return

        if button is not None:
            try:
                original_text = button.cget("text")
                button.configure(text="Pasted!")
                self.after(500, lambda: button.configure(text=original_text))
            except Exception:
                # Non-critical — ignore the visual-feedback failure.
                pass

    def copy_to_clipboard(self, textbox: ctk.CTkTextbox) -> None:
        try:
            log_content = textbox.get("1.0", "end-1c")
            if not log_content:
                return
            if pyperclip is not None:
                pyperclip.copy(log_content)
            elif hasattr(self, "clipboard_clear"):
                self.clipboard_clear()
                self.clipboard_append(log_content)
        except Exception as e:
            self.write_to_log(f"[Clipboard] Could not copy: {e}\n")

    def import_text_file(self, textbox: ctk.CTkTextbox) -> None:
        file_path = filedialog.askopenfilename(
            title="Select text file", filetypes=[("Text files", "*.txt")])
        if not file_path:
            return
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.write_to_log(f"[Import] Could not read file: {e}\n")
            return
        current_text = textbox.get("1.0", "end-1c")
        if current_text.strip():
            textbox.insert("end", "\n" + content)
        else:
            textbox.insert("end", content)

    # -------- logging -----------------------------------------------------

    def write_to_log(self, message: str, *, single: bool = True, list_: bool = True) -> None:
        """Append text to one or both log boxes in a thread-safe way.

        After appending, the message is handed to the status-label inspector
        so the 3-step banner ("Downloading / Preparing / File is ready") can
        transition in real time as the streamed log lines arrive, rather
        than only changing once the whole pipeline finishes.
        """
        def _append(box: ctk.CTkTextbox, text: str) -> None:
            box.configure(state="normal")
            box.insert("end", text)
            box.see("end")
            box.configure(state="disabled")

        if single and hasattr(self, "log_box_single") and self.log_box_single.winfo_exists():
            self.after(0, lambda b=self.log_box_single, m=message: _append(b, m))
        if list_ and hasattr(self, "log_box_list") and self.log_box_list.winfo_exists():
            self.after(0, lambda b=self.log_box_list, m=message: _append(b, m))

        # Drive the 3-step status banner off the log stream itself.
        self.after(0, lambda m=message: self._inspect_log_for_status(m))

    def _inspect_log_for_status(self, message: str) -> None:
        """Update single/list status labels as the download pipeline advances.

        Triggered on every log line so the banner shows real progress
        instead of freezing on step (1/3) the whole time.

        Step markers we recognise (matching bbb_core.py output):
          • Step 1 begin : "Calling download_bbb_data.py"
          • Step 1 done   : "download_bbb_data.py finished successfully"
          • Step 2 begin  : "Merging" or "Transcoding" or "Only webcams"
          • Step 2 done   : "Final <fmt> output ready:"
          • Step 3 ready  : "[Success]"  (sent from on_download_single_finished)
        """
        # Don't double-process success/failure lines — they only close out
        # the banner at the end and would otherwise overwrite a Ready banner.
        if "[Success]" in message or "[Error]" in message:
            return

        # Single-download banner
        if hasattr(self, "single_status_label"):
            if "Calling download_bbb_data.py" in message:
                self._set_status_label(
                    self.single_status_label,
                    text="(1/3) Downloading…",
                    clickable=False,
                )
            elif "download_bbb_data.py finished successfully" in message:
                # Move to "preparing" step only when the merge phase starts,
                # to match the visual sequence described in Information.md.
                pass
            elif ("Merging" in message or "Transcoding" in message
                  or "Only webcams" in message or "Keeping deskshare" in message):
                self._set_status_label(
                    self.single_status_label,
                    text="(2/3) Preparing File…",
                    clickable=False,
                )
            elif "Final" in message and "output ready" in message:
                # The core module reports the final path; show a short
                # clickable "here" link instead of the full path.
                self._last_video_folder_single = self._extract_folder_from_log(message)
                self._set_status_label(
                    self.single_status_label,
                    text="File is ready here",
                    color="#A07AFF", clickable=True, underline_only_link=True,
                )

        # Same transitions for the list-download banner (a batch run shares
        # the log stream with the per-link worker).
        if hasattr(self, "list_status_label"):
            if "Calling download_bbb_data.py" in message:
                self._set_status_label(
                    self.list_status_label,
                    text="(1/3) Downloading…",
                    clickable=False,
                )
            elif ("Merging" in message or "Transcoding" in message
                  or "Only webcams" in message or "Keeping deskshare" in message):
                self._set_status_label(
                    self.list_status_label,
                    text="(2/3) Preparing File…",
                    clickable=False,
                )
            elif "Final" in message and "output ready" in message:
                self._last_video_folder_list = self._extract_folder_from_log(message)
                self._set_status_label(
                    self.list_status_label,
                    text="File is ready here",
                    color="#A07AFF", clickable=True, underline_only_link=True,
                )

    def _extract_folder_from_log(self, message: str) -> Optional[str]:
        """Pull the parent directory of the output file out of a log line
        produced by bbb_core:
            "Final MP4 output ready: /path/to/Videos/output.mp4 (xx.xx MB)"
        Returns the containing folder (e.g. /path/to/Videos) or None.
        """
        if ":" not in message:
            return None
        # Take the part after the second colon (after "Final MP4 output ready:")
        try:
            after_marker = message.split("output ready:", 1)[1].strip()
            path_part = after_marker.split("(", 1)[0].strip()
            if path_part and os.path.exists(path_part):
                return os.path.dirname(os.path.abspath(path_part))
        except Exception:
            return None
        return None

    # -------- folder opener ----------------------------------------------

    def open_folder(self, path: Optional[str]) -> None:
        if not path or not os.path.exists(path):
            return
        platform_name = platform.system()
        if platform_name == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform_name == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # -------- status banner helpers --------------------------------------

    def _set_status_label(self, label: ctk.CTkLabel, *,
                          text: str, color: str = "#A07AFF",
                          underline: bool = False, clickable: bool = False,
                          underline_only_link: bool = False) -> None:
        """Update the status banner label.

        When `underline_only_link=True`, the text "here" inside `text` will
        be rendered as a separate underlined link, while the rest of the
        text stays plain. This is achieved by swapping the label for a
        two-widget frame built ad-hoc.

        Plain mode (clickable or not) keeps the whole string in one label.
        """
        # Detect parent (video_placeholder CTkFrame that hosts the label).
        parent = label.master

        if underline_only_link:
            # We render "File is ready here" as: "File is ready " (plain) +
            # "here" (underlined, colored, clickable).
            plain_part = text.replace("here", "").rstrip() + " "
            link_part = "here"

            plain_color = ("black" if ctk.get_appearance_mode() == "Light" else "white")

            # Reuse a single frame to hold the two labels. We cache it on
            # the parent so repeated calls don't leak widgets.
            frame = getattr(parent, "_status_link_frame", None)
            if frame is None or not frame.winfo_exists():
                # Hide the old plain label.
                label.place_forget()
                frame = ctk.CTkFrame(parent, fg_color="transparent")
                frame.place(relx=0.5, rely=0.5, anchor="center")
                parent._status_link_frame = frame
                parent._plain_link_label = ctk.CTkLabel(
                    frame, text=plain_part, text_color=plain_color)
                parent._plain_link_label.pack(side="left", padx=0)
                parent._link_label = ctk.CTkLabel(
                    frame, text=link_part, text_color=color,
                    font=ctk.CTkFont(family="Arial", size=12, underline=True),
                    cursor="hand2")
                parent._link_label.pack(side="left", padx=0)
                # Wire the click handler. We must bind to both the new link
                # label and the existing `label` widget for fall-through.
                if clickable:
                    # The folder path is stashed on the parent before this
                    # call by the caller.
                    folder = getattr(parent, "_link_target", None)
                    parent._link_label.bind(
                        "<Button-1>",
                        lambda _e, f=folder: self.open_folder(f),
                    )
            else:
                # Reuse existing widgets; update only text/colors.
                parent._plain_link_label.configure(text=plain_part, text_color=plain_color)
                parent._link_label.configure(text=link_part, text_color=color)
                if clickable:
                    folder = getattr(parent, "_link_target", None)
                    # Re-bind to the latest folder; unbind first to be safe.
                    parent._link_label.unbind("<Button-1>")
                    parent._link_label.bind(
                        "<Button-1>",
                        lambda _e, f=folder: self.open_folder(f),
                    )
            # Stash the target on parent so the binding above (and later
            # re-binds) can pick it up.
            # The caller sets _last_video_folder_single/list before calling
            # _set_status_label — propagate that onto the placeholder frame.
            if hasattr(self, "_last_video_folder_single") and parent is getattr(self, "single_status_label", None).master:
                parent._link_target = self._last_video_folder_single
            elif hasattr(self, "_last_video_folder_list") and parent is getattr(self, "list_status_label", None).master:
                parent._link_target = self._last_video_folder_list

            # Also make the original plain label invisible while the link
            # frame is showing; it'll come back via the plain path below
            # when a non-link status is set on the next download.
            return

        # Plain path: hide any previously-built link frame and show the
        # original label with the new text.
        existing_frame = getattr(parent, "_status_link_frame", None)
        if existing_frame is not None and existing_frame.winfo_exists():
            existing_frame.place_forget()
        label.place(relx=0.5, rely=0.5, anchor="center")

        font_args = dict(family="Arial", size=12,
                         underline=(underline or clickable))
        cursor = "hand2" if clickable else "arrow"
        label.configure(
            text=text, text_color=color,
            font=ctk.CTkFont(**font_args), cursor=cursor,
        )

    # -------- Single Download trigger ------------------------------------

    def start_download_single(self) -> None:
        url = self.link_entry_single.get().strip()
        if not url:
            self.write_to_log("[Error] Please enter a BBB webinar link first.\n")
            return
        if not detect_ffmpeg():
            self.write_to_log("[Error] FFmpeg not found! Click 'Check / Download FFmpeg'.\n")
            self.check_or_download_ffmpeg()
            return

        output_dir = self.location_entry_single.get().strip() or "./downloads"
        download_videos = bool(self.download_videos_var_1.get())
        download_slides = bool(self.download_slides_var_1.get())
        download_thumbs = bool(self.download_thumbs_var_1.get())
        format_opt = self.opt_format_1.get()

        self.download_button.configure(state="disabled")
        self._set_status_label(
            self.single_status_label,
            text="(1/3) Downloading…",
            clickable=False,
        )

        self.write_to_log(
            f"[Start] Initiating single link download...\nLink: {url}\n"
            f"Save Path: {output_dir}\n",
        )

        thread = bbb_core.DownloadThread(
            url=url, output_dir=output_dir,
            download_videos=download_videos,
            download_slides=download_slides,
            download_thumbs=download_thumbs,
            format_opt=format_opt,
            log_callback=self.write_to_log,
            on_finish_callback=lambda success: self.on_download_single_finished(success),
        )
        thread.daemon = True
        thread.start()

    def on_download_single_finished(self, success: bool) -> None:
        self.download_button.configure(state="normal")
        if not success:
            self._set_status_label(
                self.single_status_label,
                text="Download failed — see logs.",
                color="red", clickable=False,
            )
            self.write_to_log("[Error] Download process encountered an error.\n")
            return

        # The live log inspector (_inspect_log_for_status) should already have
        # swapped the banner to "File is ready here" when the core emitted the
        # "Final ... output ready:" line. If for some reason we didn't catch
        # that signal, fall back to a short clickable label here.
        if (self._last_video_folder_single is None
            or not self._last_video_folder_single):
            try:
                output_dir = self.location_entry_single.get().strip() or "./downloads"
                _, _, record_id = bbb_core.parse_bbb_url(self.link_entry_single.get().strip())
                self._last_video_folder_single = os.path.join(output_dir, record_id, "Videos")
            except Exception:
                self._last_video_folder_single = None

        self._set_status_label(
            self.single_status_label,
            text="File is ready here",
            color="#A07AFF", clickable=True, underline_only_link=True,
        )
        self.write_to_log("[Success] Download finished successfully.\n")

    # -------- List Download trigger --------------------------------------

    def start_download_list(self) -> None:
        links_text = self.links_box_list.get("1.0", "end").strip()
        if not links_text:
            self.write_to_log("[Error] Please paste one or more links first.\n")
            return
        if not detect_ffmpeg():
            self.write_to_log("[Error] FFmpeg not found! Click 'Check / Download FFmpeg'.\n")
            self.check_or_download_ffmpeg()
            return

        links = [ln.strip() for ln in links_text.splitlines() if ln.strip()]
        if not links:
            self.write_to_log("[Error] No valid lines found in the link box.\n")
            return

        output_dir = self.location_entry_list.get().strip() or "./downloads"
        download_videos = bool(self.download_videos_var_2.get())
        download_slides = bool(self.download_slides_var_2.get())
        download_thumbs = bool(self.download_thumbs_var_2.get())
        format_opt = self.opt_format_2.get()

        self.list_download_button.configure(state="disabled")
        self._set_status_label(
            self.list_status_label,
            text=f"(1/3) Downloading {len(links)} links…",
        )
        self.write_to_log(
            f"[Start] Starting batch download of {len(links)} links...\n",
        )

        def worker() -> None:
            try:
                bbb_core.run_batch(
                    links, output_dir,
                    download_videos=download_videos,
                    download_slides=download_slides,
                    download_thumbs=download_thumbs,
                    format_opt=format_opt,
                    log_callback=self.write_to_log,
                )
                self.on_download_list_finished(True)
            except Exception as e:
                self.write_to_log(f"[Batch] Fatal error: {e}\n")
                self.on_download_list_finished(False)

        threading.Thread(target=worker, daemon=True).start()

    def on_download_list_finished(self, success: bool) -> None:
        self.list_download_button.configure(state="normal")
        if success:
            self._set_status_label(
                self.list_status_label,
                text="All links processed.",
                color="#A07AFF", clickable=False,
            )
            self.write_to_log("[Success] All links in the list were processed.\n")
        else:
            self._set_status_label(
                self.list_status_label,
                text="Batch failed — see logs.",
                color="red", clickable=False,
            )
            self.write_to_log("[Error] Batch download encountered an error.\n")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point — launches the BBB Downloader GUI.

    On macOS, unhandled exceptions out of Tk button callbacks can cause
    the interpreter to exit silently (no traceback to the terminal). To
    make those diagnosable, we install a global callback-exception hook
    that logs any such exception to stderr AND to a file before the app
    disappears. This makes 'the app suddenly closed' bugs tractable.
    """
    import os
    import sys
    import traceback
    import tkinter as tk

    crash_log_path = os.path.join(os.path.dirname(__file__), "crash.log")

    def _report_callback_exception(exctype, value, tb):
        text = "".join(traceback.format_exception(exctype, value, tb))
        sys.stderr.write("\n[TK CALLBACK EXCEPTION]\n" + text + "\n")
        try:
            with open(crash_log_path, "a", encoding="utf-8") as f:
                f.write("\n=====" + text + "=====\n")
        except Exception:
            pass

    # Install the hook on the Tk class so every window gets it.
    tk.Tk.report_callback_exception = staticmethod(_report_callback_exception)

    # Also hook sys.excepthook so unhandled exceptions in our own threads
    # (e.g. the download thread) get logged to the same file.
    def _sys_excepthook(exctype, value, tb):
        text = "".join(traceback.format_exception(exctype, value, tb))
        sys.stderr.write("\n[UNHANDLED EXCEPTION]\n" + text + "\n")
        try:
            with open(crash_log_path, "a", encoding="utf-8") as f:
                f.write("\n=====" + text + "=====\n")
        except Exception:
            pass
        sys.__excepthook__(exctype, value, tb)

    sys.excepthook = _sys_excepthook

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
