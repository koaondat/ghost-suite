"""
ui/pages/dashboard.py — Dashboard page
"""
from __future__ import annotations

import datetime
import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import TYPE_CHECKING

import license_service
from app_config import PRODUCT_NAME, APP_VERSION, DISCORD_URL, SUPPORT_URL
from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, SURFACE4, BORDER, BORDER2,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM, ACCENT_DARK,
    SUCCESS, SUCCESS_DIM, DANGER, DANGER_DIM, WARNING, WARNING_DIM,
    PAD, F_BODY, F_BOLD, F_SMALL, F_H2, F_LABEL, F_BIG, F_XLARGE, F_TITLE,
)
from ui.widgets import (
    frame, label, sep, card, Button, section_label, stat_card, Skeleton, Toast,
)

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class DashboardPage(tk.Frame):
    def __init__(self, parent, token: str, username: str,
                 lic_info: license_service.LicenseInfo, app: "MainWindow"):
        super().__init__(parent, bg=BG)
        self._token    = token
        self._username = username
        self._lic      = lic_info
        self._app      = app
        self._countdown_after_id = None
        self._build()

    # ── build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # Scrollable canvas wrapper
        self._canvas = tk.Canvas(self, bg=BG, bd=0, highlightthickness=0)
        scroll = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = frame(self._canvas, bg=BG)
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self._build_content()

    def _on_inner_configure(self, _e):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, e):
        self._canvas.itemconfig(self._win_id, width=e.width)

    def _on_mousewheel(self, e):
        self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

    def _build_content(self) -> None:
        p = self._inner
        px = PAD + 8

        # ── Welcome header ────────────────────────────────────────────
        hdr = frame(p, bg=BG)
        hdr.pack(fill="x", padx=px, pady=(PAD + 4, 0))

        tk.Label(hdr, text=f"Welcome back, {self._username}",
                 bg=BG, fg=WHITE,
                 font=tkfont.Font(family="Segoe UI", size=20, weight="bold"),
                 anchor="w").pack(fill="x")
        tk.Label(hdr, text="Everything is ready.",
                 bg=BG, fg=TEXT_MUTED,
                 font=tkfont.Font(family="Segoe UI", size=10),
                 anchor="w").pack(fill="x")

        sep(p, BORDER).pack(fill="x", padx=px, pady=(16, 0))

        # ── Quick-stat cards row ──────────────────────────────────────
        stat_row = frame(p, bg=BG)
        stat_row.pack(fill="x", padx=px, pady=(16, 0))
        stat_row.columnconfigure((0, 1, 2, 3), weight=1, uniform="sc")

        status_color = SUCCESS if self._lic.is_active else DANGER

        cards = [
            ("LICENSE",        self._lic.status_label, "", status_color),
            ("TIME REMAINING", self._lic.time_remaining_str(), "", ACCENT),
            ("PRODUCT",        PRODUCT_NAME, "", TEXT2),
            ("VERSION",        f"v{APP_VERSION}", "", TEXT2),
        ]
        self._stat_labels: dict[str, tk.Label] = {}
        for col, (lbl_txt, val_txt, sub, color) in enumerate(cards):
            c = card(stat_row, bg=SURFACE)
            c.grid(row=0, column=col, padx=(0 if col == 0 else 8, 0), sticky="nsew", ipady=4)
            inner = frame(c, bg=SURFACE)
            inner.pack(fill="both", padx=16, pady=14)
            tk.Label(inner, text=lbl_txt, bg=SURFACE, fg=TEXT_DIM,
                     font=F_LABEL, anchor="w").pack(anchor="w")
            vl = tk.Label(inner, text=val_txt, bg=SURFACE, fg=color,
                          font=tkfont.Font(family="Segoe UI", size=16, weight="bold"),
                          anchor="w")
            vl.pack(anchor="w", pady=(3, 0))
            self._stat_labels[lbl_txt] = vl

        # ── Main grid (license card + quick actions) ──────────────────
        main_row = frame(p, bg=BG)
        main_row.pack(fill="x", padx=px, pady=(20, 0))
        main_row.columnconfigure(0, weight=3)
        main_row.columnconfigure(1, weight=2)

        self._build_license_card(main_row, row=0, col=0)
        self._build_quick_actions(main_row, row=0, col=1)

        # ── Announcements ─────────────────────────────────────────────
        self._build_announcements(p, padx=px)

    # ── License card ──────────────────────────────────────────────────────────

    def _build_license_card(self, parent, row: int, col: int) -> None:
        c = card(parent, bg=SURFACE)
        c.grid(row=row, column=col, padx=(0, 12), sticky="nsew", pady=0)

        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=20)

        # Header
        hdr = frame(inner, bg=SURFACE)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Your License", bg=SURFACE, fg=TEXT,
                 font=tkfont.Font(family="Segoe UI", size=12, weight="bold"),
                 anchor="w").pack(side="left")
        status_bg = SUCCESS_DIM if self._lic.is_active else DANGER_DIM
        status_fg = SUCCESS     if self._lic.is_active else DANGER
        tk.Label(hdr, text=f"  {self._lic.status_label}  ",
                 bg=status_bg, fg=status_fg,
                 font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 padx=4, pady=2).pack(side="right")

        sep(inner, BORDER).pack(fill="x", pady=(12, 14))

        # Masked key display
        key_frame = frame(inner, bg=SURFACE3,
                          highlightthickness=1, highlightbackground=BORDER2)
        key_frame.pack(fill="x")

        self._key_visible = False
        self._masked_key  = self._lic.masked_key()
        self._full_key    = self._lic.key

        self._key_lbl = tk.Label(key_frame, text=self._masked_key,
                                  bg=SURFACE3, fg=TEXT,
                                  font=tkfont.Font(family="Consolas", size=11),
                                  anchor="w", padx=14, pady=10)
        self._key_lbl.pack(side="left", fill="x", expand=True)

        toggle_btn = tk.Label(key_frame, text="👁", bg=SURFACE3, fg=TEXT_MUTED,
                               font=tkfont.Font(family="Segoe UI Emoji", size=9),
                               padx=10, cursor="hand2")
        toggle_btn.pack(side="right")
        toggle_btn.bind("<Button-1>", lambda _e: self._toggle_key_vis())

        # Metadata rows
        sep(inner, BORDER).pack(fill="x", pady=(14, 12))

        rows = [
            ("Status",    self._lic.status_label, SUCCESS if self._lic.is_active else DANGER),
            ("Activated", self._lic.activated_date, TEXT2),
            ("Expires",   self._lic.expiry_date,    TEXT2),
            ("Time Left", self._lic.time_remaining_str(), ACCENT),
        ]
        self._meta_labels: dict[str, tk.Label] = {}
        for field, val, color in rows:
            row_f = frame(inner, bg=SURFACE)
            row_f.pack(fill="x", pady=3)
            tk.Label(row_f, text=field, bg=SURFACE, fg=TEXT_MUTED,
                     font=F_BODY, width=10, anchor="w").pack(side="left")
            vl = tk.Label(row_f, text=val, bg=SURFACE, fg=color,
                          font=F_BOLD, anchor="w")
            vl.pack(side="left")
            self._meta_labels[field] = vl

        sep(inner, BORDER).pack(fill="x", pady=(14, 12))

        # Copy key button
        copy_btn = tk.Button(
            inner, text="COPY KEY",
            bg=SURFACE3, fg=TEXT_MUTED,
            activebackground=SURFACE4, activeforeground=TEXT,
            relief="flat", bd=0, font=F_BOLD,
            cursor="hand2", pady=8, padx=16,
            highlightthickness=1, highlightbackground=BORDER,
            command=self._copy_key,
        )
        copy_btn.pack(anchor="w")
        copy_btn.bind("<Enter>", lambda _e: copy_btn.configure(bg=SURFACE4, fg=TEXT))
        copy_btn.bind("<Leave>", lambda _e: copy_btn.configure(bg=SURFACE3, fg=TEXT_MUTED))

    def _toggle_key_vis(self) -> None:
        self._key_visible = not self._key_visible
        self._key_lbl.configure(
            text=self._full_key if self._key_visible else self._masked_key)

    def _copy_key(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._full_key)
        Toast(self._app, "License key copied to clipboard.", variant="success")

    # ── Quick actions ─────────────────────────────────────────────────────────

    def _build_quick_actions(self, parent, row: int, col: int) -> None:
        c = card(parent, bg=SURFACE)
        c.grid(row=row, column=col, sticky="nsew")

        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=20)

        tk.Label(inner, text="QUICK ACTIONS", bg=SURFACE, fg=TEXT_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 14))

        def _action_btn(text: str, cmd, variant: str = "secondary") -> None:
            b = tk.Button(
                inner, text=text,
                bg=ACCENT if variant == "primary" else SURFACE3,
                fg=WHITE if variant == "primary" else TEXT2,
                activebackground=ACCENT_HOV if variant == "primary" else SURFACE4,
                activeforeground=WHITE if variant == "primary" else TEXT,
                relief="flat", bd=0, font=F_BOLD, cursor="hand2",
                pady=11, padx=16,
                highlightthickness=1,
                highlightbackground=ACCENT if variant == "primary" else BORDER,
                command=cmd,
            )
            b.pack(fill="x", pady=(0, 8))
            if variant == "primary":
                b.bind("<Enter>", lambda _e: b.configure(bg=ACCENT_HOV))
                b.bind("<Leave>", lambda _e: b.configure(bg=ACCENT))
            else:
                b.bind("<Enter>", lambda _e: b.configure(bg=SURFACE4, fg=TEXT))
                b.bind("<Leave>", lambda _e: b.configure(bg=SURFACE3, fg=TEXT2))

        _action_btn("OPEN SPOOFER",      self._open_spoofer, "primary")
        _action_btn("CHECK FOR UPDATES", self._check_updates)
        _action_btn("SUPPORT",           self._open_support)

    # ── Announcements ─────────────────────────────────────────────────────────

    def _build_announcements(self, parent, padx: int) -> None:
        frame(parent, bg=BG, height=20).pack()
        section_label(parent, "Announcements", bg=BG).pack(
            fill="x", padx=padx, pady=(0, 8))

        c = card(parent, bg=SURFACE)
        c.pack(fill="x", padx=padx, pady=(0, PAD))

        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=16)

        # Header row
        hdr = frame(inner, bg=SURFACE)
        hdr.pack(fill="x")
        dot = tk.Canvas(hdr, width=8, height=8, bg=SURFACE,
                        bd=0, highlightthickness=0)
        dot.pack(side="left", padx=(0, 8))
        dot.create_oval(0, 0, 8, 8, fill=ACCENT, outline="")
        tk.Label(hdr, text=f"{PRODUCT_NAME} v{APP_VERSION} — Latest Release",
                 bg=SURFACE, fg=TEXT,
                 font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                 anchor="w").pack(side="left")
        tk.Label(hdr, text="Today", bg=SURFACE, fg=TEXT_DIM,
                 font=F_SMALL).pack(side="right")

        sep(inner, BORDER).pack(fill="x", pady=(10, 10))

        bullets = [
            "Application launched successfully.",
            "All system profiles ready.",
            "License validated with server.",
            "Auto-update check enabled.",
        ]
        for b in bullets:
            row = frame(inner, bg=SURFACE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="•", bg=SURFACE, fg=ACCENT,
                     font=F_BODY).pack(side="left", padx=(0, 8))
            tk.Label(row, text=b, bg=SURFACE, fg=TEXT2,
                     font=F_BODY, anchor="w").pack(side="left")

    # ── Countdown timer ───────────────────────────────────────────────────────

    def on_show(self) -> None:
        self._start_countdown()
        threading.Thread(target=self._refresh_license_bg, daemon=True).start()

    def _start_countdown(self) -> None:
        if self._countdown_after_id:
            self.after_cancel(self._countdown_after_id)
        self._tick()

    def _tick(self) -> None:
        # Recalculate time remaining from expiry
        remaining = self._lic.days_remaining
        # Build hh/mm/ss from days for a live countdown feel
        total_secs = remaining * 86400
        d  = total_secs // 86400
        h  = (total_secs % 86400) // 3600
        m  = (total_secs % 3600) // 60
        display = f"{d}d {h:02d}h {m:02d}m"
        if "TIME REMAINING" in self._stat_labels:
            self._stat_labels["TIME REMAINING"].configure(text=display)
        if "Time Left" in self._meta_labels:
            self._meta_labels["Time Left"].configure(text=f"{remaining} days")
        self._countdown_after_id = self.after(60_000, self._tick)

    def _refresh_license_bg(self) -> None:
        lic = license_service.fetch_once(self._token)
        self.after(0, lambda: self.update_license(lic))

    def update_license(self, lic: license_service.LicenseInfo) -> None:
        self._lic = lic
        # Update stat card labels
        status_color = SUCCESS if lic.is_active else DANGER
        if "LICENSE" in self._stat_labels:
            self._stat_labels["LICENSE"].configure(
                text=lic.status_label, fg=status_color)
        if "TIME REMAINING" in self._stat_labels:
            self._stat_labels["TIME REMAINING"].configure(
                text=lic.time_remaining_str(), fg=ACCENT)
        # Update meta labels
        if "Status" in self._meta_labels:
            self._meta_labels["Status"].configure(
                text=lic.status_label,
                fg=SUCCESS if lic.is_active else DANGER)
        if "Expires" in self._meta_labels:
            self._meta_labels["Expires"].configure(text=lic.expiry_date)
        if "Activated" in self._meta_labels:
            self._meta_labels["Activated"].configure(text=lic.activated_date)
        # Update key display
        self._full_key   = lic.key
        self._masked_key = lic.masked_key()
        if not self._key_visible:
            self._key_lbl.configure(text=self._masked_key)

    def _open_spoofer(self) -> None:
        self._app.open_spoofer()

    def _check_updates(self) -> None:
        import updater
        Toast(self._app, "Checking for updates…", variant="info")
        def _worker():
            rel = updater.check_for_update()
            if rel:
                self.after(0, lambda r=rel: self._app._show_update_dialog(r))
            else:
                self.after(0, lambda: Toast(self._app, "You're up to date.", variant="success"))
        threading.Thread(target=_worker, daemon=True).start()

    def _open_support(self) -> None:
        import webbrowser
        webbrowser.open(SUPPORT_URL)
