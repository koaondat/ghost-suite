"""
ui/update_dialog.py — Update available dialog
"""
from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import TYPE_CHECKING

import updater
from app_config import PRODUCT_NAME, APP_VERSION
from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, SURFACE4, BORDER,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM, DANGER, DANGER_DIM, SUCCESS,
    PAD, F_BODY, F_BOLD, F_SMALL, F_H2,
)
from ui.widgets import frame, sep, ProgressBar, Toast

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class UpdateDialog(tk.Toplevel):
    """
    Modal shown when a newer version is detected.
    Non-mandatory: UPDATE NOW + LATER.
    Mandatory: UPDATE NOW only.
    """

    def __init__(self, master: "MainWindow", release: dict) -> None:
        super().__init__(master)
        self._master    = master
        self._release   = release
        self._mandatory = bool(release.get("mandatory"))
        self._staged    = None

        self.title(f"{PRODUCT_NAME} Update")
        self.configure(bg=SURFACE)
        self.resizable(False, False)
        self.grab_set()
        self.transient(master)

        if self._mandatory:
            self.protocol("WM_DELETE_WINDOW", lambda: None)
        else:
            self.protocol("WM_DELETE_WINDOW", self._later)

        self._build()
        self._center()

    def _build(self) -> None:
        version = self._release.get("version", "?")
        notes   = self._release.get("releaseNotes") or []

        # Header stripe
        tk.Frame(self, bg=ACCENT if not self._mandatory else DANGER,
                 height=3).pack(fill="x")

        body = frame(self, bg=SURFACE)
        body.pack(fill="both", padx=24, pady=20)

        if self._mandatory:
            tk.Label(body, text="Update Required",
                     bg=SURFACE, fg=DANGER,
                     font=tkfont.Font(family="Segoe UI", size=14, weight="bold")
                     ).pack(anchor="w")
            tk.Label(body,
                     text="This version is no longer supported.\nUpdate to continue.",
                     bg=SURFACE, fg=TEXT_MUTED, font=F_SMALL, justify="left"
                     ).pack(anchor="w", pady=(4, 14))
        else:
            tk.Label(body, text="Update Available",
                     bg=SURFACE, fg=TEXT,
                     font=tkfont.Font(family="Segoe UI", size=14, weight="bold")
                     ).pack(anchor="w")
            tk.Label(body, text=f"Version {version}",
                     bg=SURFACE, fg=ACCENT, font=F_BOLD
                     ).pack(anchor="w", pady=(4, 14))

        if notes:
            tk.Label(body, text="What's New", bg=SURFACE, fg=TEXT_MUTED,
                     font=F_H2).pack(anchor="w")
            notes_box = tk.Frame(body, bg=SURFACE2,
                                  highlightthickness=1, highlightbackground=BORDER)
            notes_box.pack(fill="x", pady=(4, 14))
            for note in notes[:8]:
                tk.Label(notes_box, text=f"  •  {note}",
                          bg=SURFACE2, fg=TEXT2, font=F_SMALL,
                          anchor="w", wraplength=360, justify="left",
                          padx=6, pady=3).pack(fill="x")

        # Progress area
        self._prog_frame = frame(body, bg=SURFACE)
        self._prog_frame.pack(fill="x", pady=(0, 8))
        self._prog_lbl = tk.Label(self._prog_frame, text="",
                                   bg=SURFACE, fg=TEXT_MUTED, font=F_SMALL)
        self._prog_lbl.pack(anchor="w")
        self._prog_bar = ProgressBar(self._prog_frame, height=5,
                                      color=ACCENT, bg=SURFACE3)

        # Error label
        self._err_lbl = tk.Label(body, text="",
                                  bg=SURFACE, fg=DANGER, font=F_SMALL,
                                  wraplength=360, justify="left")
        self._err_lbl.pack(anchor="w")

        sep(body, BORDER).pack(fill="x", pady=(8, 14))

        btn_row = frame(body, bg=SURFACE)
        btn_row.pack(fill="x")

        update_label = "UPDATE NOW" if not self._mandatory else "UPDATE"
        self._update_btn = tk.Button(
            btn_row, text=update_label,
            bg=ACCENT, fg=WHITE,
            activebackground=ACCENT_HOV, activeforeground=WHITE,
            relief="flat", bd=0, font=F_BOLD, cursor="hand2",
            padx=16, pady=9,
            command=self._start_update,
        )
        self._update_btn.pack(side="left")
        self._update_btn.bind("<Enter>",
                               lambda _e: self._update_btn.configure(bg=ACCENT_HOV))
        self._update_btn.bind("<Leave>",
                               lambda _e: self._update_btn.configure(bg=ACCENT))

        if not self._mandatory:
            tk.Button(
                btn_row, text="Later",
                bg=SURFACE3, fg=TEXT_MUTED,
                activebackground=SURFACE4, activeforeground=TEXT,
                relief="flat", bd=0, font=F_BODY, cursor="hand2",
                padx=16, pady=9,
                command=self._later,
            ).pack(side="left", padx=(8, 0))

    def _center(self) -> None:
        self.update_idletasks()
        w  = 420
        h  = self.winfo_reqheight() + 20
        px = self._master.winfo_x() + (self._master.winfo_width()  - w) // 2
        py = self._master.winfo_y() + (self._master.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")

    def _later(self) -> None:
        self.destroy()

    def _start_update(self) -> None:
        self._update_btn.configure(state="disabled", text="Downloading…")
        self._err_lbl.configure(text="")
        self._prog_bar.pack(fill="x", pady=(4, 0))
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self) -> None:
        def _prog(msg: str, pct) -> None:
            self.after(0, lambda m=msg, p=pct: self._on_progress(m, p))

        staged = updater.download_update(self._release, _prog)
        if staged:
            self.after(0, lambda s=staged: self._on_download_done(s))
        else:
            self.after(0, lambda: self._update_btn.configure(
                state="normal", text="Retry"))

    def _on_progress(self, msg: str, pct) -> None:
        self._prog_lbl.configure(text=msg)
        if pct is not None:
            self._prog_bar.set_progress(pct)
        if "Error" in msg or "failed" in msg.lower() or "Verification failed" in msg:
            self._err_lbl.configure(text=msg)
            self._prog_lbl.configure(text="")

    def _on_download_done(self, staged) -> None:
        self._prog_lbl.configure(text="Applying update…")
        self._prog_bar.set_progress(100)
        updater.apply_update(staged, lambda m, p: None)
        self.after(500, lambda: self._master.destroy())
