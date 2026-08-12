"""
ui/pages/settings.py — Settings page
"""
from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from typing import TYPE_CHECKING

import license_service
import updater
from app_config import (
    APP_VERSION, PRODUCT_NAME, PRODUCT_TAGLINE, DEVELOPER_NAME,
    BRAND_NAME, COPYRIGHT_YEAR, DISCORD_URL, SUPPORT_URL,
)
from settings_manager import settings, Settings
from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, SURFACE4, BORDER, BORDER2,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM,
    SUCCESS, SUCCESS_DIM, DANGER, DANGER_DIM, WARNING,
    PAD, F_BODY, F_BOLD, F_SMALL, F_H2, F_LABEL, F_XLARGE, F_TITLE,
)
from ui.widgets import (
    frame, label, sep, card, section_label, Toggle, Toast, confirm_dialog,
)

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class SettingsPage(tk.Frame):
    def __init__(self, parent, token: str, username: str,
                 lic_info: license_service.LicenseInfo, app: "MainWindow"):
        super().__init__(parent, bg=BG)
        self._token = token
        self._lic   = lic_info
        self._app   = app
        self._build()

    # ── layout ────────────────────────────────────────────────────────────────

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
        p  = self._inner
        px = PAD + 8

        # ── Page header ──────────────────────────────────────────────
        tk.Label(p, text="Settings", bg=BG, fg=WHITE,
                 font=tkfont.Font(family="Segoe UI", size=20, weight="bold"),
                 anchor="w").pack(fill="x", padx=px, pady=(PAD + 4, 4))
        tk.Label(p, text="Manage your preferences, license, and account.",
                 bg=BG, fg=TEXT_MUTED, font=F_BODY, anchor="w").pack(
                     fill="x", padx=px)
        sep(p, BORDER).pack(fill="x", padx=px, pady=(14, 0))

        # Two-column layout
        cols = frame(p, bg=BG)
        cols.pack(fill="both", expand=True, padx=px, pady=(20, 0))
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)

        # Left column
        left = frame(cols, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._build_general(left)
        self._build_appearance(left)

        # Right column
        right = frame(cols, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self._build_application(right)
        self._build_license(right)
        self._build_support(right)
        self._build_about(right)

        frame(p, bg=BG, height=PAD).pack()

    # ── Section: General ──────────────────────────────────────────────────────

    def _build_general(self, parent) -> None:
        self._section_card(parent, "General", [
            ("Launch on startup",    "launch_on_startup"),
            ("Start minimized",      "start_minimized"),
            ("Minimize to tray",     "minimize_to_tray"),
            ("Check for updates automatically", "auto_update_check"),
            ("Confirm before closing",          "confirm_before_close"),
        ])

    # ── Section: Appearance ───────────────────────────────────────────────────

    def _build_appearance(self, parent) -> None:
        c = card(parent, bg=SURFACE)
        c.pack(fill="x", pady=(0, 12))
        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=18)

        tk.Label(inner, text="APPEARANCE", bg=SURFACE, fg=TEXT_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 14))

        # Theme (dark only for now)
        self._setting_row(inner, "Theme", "Dark")

        # Accent color picker
        sep(inner, BORDER).pack(fill="x", pady=(10, 10))
        tk.Label(inner, text="Accent Color", bg=SURFACE, fg=TEXT_MUTED,
                 font=F_BODY, anchor="w").pack(fill="x", pady=(0, 8))

        swatch_row = frame(inner, bg=SURFACE)
        swatch_row.pack(fill="x")

        accent_colors = Settings.accent_choices()
        self._selected_accent = tk.StringVar(value=settings.accent)

        for name in accent_colors:
            hex_c = Settings.accent_hex_for(name)
            btn = tk.Canvas(swatch_row, width=24, height=24, bg=SURFACE,
                            bd=0, highlightthickness=2,
                            highlightbackground=ACCENT if name == settings.accent else BORDER,
                            cursor="hand2")
            btn.pack(side="left", padx=(0, 6))
            btn.create_oval(2, 2, 22, 22, fill=hex_c, outline="")
            btn.bind("<Button-1>", lambda _e, n=name, b=btn, h=hex_c: self._pick_accent(n, b, h))

        sep(inner, BORDER).pack(fill="x", pady=(12, 10))

        # Interface scale
        self._setting_row(inner, "Interface Scale", "100%")

        # Animations toggle
        anim_row = frame(inner, bg=SURFACE)
        anim_row.pack(fill="x", pady=(10, 4))
        tk.Label(anim_row, text="Animations", bg=SURFACE, fg=TEXT2,
                 font=F_BODY, anchor="w").pack(side="left")
        Toggle(anim_row, value=settings.get("animations"), bg=SURFACE,
               on_change=lambda v: settings.set("animations", v)).pack(side="right")

        red_row = frame(inner, bg=SURFACE)
        red_row.pack(fill="x", pady=4)
        tk.Label(red_row, text="Reduced Motion", bg=SURFACE, fg=TEXT2,
                 font=F_BODY, anchor="w").pack(side="left")
        Toggle(red_row, value=settings.get("reduced_motion"), bg=SURFACE,
               on_change=lambda v: settings.set("reduced_motion", v)).pack(side="right")

    def _pick_accent(self, name: str, btn_canvas: tk.Canvas, hex_c: str) -> None:
        settings.set("accent", name)
        self._selected_accent.set(name)
        import ui.theme as theme
        theme.reload_accent(hex_c)
        Toast(self._app, f"Accent color changed to {name.capitalize()}.", variant="info")

    # ── Section: Application ──────────────────────────────────────────────────

    def _build_application(self, parent) -> None:
        c = card(parent, bg=SURFACE)
        c.pack(fill="x", pady=(0, 12))
        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=18)

        tk.Label(inner, text="APPLICATION", bg=SURFACE, fg=TEXT_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 14))

        self._setting_row(inner, "Current Version", f"v{APP_VERSION}")

        # Update channel dropdown
        sep(inner, BORDER).pack(fill="x", pady=(10, 10))
        ch_row = frame(inner, bg=SURFACE)
        ch_row.pack(fill="x")
        tk.Label(ch_row, text="Update Channel", bg=SURFACE, fg=TEXT_MUTED,
                 font=F_BODY, anchor="w").pack(side="left")

        self._ch_var = tk.StringVar(value=settings.update_channel.capitalize())
        ch_cb = ttk.Combobox(ch_row, textvariable=self._ch_var,
                              values=["Stable", "Beta"],
                              state="readonly", width=10)
        ch_cb.pack(side="right")
        ch_cb.bind("<<ComboboxSelected>>",
                   lambda _e: settings.set("update_channel",
                                           self._ch_var.get().lower()))

        sep(inner, BORDER).pack(fill="x", pady=(12, 12))

        # Check for updates button
        upd_btn = tk.Button(
            inner, text="CHECK FOR UPDATES",
            bg=SURFACE3, fg=TEXT2,
            activebackground=SURFACE4, activeforeground=TEXT,
            relief="flat", bd=0, font=F_BOLD, cursor="hand2",
            pady=9, padx=16,
            highlightthickness=1, highlightbackground=BORDER,
            command=self._check_updates,
        )
        upd_btn.pack(anchor="w")
        upd_btn.bind("<Enter>", lambda _e: upd_btn.configure(bg=SURFACE4, fg=TEXT))
        upd_btn.bind("<Leave>", lambda _e: upd_btn.configure(bg=SURFACE3, fg=TEXT2))

    # ── Section: License ──────────────────────────────────────────────────────

    def _build_license(self, parent) -> None:
        c = card(parent, bg=SURFACE)
        c.pack(fill="x", pady=(0, 12))
        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=18)

        tk.Label(inner, text="LICENSE", bg=SURFACE, fg=TEXT_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 14))

        status_color = SUCCESS if self._lic.is_active else DANGER
        self._setting_row(inner, "Status",  self._lic.status_label, color=status_color)
        self._setting_row(inner, "Key",     self._lic.masked_key())
        self._setting_row(inner, "Expires", self._lic.expiry_date)

        sep(inner, BORDER).pack(fill="x", pady=(12, 12))

        btn_row = frame(inner, bg=SURFACE)
        btn_row.pack(fill="x")

        def _outline_btn(parent, text: str, cmd) -> tk.Button:
            b = tk.Button(parent, text=text,
                          bg=SURFACE3, fg=TEXT2,
                          activebackground=SURFACE4, activeforeground=TEXT,
                          relief="flat", bd=0, font=F_SMALL, cursor="hand2",
                          pady=8, padx=12,
                          highlightthickness=1, highlightbackground=BORDER,
                          command=cmd)
            b.pack(side="left", padx=(0, 8))
            b.bind("<Enter>", lambda _e: b.configure(bg=SURFACE4, fg=TEXT))
            b.bind("<Leave>", lambda _e: b.configure(bg=SURFACE3, fg=TEXT2))
            return b

        _outline_btn(btn_row, "COPY KEY",       self._copy_key)
        _outline_btn(btn_row, "REFRESH LICENSE", self._refresh_license)

    # ── Section: Support ──────────────────────────────────────────────────────

    def _build_support(self, parent) -> None:
        c = card(parent, bg=SURFACE)
        c.pack(fill="x", pady=(0, 12))
        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=18)

        tk.Label(inner, text="SUPPORT", bg=SURFACE, fg=TEXT_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 6))
        tk.Label(inner, text="Need help? We're here for you.",
                 bg=SURFACE, fg=TEXT_MUTED, font=F_BODY).pack(anchor="w", pady=(0, 14))

        btn_row = frame(inner, bg=SURFACE)
        btn_row.pack(fill="x")

        def _support_btn(parent, text: str, cmd, primary: bool = False) -> None:
            b = tk.Button(
                parent, text=text,
                bg=ACCENT if primary else SURFACE3,
                fg=WHITE if primary else TEXT2,
                activebackground=ACCENT_HOV if primary else SURFACE4,
                activeforeground=WHITE if primary else TEXT,
                relief="flat", bd=0, font=F_SMALL, cursor="hand2",
                pady=8, padx=12,
                highlightthickness=1,
                highlightbackground=ACCENT if primary else BORDER,
                command=cmd,
            )
            b.pack(side="left", padx=(0, 8))
            if primary:
                b.bind("<Enter>", lambda _e: b.configure(bg=ACCENT_HOV))
                b.bind("<Leave>", lambda _e: b.configure(bg=ACCENT))
            else:
                b.bind("<Enter>", lambda _e: b.configure(bg=SURFACE4, fg=TEXT))
                b.bind("<Leave>", lambda _e: b.configure(bg=SURFACE3, fg=TEXT2))

        _support_btn(btn_row, "JOIN DISCORD",    self._open_discord, primary=True)
        _support_btn(btn_row, "OPEN SUPPORT",    self._open_support)
        _support_btn(btn_row, "COPY DIAGNOSTICS", self._copy_diagnostics)

    # ── Section: About ────────────────────────────────────────────────────────

    def _build_about(self, parent) -> None:
        c = card(parent, bg=SURFACE2)
        c.pack(fill="x", pady=(0, PAD))

        # Top accent stripe
        tk.Frame(c, bg=ACCENT, height=3).pack(fill="x")

        inner = frame(c, bg=SURFACE2)
        inner.pack(fill="both", padx=20, pady=24)

        # Diamond logo
        logo_c = tk.Canvas(inner, width=36, height=36, bg=SURFACE2,
                            bd=0, highlightthickness=0)
        logo_c.pack()
        pts = [18, 2, 34, 18, 18, 34, 2, 18]
        logo_c.create_polygon(pts, fill=ACCENT, outline="")
        inner_pts = [18, 8, 28, 18, 18, 28, 8, 18]
        logo_c.create_polygon(inner_pts, fill=SURFACE2, outline="")
        logo_c.create_oval(14, 14, 22, 22, fill=ACCENT, outline="")

        tk.Label(inner, text=PRODUCT_NAME, bg=SURFACE2, fg=WHITE,
                 font=tkfont.Font(family="Segoe UI", size=14, weight="bold")
                 ).pack(pady=(10, 2))
        tk.Label(inner, text=f"Version {APP_VERSION}", bg=SURFACE2, fg=ACCENT,
                 font=F_BOLD).pack()
        sep(inner, BORDER).pack(fill="x", pady=(14, 14))
        tk.Label(inner, text=PRODUCT_TAGLINE, bg=SURFACE2, fg=TEXT_MUTED,
                 font=F_BODY).pack()
        tk.Label(inner, text=f"Developed by {DEVELOPER_NAME}",
                 bg=SURFACE2, fg=TEXT_DIM, font=F_SMALL).pack(pady=(6, 0))
        tk.Label(inner, text=f"© {COPYRIGHT_YEAR} {BRAND_NAME}  •  All rights reserved.",
                 bg=SURFACE2, fg=TEXT_DIM, font=F_SMALL).pack(pady=(4, 0))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _section_card(self, parent, title: str,
                      toggle_items: list[tuple[str, str]]) -> None:
        c = card(parent, bg=SURFACE)
        c.pack(fill="x", pady=(0, 12))
        inner = frame(c, bg=SURFACE)
        inner.pack(fill="both", padx=20, pady=18)

        tk.Label(inner, text=title.upper(), bg=SURFACE, fg=TEXT_DIM,
                 font=F_LABEL, anchor="w").pack(fill="x", pady=(0, 14))

        for i, (lbl_txt, key) in enumerate(toggle_items):
            if i > 0:
                sep(inner, BORDER).pack(fill="x", pady=(4, 4))
            row = frame(inner, bg=SURFACE)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=lbl_txt, bg=SURFACE, fg=TEXT2,
                     font=F_BODY, anchor="w").pack(side="left")
            Toggle(row, value=settings.get(key), bg=SURFACE,
                   on_change=lambda v, k=key: settings.set(k, v)).pack(side="right")

    def _setting_row(self, parent, field: str, value: str,
                     color: str = TEXT2) -> None:
        row = frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=field, bg=SURFACE, fg=TEXT_MUTED,
                 font=F_BODY, width=16, anchor="w").pack(side="left")
        tk.Label(row, text=value, bg=SURFACE, fg=color,
                 font=F_BOLD, anchor="w").pack(side="left")

    def _copy_key(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._lic.key)
        Toast(self._app, "License key copied.", variant="success")

    def _refresh_license(self) -> None:
        Toast(self._app, "Refreshing license…", variant="info")
        def _worker():
            lic = license_service.fetch_once(self._token)
            self.after(0, lambda l=lic: self._app.update_license(l))
            self.after(0, lambda: Toast(self._app,
                                        "License refreshed." if lic.is_active
                                        else f"License status: {lic.status_label}",
                                        variant="success" if lic.is_active else "warning"))
        threading.Thread(target=_worker, daemon=True).start()

    def _check_updates(self) -> None:
        Toast(self._app, "Checking for updates…", variant="info")
        def _worker():
            rel = updater.check_for_update(settings.update_channel)
            if rel:
                self.after(0, lambda r=rel: self._app._show_update_dialog(r))
            else:
                self.after(0, lambda: Toast(self._app,
                                            "You're on the latest version.", variant="success"))
        threading.Thread(target=_worker, daemon=True).start()

    def _copy_diagnostics(self) -> None:
        import platform, sys as _sys
        diag = (
            f"Product:  {PRODUCT_NAME} v{APP_VERSION}\n"
            f"Platform: {platform.platform()}\n"
            f"Python:   {_sys.version}\n"
            f"License:  {self._lic.status_label}\n"
            f"Tier:     {self._lic.tier}\n"
            f"Expires:  {self._lic.expiry_date}\n"
        )
        self.clipboard_clear()
        self.clipboard_append(diag)
        Toast(self._app, "Diagnostics copied to clipboard.", variant="success")

    def _open_discord(self) -> None:
        import webbrowser
        webbrowser.open(DISCORD_URL)

    def _open_support(self) -> None:
        import webbrowser
        webbrowser.open(SUPPORT_URL)
