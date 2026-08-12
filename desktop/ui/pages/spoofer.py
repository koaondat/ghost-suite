"""
ui/pages/spoofer.py — Spoofer page
"""
from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import TYPE_CHECKING

import license_service
from spoofer_engine import SpooferEngine, RestoreEngine, SpooferResult, category_statuses
from settings_manager import settings
from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, SURFACE4, BORDER, BORDER2,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM,
    SUCCESS, SUCCESS_DIM, DANGER, DANGER_DIM, WARNING,
    PAD, F_BODY, F_BOLD, F_SMALL, F_H2, F_LABEL, F_BIG, F_XLARGE,
)
from ui.widgets import (
    frame, label, sep, card, section_label, Toggle, ProgressBar,
    StatusDot, Button, Toast, confirm_dialog,
)

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class SpooferPage(tk.Frame):
    def __init__(self, parent, token: str, username: str,
                 lic_info: license_service.LicenseInfo, app: "MainWindow"):
        super().__init__(parent, bg=BG)
        self._token   = token
        self._lic     = lic_info
        self._app     = app
        self._running = False
        self._engine: SpooferEngine | None = None
        self._build()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self._canvas = tk.Canvas(self, bg=BG, bd=0, highlightthickness=0)
        scroll = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = frame(self._canvas, bg=BG)
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
                         lambda _e: self._canvas.configure(
                             scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(self._win_id, width=e.width))
        self._canvas.bind_all("<MouseWheel>",
                              lambda e: self._canvas.yview_scroll(
                                  int(-1 * (e.delta / 120)), "units"))

        self._build_content()

    def _build_content(self) -> None:
        p   = self._inner
        px  = PAD + 8

        # ── Page header ──────────────────────────────────────────────
        hdr = frame(p, bg=BG)
        hdr.pack(fill="x", padx=px, pady=(PAD + 4, 0))
        tk.Label(hdr, text="Spoofer", bg=BG, fg=WHITE,
                 font=tkfont.Font(family="Segoe UI", size=20, weight="bold"),
                 anchor="w").pack(side="left")

        # Status badge (top right of header)
        self._status_badge = tk.Label(
            hdr, text="  ● READY  ",
            bg=SUCCESS_DIM, fg=SUCCESS,
            font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
            padx=6, pady=4)
        self._status_badge.pack(side="right")

        sep(p, BORDER).pack(fill="x", padx=px, pady=(14, 0))

        # ── Main columns ─────────────────────────────────────────────
        cols = frame(p, bg=BG)
        cols.pack(fill="both", expand=True, padx=px, pady=(20, 0))
        cols.columnconfigure(0, weight=3)
        cols.columnconfigure(1, weight=2)

        self._build_main_panel(cols)
        self._build_options_panel(cols)

        # ── Category status table ─────────────────────────────────────
        self._build_category_status(p, padx=px)

    # ── Main control panel ────────────────────────────────────────────────────

    def _build_main_panel(self, parent) -> None:
        c = card(parent, bg=SURFACE)
        c.grid(row=0, column=0, padx=(0, 12), sticky="nsew")

        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=24, pady=24)

        # Status row
        status_row = frame(inner, bg=SURFACE)
        status_row.pack(fill="x", pady=(0, 6))
        tk.Label(status_row, text="System Status", bg=SURFACE, fg=TEXT_MUTED,
                 font=F_H2, anchor="w").pack(side="left")
        self._status_dot  = StatusDot(status_row, color=SUCCESS, pulse=True, bg=SURFACE)
        self._status_dot.pack(side="right", padx=(8, 0))
        self._status_lbl = tk.Label(status_row, text="Ready", bg=SURFACE,
                                    fg=SUCCESS, font=F_BOLD, anchor="e")
        self._status_lbl.pack(side="right")

        # Protection mode row
        mode_row = frame(inner, bg=SURFACE)
        mode_row.pack(fill="x", pady=(6, 0))
        tk.Label(mode_row, text="Protection Mode", bg=SURFACE, fg=TEXT_MUTED,
                 font=F_H2, anchor="w").pack(side="left")
        tk.Label(mode_row, text="Standard", bg=SURFACE, fg=TEXT2,
                 font=F_BODY, anchor="e").pack(side="right")

        sep(inner, BORDER).pack(fill="x", pady=(18, 18))

        # Big launch box
        launch_box = tk.Frame(inner, bg=SURFACE2,
                              highlightthickness=1, highlightbackground=BORDER2)
        launch_box.pack(fill="x")

        box_inner = frame(launch_box, bg=SURFACE2)
        box_inner.pack(fill="both", padx=24, pady=28)

        self._launch_title = tk.Label(
            box_inner, text="READY TO START",
            bg=SURFACE2, fg=TEXT_MUTED,
            font=tkfont.Font(family="Segoe UI", size=11, weight="bold"),
            anchor="center")
        self._launch_title.pack(fill="x", pady=(0, 20))

        # Main START button
        self._start_btn = tk.Button(
            box_inner, text="START SPOOF",
            bg=ACCENT, fg=WHITE,
            activebackground=ACCENT_HOV, activeforeground=WHITE,
            relief="flat", bd=0,
            font=tkfont.Font(family="Segoe UI", size=12, weight="bold"),
            cursor="hand2", pady=14, padx=36,
            highlightthickness=2, highlightbackground=ACCENT,
            command=self._on_start,
        )
        self._start_btn.pack(fill="x")
        self._start_btn.bind("<Enter>",
                             lambda _e: self._start_btn.configure(bg=ACCENT_HOV))
        self._start_btn.bind("<Leave>",
                             lambda _e: self._start_btn.configure(bg=ACCENT))

        sep(inner, BORDER).pack(fill="x", pady=(20, 18))

        # Progress area (hidden until running)
        self._prog_frame = frame(inner, bg=SURFACE)
        self._prog_frame.pack(fill="x")

        self._prog_label = tk.Label(self._prog_frame, text="",
                                     bg=SURFACE, fg=TEXT_MUTED, font=F_SMALL, anchor="w")
        self._prog_label.pack(fill="x")

        self._prog_bar = ProgressBar(self._prog_frame, height=6,
                                      color=ACCENT, bg=SURFACE3)
        self._prog_bar.pack(fill="x", pady=(6, 0))
        self._prog_frame.pack_forget()    # hidden until start

        # Result message
        self._result_lbl = tk.Label(inner, text="",
                                     bg=SURFACE, fg=SUCCESS, font=F_BOLD,
                                     wraplength=400, justify="left", anchor="w")
        self._result_lbl.pack(fill="x", pady=(10, 0))

        sep(inner, BORDER).pack(fill="x", pady=(18, 18))

        # Restore button
        restore_btn = tk.Button(
            inner, text="RESTORE",
            bg=SURFACE3, fg=TEXT_MUTED,
            activebackground=SURFACE4, activeforeground=TEXT,
            relief="flat", bd=0, font=F_BOLD,
            cursor="hand2", pady=9, padx=20,
            highlightthickness=1, highlightbackground=BORDER,
            command=self._on_restore,
        )
        restore_btn.pack(anchor="w")
        restore_btn.bind("<Enter>",
                         lambda _e: restore_btn.configure(bg=SURFACE4, fg=TEXT))
        restore_btn.bind("<Leave>",
                         lambda _e: restore_btn.configure(bg=SURFACE3, fg=TEXT_MUTED))
        tk.Label(inner, text="Revert changes made by this application",
                 bg=SURFACE, fg=TEXT_DIM, font=F_SMALL, anchor="w").pack(anchor="w", pady=(4, 0))

    # ── Options panel ─────────────────────────────────────────────────────────

    def _build_options_panel(self, parent) -> None:
        outer = frame(parent, bg=BG)
        outer.grid(row=0, column=1, sticky="nsew")

        # ── Profile toggles card ──────────────────────────────────────
        c = card(outer, bg=SURFACE)
        c.pack(fill="x", pady=(0, 12))

        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=18)

        # Expandable "Advanced Options" header
        adv_hdr = frame(inner, bg=SURFACE)
        adv_hdr.pack(fill="x")
        self._adv_expanded = tk.BooleanVar(value=True)
        adv_toggle = tk.Label(
            adv_hdr, text="Advanced Options  ▾",
            bg=SURFACE, fg=TEXT, cursor="hand2",
            font=tkfont.Font(family="Segoe UI", size=9, weight="bold"))
        adv_toggle.pack(side="left")
        adv_toggle.bind("<Button-1>", lambda _e: self._toggle_advanced())

        sep(inner, BORDER).pack(fill="x", pady=(12, 14))

        self._adv_body = frame(inner, bg=SURFACE)
        self._adv_body.pack(fill="x")

        toggle_opts = [
            ("System Profile",  "spoof_system"),
            ("Storage Profile", "spoof_storage"),
            ("Network Profile", "spoof_network"),
            ("Windows Profile", "spoof_windows"),
            ("Temp Cleanup",    "cleanup_temp"),
        ]
        self._toggles: dict[str, Toggle] = {}
        for label_txt, key in toggle_opts:
            self._add_toggle_row(self._adv_body, label_txt, key)

        sep(self._adv_body, BORDER).pack(fill="x", pady=(10, 10))

        extra_opts = [
            ("Randomize on every run",      "randomize_every_run"),
            ("Create backup before changes","create_backup"),
            ("Verify after changes",        "verify_after_spoof"),
        ]
        for label_txt, key in extra_opts:
            self._add_toggle_row(self._adv_body, label_txt, key)

    def _add_toggle_row(self, parent, label_txt: str, key: str) -> None:
        row = frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label_txt, bg=SURFACE, fg=TEXT2,
                 font=F_BODY, anchor="w").pack(side="left")
        t = Toggle(row, value=settings.get(key), bg=SURFACE,
                   on_change=lambda v, k=key: settings.set(k, v))
        t.pack(side="right")
        self._toggles[key] = t

    def _toggle_advanced(self) -> None:
        if self._adv_expanded.get():
            self._adv_body.pack_forget()
            self._adv_expanded.set(False)
        else:
            self._adv_body.pack(fill="x")
            self._adv_expanded.set(True)

    # ── Category status table ─────────────────────────────────────────────────

    def _build_category_status(self, parent, padx: int) -> None:
        frame(parent, bg=BG, height=20).pack()
        section_label(parent, "Component Status", bg=BG).pack(
            fill="x", padx=padx, pady=(0, 8))

        c = card(parent, bg=SURFACE)
        c.pack(fill="x", padx=padx, pady=(0, PAD))

        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=16)

        statuses = category_statuses()
        self._cat_labels: dict[str, tk.Label] = {}

        cols = frame(inner, bg=SURFACE)
        cols.pack(fill="x")
        for i, (cat, status) in enumerate(statuses.items()):
            col = frame(cols, bg=SURFACE)
            col.pack(side="left", expand=True, fill="x", padx=(0 if i == 0 else 16, 0))

            dot_row = frame(col, bg=SURFACE)
            dot_row.pack(fill="x")

            dot = tk.Canvas(dot_row, width=8, height=8, bg=SURFACE,
                            bd=0, highlightthickness=0)
            dot.pack(side="left", padx=(0, 6))
            dot.create_oval(0, 0, 8, 8, fill=SUCCESS, outline="")

            tk.Label(dot_row, text=cat, bg=SURFACE, fg=TEXT_MUTED,
                     font=F_SMALL).pack(side="left")

            vl = tk.Label(col, text=status, bg=SURFACE, fg=SUCCESS,
                          font=F_BOLD, anchor="w")
            vl.pack(fill="x", pady=(2, 0))
            self._cat_labels[cat] = vl

    # ── Spoof logic ───────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._running:
            return
        if not self._lic.is_active:
            Toast(self._app, "Active license required.", variant="error")
            return

        if not confirm_dialog(
            self._app,
            "Start Spoof",
            "This will apply system profile changes.\nA backup will be created automatically.",
            confirm_label="Start",
        ):
            return

        self._set_running(True)
        opts = settings.spoof_options

        self._engine = SpooferEngine(
            options=opts,
            progress_cb=lambda lbl, pct: self.after(
                0, lambda l=lbl, p=pct: self._on_progress(l, p)),
            done_cb=lambda result: self.after(
                0, lambda r=result: self._on_done(r)),
        )
        self._engine.start()

    def _on_progress(self, step_label: str, pct: int) -> None:
        self._prog_frame.pack(fill="x")
        self._prog_label.configure(text=step_label)
        self._prog_bar.set_progress(pct)
        self._launch_title.configure(text=step_label.upper())
        # Update category dots
        step_lower = step_label.lower()
        for cat in ("System", "Storage", "Network", "Windows", "Cleanup"):
            if cat.lower() in step_lower and cat in self._cat_labels:
                self._cat_labels[cat].configure(text="Processing…", fg=ACCENT)

    def _on_done(self, result: SpooferResult) -> None:
        self._set_running(False)
        if result.ok:
            self._prog_label.configure(text="Complete ✓")
            self._prog_bar.set_progress(100)
            self._result_lbl.configure(text="Spoof completed successfully.", fg=SUCCESS)
            self._launch_title.configure(text="COMPLETE")
            self._status_badge.configure(text="  ● COMPLETE  ",
                                          bg=SUCCESS_DIM, fg=SUCCESS)
            self._status_lbl.configure(text="Complete", fg=SUCCESS)
            # Reset category dots to Ready
            for cat, lbl in self._cat_labels.items():
                lbl.configure(text="Applied", fg=SUCCESS)
            Toast(self._app, "Spoof completed successfully.", variant="success")
        else:
            self._result_lbl.configure(text=f"Error: {result.message}", fg=DANGER)
            self._launch_title.configure(text="ERROR")
            self._status_badge.configure(text="  ● ERROR  ",
                                          bg=DANGER_DIM, fg=DANGER)
            Toast(self._app, f"Operation failed: {result.message}", variant="error")

    def _set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self._start_btn.configure(
                state="disabled", text="RUNNING…", bg=ACCENT_DIM)
            self._status_badge.configure(text="  ● RUNNING  ",
                                          bg=ACCENT_DIM, fg=ACCENT)
            self._status_lbl.configure(text="Running", fg=ACCENT)
            self._status_dot.set_color(ACCENT, pulse=True)
            self._result_lbl.configure(text="")
        else:
            self._start_btn.configure(
                state="normal", text="START SPOOF", bg=ACCENT)
            self._status_dot.set_color(SUCCESS, pulse=True)

    def _on_restore(self) -> None:
        if self._running:
            return
        if not confirm_dialog(
            self._app,
            "Restore Previous State",
            "This will revert changes made by the last spoof operation.\nProceed?",
            confirm_label="Restore",
        ):
            return

        self._set_running(True)
        self._launch_title.configure(text="RESTORING…")
        self._prog_frame.pack(fill="x")

        engine = RestoreEngine(
            progress_cb=lambda lbl, pct: self.after(
                0, lambda l=lbl, p=pct: self._on_restore_progress(l, p)),
            done_cb=lambda result: self.after(
                0, lambda r=result: self._on_restore_done(r)),
        )
        engine.start()

    def _on_restore_progress(self, label: str, pct: int) -> None:
        self._prog_label.configure(text=label)
        self._prog_bar.set_progress(pct)
        self._launch_title.configure(text=label.upper())

    def _on_restore_done(self, result: SpooferResult) -> None:
        self._set_running(False)
        if result.ok:
            self._result_lbl.configure(text="Restore completed.", fg=SUCCESS)
            self._launch_title.configure(text="RESTORED")
            Toast(self._app, "Restore completed successfully.", variant="success")
            for lbl in self._cat_labels.values():
                lbl.configure(text="Ready", fg=SUCCESS)
        else:
            self._result_lbl.configure(text=f"Restore error: {result.message}", fg=DANGER)
            Toast(self._app, result.message, variant="error")

    def on_show(self) -> None:
        # Reset to idle state each time the page is shown
        if not self._running:
            self._launch_title.configure(text="READY TO START")
            self._result_lbl.configure(text="")
            self._prog_frame.pack_forget()
            self._status_badge.configure(text="  ● READY  ",
                                          bg=SUCCESS_DIM, fg=SUCCESS)
