"""
gui.py — GhostConfig  (GUI entry point)
========================================
Tabbed main window with 5 tabs:
  Dashboard    — live license key, GUID, volume serial, status cards
  Spoofer      — GUID / MAC / volume controls with live log
  Devices      — detailed hardware component info
  Task Manager — live process monitor
  Settings     — update channel, license display, backup dir
  Support      — FAQ / help

Admin functionality is managed entirely from the web admin panel.

Auth screen is shown as a Toplevel first; main window appears on
successful login.

Requires: Python 3.8+, Windows, run as Administrator.
"""

from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import os
import queue
import random
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Callable

# ── Auto-update: current version of this build ───────────────────────────────
CURRENT_VERSION = "1.0.0"

# Update channel is persisted in update_settings.json next to the exe
_UPDATE_SETTINGS_PATH: Path = (
    Path(sys.executable).parent if getattr(sys, "frozen", False)
    else Path(__file__).parent
) / "update_settings.json"

_API_BASE_URL = os.environ.get("GHOST_API_URL", "").rstrip("/")


def _load_update_settings() -> dict:
    try:
        if _UPDATE_SETTINGS_PATH.exists():
            return json.loads(_UPDATE_SETTINGS_PATH.read_text("utf-8"))
    except Exception:
        pass
    return {"channel": "stable"}


def _save_update_settings(data: dict) -> None:
    try:
        _UPDATE_SETTINGS_PATH.write_text(json.dumps(data, indent=2), "utf-8")
    except Exception:
        pass


def _semver_tuple(v: str) -> tuple:
    """Parse '1.2.3' or 'v1.2.3' → (1, 2, 3). Returns (0, 0, 0) on error."""
    try:
        parts = v.lstrip("v").split(".", 2)
        return tuple(int(p) for p in (parts + ["0", "0"])[:3])
    except Exception:
        return (0, 0, 0)


def _fetch_latest_release(channel: str = "stable") -> dict | None:
    """Call /api/releases/latest and return the JSON dict, or None on failure."""
    if not _API_BASE_URL:
        return None
    url = f"{_API_BASE_URL}/api/releases/latest?channel={channel}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"GhostConfig/{CURRENT_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            return data
    except Exception:
        pass
    return None


def _fetch_client_settings() -> dict:
    """GET /api/client/settings — returns silent_updates flag etc."""
    if not _API_BASE_URL:
        return {}
    try:
        req = urllib.request.Request(
            f"{_API_BASE_URL}/api/client/settings",
            headers={"User-Agent": f"GhostConfig/{CURRENT_VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# ── path so PyInstaller frozen exe can find keygen / config_utility ──────────
sys.path.insert(0, os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__
)))

import config_utility as cu
import keygen as kg
import devices as dv
try:
    import activity_log as al
except ImportError:
    al = None  # type: ignore[assignment]
from license_manager import (
    LicenseRole,
    Permission,
    PermissionDeniedError,
    PermissionManager,
    LicenseExpiredError,
    LicenseRevokedError,
)

# ── Activity-tab guard: only build/show if the admin has VIEW_LOGIN_ACTIVITY ──
# (AdminPanel is already ADMIN-only — this adds a defence-in-depth check.)
def _has_activity_perm(master: tk.Tk) -> bool:
    pm = getattr(master, "_pm", None)
    if pm is None:
        return False
    return pm.has_permission(Permission.VIEW_LOGIN_ACTIVITY)

# ── Palette  — dark charcoal theme ───────────────────────────────────────────
BG         = "#141618"          # app background  (near-black charcoal)
SURFACE    = "#1e2025"          # panel / card surface
SURFACE2   = "#25282e"          # slightly lighter panel
SURFACE3   = "#2c3038"          # hover / alternate row
BORDER     = "#32363f"          # subtle border
BORDER2    = "#3d424d"          # slightly stronger border
TEXT       = "#e8eaed"          # primary text (near-white)
TEXT2      = "#c9cdd4"          # secondary text
TEXT_MUTED = "#7c8492"          # muted / labels
TEXT_MUTED2= "#555c6a"          # very muted
ACCENT     = "#4f8ef7"          # blue accent
ACCENT_HOV = "#3b7de8"          # accent hover
ACCENT_LIT = "#1a3260"          # accent tint (dark-mode safe)
ACCENT_DIM = "#1a2a44"          # active nav bg (dark tint)
ACCENT_GLOW= "#4f8ef7"          # focus ring
DANGER     = "#f87171"          # red
DANGER_HOV = "#ef4444"
DANGER_BG  = "#2d1515"          # dark red tint
SUCCESS    = "#4ade80"          # green
SUCCESS_HOV= "#22c55e"
SUCCESS_BG = "#122412"          # dark green tint
WARNING    = "#fbbf24"          # amber
WARNING_BG = "#2a1f06"          # dark amber tint
GOLD       = "#fbbf24"
INFO       = "#38bdf8"          # sky blue
INFO_BG    = "#0c2233"          # dark sky tint
PURPLE     = "#a78bfa"          # purple
PURPLE_BG  = "#1e1630"          # dark purple tint
WHITE      = "#e8eaed"          # "white" repurposed as primary text
PAD        = 16                 # base content padding
SIDEBAR_W  = 220                # sidebar width
TOPBAR_H   = 52                 # topbar height

# ── Fonts (initialised once after the first Tk root exists) ──────────────────
F_TITLE: tkfont.Font
F_BODY:  tkfont.Font
F_MONO:  tkfont.Font
F_SMALL: tkfont.Font
F_BOLD:  tkfont.Font
F_BIG:   tkfont.Font
F_H2:    tkfont.Font
F_LABEL: tkfont.Font            # small-caps uppercase label


def _init_fonts(root: tk.Misc) -> None:
    """Match HTML: body font-size 13.5px, font-family Segoe UI."""
    global F_TITLE, F_BODY, F_MONO, F_SMALL, F_BOLD, F_BIG, F_H2, F_LABEL
    # HTML body: 13.5px  ≈  10pt in Tkinter (Windows 96dpi: 1pt=1.33px)
    F_BODY  = tkfont.Font(root=root, family="Segoe UI",          size=10)
    # .topbar-title: 15px fw700 → size=11 bold
    F_TITLE = tkfont.Font(root=root, family="Segoe UI",          size=11, weight="bold")
    # monospace: Cascadia Code 11px → size=8
    F_MONO  = tkfont.Font(root=root, family="Cascadia Code",     size=8)
    # .nav-item / small labels: 13px → size=9-10
    F_SMALL = tkfont.Font(root=root, family="Segoe UI",          size=9)
    # .btn font-weight 600: size=10 bold
    F_BOLD  = tkfont.Font(root=root, family="Segoe UI",          size=10, weight="bold")
    # .stat-value: 26px fw800 → size=18 bold
    F_BIG   = tkfont.Font(root=root, family="Segoe UI",          size=18, weight="bold")
    # .section-title: 13px fw700 → size=9 bold
    F_H2    = tkfont.Font(root=root, family="Segoe UI",          size=9,  weight="bold")
    # .nav-group-label: 10.5px fw700 uppercase → size=8 bold
    F_LABEL = tkfont.Font(root=root, family="Segoe UI",          size=8,  weight="bold")
    try:
        import tkinter.font as _tf
        if "Cascadia Code" not in _tf.families(root):
            F_MONO = tkfont.Font(root=root, family="Consolas", size=8)
    except Exception:
        pass


# ── Widget helpers ────────────────────────────────────────────────────────────
def _frame(parent: tk.Widget, **kw) -> tk.Frame:
    return tk.Frame(parent, bg=kw.pop("bg", BG), **kw)


def _label(parent: tk.Widget, text: str = "", **kw) -> tk.Label:
    return tk.Label(parent, text=text,
                    bg=kw.pop("bg", BG), fg=kw.pop("fg", TEXT),
                    font=kw.pop("font", F_BODY), **kw)


def _btn(parent: tk.Widget, text: str, cmd: Callable,
         color: str = ACCENT, fg: str = WHITE, width: int = 0,
         small: bool = False) -> tk.Button:
    """Flat button — solid accent or ghost style for dark theme."""
    px = 10 if small else 14
    py = 4  if small else 7
    fnt = F_SMALL if small else F_BOLD
    if color == ACCENT:
        hov = ACCENT_HOV
    elif color in (SURFACE2, SURFACE3, BG):
        hov = SURFACE3
    else:
        hov = color
    b = tk.Button(
        parent, text=text, command=cmd,
        bg=color, fg=fg,
        activebackground=hov, activeforeground=fg,
        relief="flat", bd=0,
        padx=px, pady=py,
        font=fnt, cursor="hand2", width=width,
        highlightthickness=1,
        highlightbackground=BORDER,
    )
    def _enter(_e, h=hov):
        b.configure(bg=h)
    def _leave(_e, c=color):
        b.configure(bg=c)
    b.bind("<Enter>", _enter)
    b.bind("<Leave>", _leave)
    return b


def _entry(parent: tk.Widget, textvariable: tk.StringVar, **kw) -> tk.Entry:
    """Dark-themed input: bg surface-3, 1px border, focus ring accent."""
    return tk.Entry(
        parent, textvariable=textvariable,
        bg=SURFACE3, fg=TEXT, insertbackground=ACCENT,
        relief="flat", bd=0, font=F_MONO,
        highlightthickness=1,
        highlightbackground=BORDER2,
        highlightcolor=ACCENT,
        selectbackground=ACCENT_LIT, selectforeground=TEXT,
        **kw,
    )


def _sep(parent: tk.Widget, color: str = BORDER) -> tk.Frame:
    """HTML .divider: height 1px, background --border."""
    return tk.Frame(parent, bg=color, height=1)


# ── Upgrade popup ─────────────────────────────────────────────────────────────

def show_upgrade_popup(root: tk.Tk, feature_name: str = "",
                       required_role: str = "Pro") -> None:
    """Modal popup shown when a Trial user clicks a locked feature."""
    popup = tk.Toplevel(root)
    popup.title("Feature Locked")
    popup.resizable(False, False)
    popup.configure(bg=SURFACE)
    popup.grab_set()
    root.update_idletasks()
    px = root.winfo_x() + root.winfo_width()  // 2 - 220
    py = root.winfo_y() + root.winfo_height() // 2 - 120
    popup.geometry(f"440x240+{px}+{py}")

    tk.Label(popup, text="🔒",
             font=tkfont.Font(family="Segoe UI Emoji", size=26),
             bg=SURFACE, fg=WARNING).pack(pady=(20, 4))
    tk.Label(popup, text=f"This feature requires GhostConfig {required_role}",
             font=tkfont.Font(family="Segoe UI", size=12, weight="bold"),
             fg=TEXT, bg=SURFACE).pack()
    sub = (f'"{feature_name}" is not available on your current plan.'
           if feature_name else "Upgrade your license to access this feature.")
    tk.Label(popup, text=sub,
             font=tkfont.Font(family="Segoe UI", size=9),
             fg=TEXT_MUTED, bg=SURFACE, wraplength=380, justify="center").pack(pady=(6, 14))
    tk.Frame(popup, bg=BORDER, height=1).pack(fill="x", padx=24)
    btn_row = tk.Frame(popup, bg=SURFACE)
    btn_row.pack(pady=14)
    tk.Button(
        btn_row, text="Upgrade License",
        font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
        fg=WHITE, bg=ACCENT, activebackground=ACCENT_HOV, activeforeground=WHITE,
        relief="flat", bd=0, cursor="hand2", padx=16, pady=7,
        command=lambda: (popup.destroy(),
                         messagebox.showinfo("Upgrade",
                             "Register a new account with a Pro or Admin license key."))
    ).pack(side="left", padx=(0, 8))
    tk.Button(
        btn_row, text="Cancel",
        font=tkfont.Font(family="Segoe UI", size=10),
        fg=TEXT_MUTED, bg=SURFACE2, activebackground=BORDER, activeforeground=TEXT,
        relief="flat", bd=0, cursor="hand2", padx=16, pady=7,
        command=popup.destroy,
    ).pack(side="left")


# ── Locked-feature button ─────────────────────────────────────────────────────

def _locked_btn(parent: tk.Widget, text: str, permission: Permission,
                pm: PermissionManager, root: tk.Tk,
                real_command=None, color: str = ACCENT,
                fg: str = WHITE, small: bool = False) -> tk.Button:
    """
    Returns a normal _btn when *pm* grants *permission*, otherwise a dimmed
    locked button that opens the upgrade popup on click.
    """
    if pm.has_permission(permission):
        return _btn(parent, text, real_command or (lambda: None),
                    color=color, fg=fg, small=small)

    required = pm.required_role_label(permission)
    locked_text = f"🔒 {text}  [{required.upper()}]"
    feature = text.strip()

    px, py = (10, 5) if small else (16, 8)
    fnt = F_SMALL if small else F_BOLD

    def _on_click():
        show_upgrade_popup(root, feature_name=feature, required_role=required)

    b = tk.Button(
        parent, text=locked_text, command=_on_click,
        bg=SURFACE2, fg=TEXT_MUTED,
        activebackground=SURFACE2, activeforeground=TEXT_MUTED,
        relief="flat", bd=0, padx=px, pady=py,
        font=fnt, cursor="hand2",
        highlightthickness=1, highlightbackground=BORDER,
    )
    return b



def _card(parent: tk.Widget, **kw) -> tk.Frame:
    """HTML .card: bg white, border 1px --border, border-radius 12px (--radius-lg)."""
    return tk.Frame(
        parent, bg=SURFACE,
        highlightthickness=1, highlightbackground=BORDER,
        **kw,
    )


def _section_label(parent: tk.Widget, text: str,
                   bg: str = SURFACE) -> tk.Label:
    """HTML .section-title: 13px 700 uppercase --muted, border-bottom 1px."""
    return tk.Label(
        parent, text=text.upper(),
        bg=bg, fg=TEXT_MUTED,
        font=F_H2,
        anchor="w",
    )


def _apply_ttk_theme(root: tk.Misc) -> None:
    """Apply dark charcoal design-system colours to all ttk widgets."""
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".",
        background=SURFACE, foreground=TEXT,
        fieldbackground=SURFACE2, bordercolor=BORDER,
        troughcolor=BG, lightcolor=BORDER, darkcolor=BORDER,
        font=("Segoe UI", 9),
        relief="flat")
    # Combobox
    s.configure("TCombobox",
        selectbackground=ACCENT_LIT, selectforeground=TEXT,
        fieldbackground=SURFACE2, background=SURFACE2,
        foreground=TEXT, arrowcolor=TEXT_MUTED,
        bordercolor=BORDER2, lightcolor=BORDER2, darkcolor=BORDER2,
        relief="flat", padding=4)
    s.map("TCombobox",
        fieldbackground=[("readonly", SURFACE2), ("focus", SURFACE2)],
        foreground=[("readonly", TEXT)],
        background=[("readonly", SURFACE2)],
        bordercolor=[("focus", ACCENT)])
    # Scrollbar — dark thin thumb
    s.configure("TScrollbar",
        background=BORDER2, troughcolor=BG,
        arrowcolor=TEXT_MUTED2, bordercolor=BG,
        relief="flat", arrowsize=0, width=6)
    s.map("TScrollbar",
        background=[("active", TEXT_MUTED)])
    # Notebook
    s.configure("TNotebook",
        background=BG, bordercolor=BORDER,
        tabmargins=[0, 0, 0, 0])
    s.configure("TNotebook.Tab",
        background=SURFACE2, foreground=TEXT_MUTED,
        padding=[14, 7], bordercolor=BORDER,
        font=("Segoe UI", 9),
        relief="flat")
    s.map("TNotebook.Tab",
        background=[("selected", SURFACE)],
        foreground=[("selected", TEXT)],
        focuscolor=[("selected", ACCENT)])
    # Treeview — dark table style
    s.configure("Treeview",
        background=SURFACE, foreground=TEXT2,
        fieldbackground=SURFACE, rowheight=32,
        bordercolor=BORDER, relief="flat")
    s.configure("Treeview.Heading",
        background=SURFACE2, foreground=TEXT_MUTED,
        relief="flat", borderwidth=0,
        font=("Segoe UI", 8, "bold"),
        padding=(14, 10))
    s.map("Treeview",
        background=[("selected", ACCENT_DIM)],
        foreground=[("selected", ACCENT)])


# ── Sidebar Navigation ────────────────────────────────────────────────────────

class _NavItem:
    """
    Sidebar nav item matching HTML .nav-item exactly:
      - padding: 7px 12px 7px 14px   (top/bottom 7px, right 12px, left 14px)
      - margin: 1px 8px               (vertical 1px, horizontal 8px)
      - border-radius: 8px (--radius)
      - active: bg #dbeafe (--accent-light), color #2563eb (--accent)
      - active: border-left 3px solid --accent  (settings-dashboard style)
      - hover:  bg --surface-2 (#f7f8fa), color --text-2 (#374151)
      - normal: color --muted (#6b7280)
      - font: 13px (size=9-10) weight 500 normal / 600 bold when active
    """

    def __init__(self, parent: tk.Widget, icon: str, label: str,
                 on_click, index: int) -> None:
        self._active  = False
        self._hovered = False
        self._idx     = index
        self._click   = on_click

        # Outer row
        self._outer = tk.Frame(parent, bg=SURFACE, cursor="hand2")
        self._outer.pack(fill="x", padx=8, pady=1)

        # Left accent border — 3px
        self._bar = tk.Frame(self._outer, width=3, bg=SURFACE)
        self._bar.pack(side="left", fill="y")

        # Inner container
        self._inner = tk.Frame(self._outer, bg=SURFACE, cursor="hand2")
        self._inner.pack(side="left", fill="both", expand=True)

        # Icon label
        self._icon_lbl = tk.Label(
            self._inner, text=icon,
            font=tkfont.Font(family="Segoe UI Emoji", size=10),
            bg=SURFACE, fg=TEXT_MUTED,
            padx=9, pady=7, cursor="hand2",
        )
        self._icon_lbl.pack(side="left")

        # Label
        self._lbl = tk.Label(
            self._inner, text=label,
            font=tkfont.Font(family="Segoe UI", size=9),
            bg=SURFACE, fg=TEXT_MUTED,
            anchor="w", cursor="hand2",
        )
        self._lbl.pack(side="left", fill="x", expand=True, pady=7)

        for w in (self._outer, self._inner, self._icon_lbl, self._lbl, self._bar):
            w.bind("<Button-1>", lambda _e, i=index: on_click(i))
            w.bind("<Enter>",    lambda _e: self._on_enter())
            w.bind("<Leave>",    lambda _e: self._on_leave())

    # ── colour helpers ────────────────────────────────────────────────────
    def _bg(self) -> str:
        if self._active:  return ACCENT_DIM   # dark active tint
        if self._hovered: return SURFACE2     # slightly lighter on hover
        return SURFACE                        # base surface

    def _fg(self) -> str:
        if self._active:  return ACCENT       # accent when active
        if self._hovered: return TEXT2        # brighter text on hover
        return TEXT_MUTED                     # muted at rest

    def _bar_color(self) -> str:
        if self._active: return ACCENT        # 3px accent left border
        return SURFACE                        # hidden otherwise

    def _refresh(self) -> None:
        bg  = self._bg()
        fg  = self._fg()
        bar = self._bar_color()
        for w in (self._outer, self._inner, self._icon_lbl, self._lbl):
            w.configure(bg=bg)
        self._icon_lbl.configure(fg=fg)
        # Active: font-weight 600; normal: 500 (approximate with bold/normal)
        self._lbl.configure(fg=fg,
            font=tkfont.Font(family="Segoe UI", size=9,
                             weight="bold" if self._active else "normal"))
        self._bar.configure(bg=bar)

    def _on_enter(self) -> None:
        self._hovered = True
        self._refresh()

    def _on_leave(self) -> None:
        self._hovered = False
        self._refresh()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._refresh()


# ── Log queue (thread-safe) ───────────────────────────────────────────────────
_log_queue: queue.Queue = queue.Queue()


def _log(msg: str, level: str = "info") -> None:
    _log_queue.put((level, msg))


# ─────────────────────────────────────────────────────────────────────────────
# Auth Screen
# ─────────────────────────────────────────────────────────────────────────────

_CHANGELOG = f"""\
GhostConfig  v{CURRENT_VERSION}
Windows Only  |  Auto-Updates Enabled

  \u2022 Dashboard — live GUID, serial, adapter status
  \u2022 Spoofer — GUID / MAC / volume randomiser
  \u2022 Devices — detailed hardware component info
  \u2022 Task Manager — live process monitor
  \u2022 Auto-updates — detects new versions on startup
  \u2022 Settings — update channel, preferences
  \u2022 HMAC-SHA256 offline key validation
  \u2022 Auto .reg backup before every registry write
  \u2022 PyInstaller single-file exe (UAC)
"""

_SOCIAL = [("YouTube", "#ff0000"), ("Discord", "#5865f2"), ("Telegram", "#2ca5e0")]


class AuthScreen(tk.Toplevel):
    W, H = 820, 500

    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.result: dict | None = None
        self.title("GhostConfig — Sign In")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.grab_set()
        self._center()
        self._build()

    def _center(self) -> None:
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x  = (sw - self.W) // 2
        y  = (sh - self.H) // 2
        self.geometry(f"{self.W}x{self.H}+{x}+{y}")

    def _build(self) -> None:
        outer = _frame(self, bg=BG)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1, minsize=self.W // 2)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        left = _frame(outer, bg=BG)
        left.grid(row=0, column=0, sticky="nsew")
        self._build_left(left)

        right = _frame(outer, bg=SURFACE)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_right(right)

    def _build_left(self, parent: tk.Frame) -> None:
        # Logo / brand mark
        brand = _frame(parent, bg=BG)
        brand.pack(anchor="w", padx=44, pady=(38, 0))
        tk.Label(brand, text="◆ ",
                 font=tkfont.Font(family="Segoe UI", size=13),
                 fg=ACCENT_LIT, bg=BG).pack(side="left")
        tk.Label(brand, text="GhostConfig",
                 font=tkfont.Font(family="Segoe UI Semibold", size=17,
                                  weight="bold"),
                 fg=WHITE, bg=BG).pack(side="left")

        _label(parent, "Sign in or create an account to continue.",
               bg=BG, fg=TEXT_MUTED, font=F_SMALL
               ).pack(anchor="w", padx=44, pady=(5, 24))

        # Mode tabs
        tabs = _frame(parent, bg=BG)
        tabs.pack(fill="x", padx=44)
        self._mode = tk.StringVar(value="signin")
        self._tab_si  = self._make_tab(tabs, "Sign in",  "signin")
        self._tab_reg = self._make_tab(tabs, "Register", "register")
        self._tab_si.pack(side="left")
        self._tab_reg.pack(side="left", padx=(20, 0))

        _sep(parent, color=BORDER).pack(fill="x", padx=44, pady=(10, 0))

        self._status_var = tk.StringVar(value="")
        self._status_lbl = _label(parent, textvariable=self._status_var,
                                  bg=BG, fg=DANGER, font=F_SMALL)

        self._form = _frame(parent, bg=BG)
        self._form.pack(fill="x", padx=44, pady=(16, 0))

        self._build_signin_form()
        self._build_register_form()
        self._show_mode("signin")

        self._status_lbl.pack(anchor="w", padx=44, pady=(5, 0))

    def _make_tab(self, parent: tk.Frame, label: str, mode: str) -> tk.Label:
        lbl = tk.Label(parent, text=label, bg=BG, fg=TEXT_MUTED,
                       font=tkfont.Font(family="Segoe UI", size=10),
                       cursor="hand2")
        lbl.bind("<Button-1>", lambda _e, m=mode: self._show_mode(m))
        return lbl

    def _show_mode(self, mode: str) -> None:
        self._mode.set(mode)
        self._status_var.set("")
        _act = tkfont.Font(family="Segoe UI Semibold", size=10, weight="bold")
        _dim = tkfont.Font(family="Segoe UI",          size=10)
        if mode == "signin":
            self._tab_si.configure(fg=WHITE,       font=_act)
            self._tab_reg.configure(fg=TEXT_MUTED,  font=_dim)
            self._reg_frame.pack_forget()
            self._si_frame.pack(fill="x")
        else:
            self._tab_reg.configure(fg=WHITE,       font=_act)
            self._tab_si.configure(fg=TEXT_MUTED,   font=_dim)
            self._si_frame.pack_forget()
            self._reg_frame.pack(fill="x")

    def _field(self, parent: tk.Frame, icon: str, placeholder: str,
               show: str = "") -> tk.Entry:
        # Wrapper with focus-ring via highlightbackground
        row = tk.Frame(parent, bg=SURFACE2,
                       highlightthickness=1,
                       highlightbackground=BORDER,
                       highlightcolor=ACCENT_GLOW)
        row.pack(fill="x", pady=(0, 10))

        tk.Label(row, text=icon, bg=SURFACE2, fg=TEXT_MUTED,
                 font=tkfont.Font(family="Segoe UI", size=10),
                 padx=12, pady=11).pack(side="left")

        tk.Frame(row, bg=BORDER, width=1).pack(side="left", fill="y", pady=8)

        e = tk.Entry(row, bg=SURFACE2, fg=TEXT_MUTED,
                     insertbackground=ACCENT_LIT, relief="flat", bd=0,
                     font=F_BODY, show=show,
                     selectbackground=ACCENT_DIM, selectforeground=TEXT)
        e.insert(0, placeholder)
        e._ph = placeholder  # type: ignore[attr-defined]

        def _fi(_evt: tk.Event) -> None:
            if e.get() == e._ph:  # type: ignore[attr-defined]
                e.delete(0, "end"); e.configure(fg=TEXT)
            row.configure(highlightbackground=ACCENT_GLOW)

        def _fo(_evt: tk.Event) -> None:
            if not e.get():
                e.insert(0, e._ph); e.configure(fg=TEXT_MUTED)  # type: ignore[attr-defined]
            row.configure(highlightbackground=BORDER)

        e.bind("<FocusIn>",  _fi)
        e.bind("<FocusOut>", _fo)
        e.pack(side="left", fill="x", expand=True, ipady=9, padx=(10, 10))
        return e

    def _val(self, e: tk.Entry) -> str:
        v = e.get()
        return "" if v == e._ph else v  # type: ignore[attr-defined]

    def _build_signin_form(self) -> None:
        f = _frame(self._form, bg=BG)
        self._si_frame = f
        self._si_user = self._field(f, "⊙", "Username")
        self._si_pass = self._field(f, "◈", "Password", show="\u2022")
        self._si_key  = self._field(f, "◆", "License key  e.g. GHOST-XXXX-XXXX-XXXX-XXXX")
        br = _frame(f, bg=BG)
        br.pack(fill="x", pady=(8, 0))
        sb = tk.Button(br, text="Sign in  →", command=self._do_signin,
                       bg=ACCENT, fg=WHITE,
                       activebackground=ACCENT_HOV, activeforeground=WHITE,
                       relief="flat", bd=0,
                       font=tkfont.Font(family="Segoe UI Semibold", size=10,
                                        weight="bold"),
                       cursor="hand2", padx=22, pady=11,
                       highlightthickness=1, highlightbackground=ACCENT)
        sb.pack(side="right")
        sb.bind("<Enter>", lambda _e: sb.configure(
            bg=ACCENT_HOV, highlightbackground=ACCENT_LIT))
        sb.bind("<Leave>", lambda _e: sb.configure(
            bg=ACCENT, highlightbackground=ACCENT))
        f.bind_all("<Return>", lambda _e: self._do_signin()
                   if self._mode.get() == "signin" else None)

    def _build_register_form(self) -> None:
        f = _frame(self._form, bg=BG)
        self._reg_frame = f
        self._reg_user  = self._field(f, "⊙", "Username")
        self._reg_pass  = self._field(f, "◈", "Password",         show="\u2022")
        self._reg_pass2 = self._field(f, "◈", "Confirm password", show="\u2022")
        self._reg_key   = self._field(f, "◆", "License key  e.g. GHOST-XXXX-XXXX-XXXX-XXXX")
        br = _frame(f, bg=BG)
        br.pack(fill="x", pady=(8, 0))
        rb = tk.Button(br, text="Create account  →", command=self._do_register,
                       bg=SUCCESS, fg=BG,
                       activebackground=SUCCESS_HOV, activeforeground=BG,
                       relief="flat", bd=0,
                       font=tkfont.Font(family="Segoe UI Semibold", size=10,
                                        weight="bold"),
                       cursor="hand2", padx=22, pady=11,
                       highlightthickness=1, highlightbackground=SUCCESS)
        rb.pack(side="right")
        rb.bind("<Enter>", lambda _e: rb.configure(
            bg=SUCCESS_HOV, highlightbackground=SUCCESS_HOV))
        rb.bind("<Leave>", lambda _e: rb.configure(
            bg=SUCCESS, highlightbackground=SUCCESS))

    # ── Admin-key pattern: GHOST-XXXX-XXXX-XXXX-XXXX or legacy QA- prefix
    @staticmethod
    def _looks_like_admin_key(value: str) -> bool:
        """Return True if *value* looks like a GHOST/QA license key string."""
        v = value.strip().upper()
        return v.startswith("GHOST-") or v.startswith("QA-")

    def _do_signin(self) -> None:
        user = self._val(self._si_user)
        pw   = self._val(self._si_pass)
        key  = self._val(self._si_key)

        # ── Admin-key login: user pasted a GHOST key into the username field ──
        # Password field is intentionally ignored for this path; the key itself
        # is the credential.  Normal Trial/Pro keys are rejected server-side.
        if self._looks_like_admin_key(user):
            res = kg.login_admin_key(user)
            if res["ok"]:
                # Build a minimal session dict compatible with _run_auth
                res["raw_key"] = user.strip().upper()
                self.result = res
                self.destroy()
            else:
                self._err(res["error"])
            return

        # ── Regular username + password login ─────────────────────────────────
        if not user or not pw or not key:
            self._err("All fields are required."); return
        res = kg.login_user(user, pw, key)
        if res["ok"]:
            res["raw_key"] = key.strip().upper()
            self.result = res
            self.destroy()
        else:
            self._err(res["error"])

    def _do_register(self) -> None:
        user = self._val(self._reg_user)
        pw   = self._val(self._reg_pass)
        pw2  = self._val(self._reg_pass2)
        key  = self._val(self._reg_key)
        if not user or not pw or not pw2 or not key:
            self._err("All fields are required."); return
        if pw != pw2:
            self._err("Passwords do not match."); return
        res = kg.register_user(user, pw, key)
        if res["ok"]:
            self._status_var.set(f"Account created ({res['tier']}) — sign in now.")
            self._status_lbl.configure(fg=SUCCESS)
            self._show_mode("signin")
        else:
            self._err(res["error"])

    def _err(self, msg: str) -> None:
        self._status_var.set(msg)
        self._status_lbl.configure(fg=DANGER)

    def _build_right(self, parent: tk.Frame) -> None:
        _section_label(parent, "Release Notes", bg=SURFACE).pack(
            anchor="w", padx=24, pady=(20, 8))

        cl = scrolledtext.ScrolledText(
            parent, bg=SURFACE2, fg=TEXT_MUTED,
            font=tkfont.Font(family="Segoe UI", size=8),
            relief="flat", bd=0, wrap="word", highlightthickness=0,
            padx=14, pady=10)
        cl.insert("1.0", _CHANGELOG)
        cl.tag_configure("h", foreground=ACCENT_LIT,
                         font=tkfont.Font(family="Segoe UI Semibold",
                                          size=9, weight="bold"))
        cl.tag_add("h", "1.0", "3.0")
        cl.configure(state="disabled")
        cl.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        _section_label(parent, "Community", bg=SURFACE).pack(
            anchor="w", padx=24, pady=(0, 8))
        soc = _frame(parent, bg=SURFACE)
        soc.pack(fill="x", padx=20, pady=(0, 22))
        for label, color in _SOCIAL:
            b = tk.Button(soc, text=label,
                          bg=SURFACE2, fg=TEXT_MUTED,
                          activebackground=color, activeforeground=WHITE,
                          relief="flat", bd=0, padx=16, pady=8,
                          font=tkfont.Font(family="Segoe UI", size=8,
                                           weight="bold"),
                          cursor="hand2",
                          highlightthickness=1, highlightbackground=BORDER)
            b.pack(side="left", padx=(0, 8))
            b.bind("<Enter>", lambda _e, c=color, btn=b: btn.configure(
                bg=c, fg=WHITE, highlightbackground=c))
            b.bind("<Leave>", lambda _e, btn=b: btn.configure(
                bg=SURFACE2, fg=TEXT_MUTED, highlightbackground=BORDER))

    def _on_close(self) -> None:
        self.result = None
        self.master.destroy()
        sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# Update Dialog
# ─────────────────────────────────────────────────────────────────────────────

class UpdateDialog(tk.Toplevel):
    """
    Modal shown when a newer version is detected.
    • Non-mandatory: shows "Update Now" and "Later" buttons.
    • Mandatory:     shows only "Update Ghost" — cannot be dismissed.

    After "Update Now" is clicked:
      1. Downloads the new exe to a temp staging directory.
      2. Verifies SHA-256 (if supplied by the release).
      3. Launches ghost_updater.py with the current PID, old exe path, new exe path.
      4. Calls self._app.destroy() to exit this process.
    """

    def __init__(self, master: tk.Tk, release: dict) -> None:
        super().__init__(master)
        self._app    = master
        self._rel    = release
        self._mandatory = bool(release.get("mandatory"))

        self.title("GhostConfig Update")
        self.configure(bg=SURFACE)
        self.resizable(False, False)
        self.grab_set()
        if self._mandatory:
            self.protocol("WM_DELETE_WINDOW", lambda: None)   # block close
        else:
            self.protocol("WM_DELETE_WINDOW", self._later)

        version  = release.get("version", "Unknown")
        notes    = release.get("releaseNotes") or []

        # ── Layout ────────────────────────────────────────────────────────
        # Header
        hdr = tk.Frame(self, bg=ACCENT, height=4)
        hdr.pack(fill="x")

        body = tk.Frame(self, bg=SURFACE, padx=24, pady=20)
        body.pack(fill="both", expand=True)

        if self._mandatory:
            tk.Label(body, text="Update Required",
                     font=tkfont.Font(family="Segoe UI", size=14, weight="bold"),
                     fg=DANGER, bg=SURFACE).pack(anchor="w")
            tk.Label(body,
                     text="This version of Ghost is no longer supported.\nPlease update to continue.",
                     font=tkfont.Font(family="Segoe UI", size=9),
                     fg=TEXT_MUTED, bg=SURFACE, justify="left").pack(anchor="w", pady=(4, 12))
        else:
            tk.Label(body, text="Ghost Update Available",
                     font=tkfont.Font(family="Segoe UI", size=14, weight="bold"),
                     fg=TEXT, bg=SURFACE).pack(anchor="w")
            tk.Label(body, text=f"Version  {version}",
                     font=tkfont.Font(family="Segoe UI", size=10),
                     fg=ACCENT, bg=SURFACE).pack(anchor="w", pady=(4, 12))

        if notes:
            tk.Label(body, text="What's New",
                     font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                     fg=TEXT_MUTED, bg=SURFACE).pack(anchor="w")
            notes_frame = tk.Frame(body, bg=SURFACE2,
                                   highlightthickness=1, highlightbackground=BORDER)
            notes_frame.pack(fill="x", pady=(4, 14))
            for note in notes[:8]:
                tk.Label(notes_frame, text=f"  •  {note}",
                         font=tkfont.Font(family="Segoe UI", size=9),
                         fg=TEXT2, bg=SURFACE2, anchor="w", wraplength=380, justify="left",
                         padx=8, pady=3).pack(fill="x")

        # Progress area (hidden until download starts)
        self._prog_frame = tk.Frame(body, bg=SURFACE)
        self._prog_frame.pack(fill="x", pady=(0, 8))
        self._prog_lbl = tk.Label(self._prog_frame, text="",
                                  font=tkfont.Font(family="Segoe UI", size=9),
                                  fg=TEXT_MUTED, bg=SURFACE)
        self._prog_lbl.pack(anchor="w")
        self._prog_bar = tk.Canvas(self._prog_frame, height=6, bg=SURFACE3,
                                   bd=0, highlightthickness=1,
                                   highlightbackground=BORDER, width=380)

        # Error label
        self._err_lbl = tk.Label(body, text="",
                                 font=tkfont.Font(family="Segoe UI", size=9),
                                 fg=DANGER, bg=SURFACE, wraplength=380, justify="left")
        self._err_lbl.pack(anchor="w")

        _sep(body, color=BORDER).pack(fill="x", pady=(8, 12))

        # Buttons
        btn_row = tk.Frame(body, bg=SURFACE)
        btn_row.pack(fill="x")

        update_label = "Update Ghost" if self._mandatory else "Update Now"
        self._update_btn = tk.Button(
            btn_row, text=update_label,
            font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
            fg=WHITE, bg=ACCENT, activebackground=ACCENT_HOV, activeforeground=WHITE,
            relief="flat", bd=0, cursor="hand2", padx=16, pady=8,
            command=self._start_update,
        )
        self._update_btn.pack(side="left")

        if not self._mandatory:
            tk.Button(
                btn_row, text="Later",
                font=tkfont.Font(family="Segoe UI", size=10),
                fg=TEXT_MUTED, bg=SURFACE2,
                activebackground=BORDER, activeforeground=TEXT,
                relief="flat", bd=0, cursor="hand2", padx=16, pady=8,
                command=self._later,
            ).pack(side="left", padx=(8, 0))

        # Centre the dialog
        self.update_idletasks()
        w, h = 440, self.winfo_reqheight() + 20
        self.geometry(f"{w}x{h}")
        px = master.winfo_x() + (master.winfo_width()  - w) // 2
        py = master.winfo_y() + (master.winfo_height() - h) // 2
        self.geometry(f"+{px}+{py}")

    def _later(self) -> None:
        self.destroy()

    def _set_error(self, msg: str) -> None:
        self._err_lbl.configure(text=msg)
        self._update_btn.configure(state="normal", text="Retry")

    def _set_progress(self, msg: str, pct: int | None = None) -> None:
        self._prog_lbl.configure(text=msg)
        self._prog_bar.pack(fill="x", pady=(4, 0))
        if pct is not None:
            self._prog_bar.delete("all")
            width = self._prog_bar.winfo_width() or 380
            filled = int(width * pct / 100)
            self._prog_bar.create_rectangle(0, 0, filled, 6, fill=ACCENT, outline="")

    def _start_update(self) -> None:
        self._update_btn.configure(state="disabled", text="Downloading…")
        self._err_lbl.configure(text="")
        threading.Thread(target=self._download_and_apply, daemon=True).start()

    def _download_and_apply(self) -> None:
        rel      = self._rel
        url      = rel.get("downloadUrl", "")
        filename = rel.get("filename", "GhostConfig.exe")
        expected_sha256 = (rel.get("sha256") or "").strip().lower()
        version  = rel.get("version", "unknown")

        if not url.startswith("https://"):
            self.after(0, lambda: self._set_error(
                "Update URL must use HTTPS. Aborting for security."))
            return

        # Validate filename — must end with .exe, no path separators
        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".exe") or "/" in filename or "\\" in filename:
            self.after(0, lambda: self._set_error("Invalid update filename. Aborting."))
            return

        # Staging directory — a subdirectory of the system temp folder
        stage_dir = Path(tempfile.gettempdir()) / "ghost_update_staging"
        stage_dir.mkdir(parents=True, exist_ok=True)
        staged = stage_dir / safe_name

        # ── Download ─────────────────────────────────────────────────────
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"GhostConfig/{CURRENT_VERSION}"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                total      = int(resp.headers.get("Content-Length", 0) or 0)
                done       = 0
                chunk_size = 65536
                t_start    = time.monotonic()
                with open(staged, "wb") as fh:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        elapsed = time.monotonic() - t_start or 0.001
                        speed   = done / elapsed          # bytes/s
                        speed_k = speed / 1024

                        if total:
                            pct = int(done * 100 / total)
                            remaining = ((total - done) / speed) if speed > 0 else 0
                            eta_s = int(remaining)
                            mb_done  = done / 1_048_576
                            mb_total = total / 1_048_576
                            msg = (
                                f"Downloading…  {pct}%  "
                                f"({mb_done:.1f} / {mb_total:.1f} MB)  "
                                f"{speed_k:.0f} KB/s  "
                                f"ETA {eta_s}s"
                            )
                            self.after(0, lambda p=pct, m=msg: self._set_progress(m, p))
                        else:
                            kb_done = done / 1024
                            msg = f"Downloading…  {kb_done:.0f} KB  ({speed_k:.0f} KB/s)"
                            self.after(0, lambda m=msg: self._set_progress(m))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._set_error(f"Download failed: {e}"))
            return

        # ── SHA-256 verification ─────────────────────────────────────────
        self.after(0, lambda: self._set_progress("Verifying…", None))
        actual_sha256 = _sha256_file(staged)

        if expected_sha256 and actual_sha256 != expected_sha256:
            try:
                staged.unlink()
            except Exception:
                pass
            self.after(0, lambda: self._set_error(
                "SHA-256 mismatch — download may be corrupted or tampered. "
                "Update aborted. The old version remains intact."))
            return

        # ── Locate updater script and current exe ────────────────────────
        if getattr(sys, "frozen", False):
            current_exe = Path(sys.executable).resolve()
            # Look for ghost_updater.exe alongside the exe first, then the .py
            updater_exe = current_exe.parent / "ghost_updater.exe"
            updater_py  = current_exe.parent / "ghost_updater.py"
        else:
            current_exe = Path(sys.argv[0]).resolve()
            updater_exe = Path(__file__).parent / "ghost_updater.exe"
            updater_py  = Path(__file__).parent / "ghost_updater.py"

        if updater_exe.exists():
            updater_cmd = [str(updater_exe)]
        elif updater_py.exists():
            updater_cmd = [sys.executable, str(updater_py)]
        else:
            self.after(0, lambda: self._set_error(
                "ghost_updater.py not found next to the exe. Cannot apply update."))
            return

        pid = os.getpid()
        backup_path = current_exe.with_suffix(".exe.bak")

        cmd = updater_cmd + [
            str(pid),
            str(current_exe),
            str(staged),
            "--backup", str(backup_path),
        ]

        self.after(0, lambda: self._set_progress("Applying update…", 100))

        try:
            subprocess.Popen(
                cmd,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                              getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._set_error(
                f"Could not launch updater: {e}"))
            return

        # Notify server of download (best-effort, non-blocking)
        def _notify():
            try:
                if _API_BASE_URL:
                    req2 = urllib.request.Request(
                        f"{_API_BASE_URL}/api/releases/downloaded",
                        data=json.dumps({"version": version}).encode("utf-8"),
                        headers={"Content-Type": "application/json",
                                 "User-Agent": f"GhostConfig/{CURRENT_VERSION}"},
                    )
                    urllib.request.urlopen(req2, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_notify, daemon=True).start()

        # Exit so the updater can replace the exe
        self.after(300, lambda: self._app.destroy())


# ─────────────────────────────────────────────────────────────────────────────
# Main Application  (single Tk root, tabbed)
# ─────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # Catch ALL exceptions thrown inside Tk callbacks / event handlers.
        self.report_callback_exception = self._on_tk_error
        self.withdraw()
        self.title("GhostConfig — System Profile Manager")
        self.configure(bg=BG)
        self.minsize(980, 680)
        self.geometry("1180x760")
        _init_fonts(self)
        _apply_ttk_theme(self)
        self._auth_result: dict | None = None
        self._nav_items: list[_NavItem] = []
        self._page_frames: list[tk.Frame] = []
        self._page_canvases: dict[int, tk.Canvas] = {}   # page_index -> scrollable canvas
        self._active_page: int = 0
        self.after(0, self._run_auth)

    def _on_tk_error(self, exc_type, exc_val, exc_tb) -> None:
        """Tk calls this for every unhandled exception in a callback."""
        import traceback as _tb
        _crash_log(exc_val)

    # ── auth phase ────────────────────────────────────────────────────────
    def _run_auth(self) -> None:
        try:
            screen = AuthScreen(self)
        except Exception as exc:
            _crash_log(exc)
            self.destroy()
            sys.exit(1)
        self.wait_window(screen)
        if screen.result is None:
            self.destroy(); sys.exit(0)
        self._auth_result = screen.result
        # ── Build PermissionManager from the validated keygen tier ────────
        tier = self._auth_result.get("tier", "TRIAL")
        key_map = {"TRIAL": "GHOST-TRIAL-2025", "PRO": "GHOST-PRO-2025",
                   "ADMIN": "GHOST-ADMIN-2025"}
        self._pm = PermissionManager({"license_key": key_map.get(tier, "GHOST-TRIAL-2025")})
        try:
            self._check_admin_then_build()
        except Exception as exc:
            _crash_log(exc)
            self.destroy()
            sys.exit(1)

    def _check_admin_then_build(self) -> None:
        # Always proceed — UAC manifest (uac_admin=True) already elevated the exe.
        # Skip the IsUserAnAdmin check: on some Windows configs it returns 0 even
        # when the process IS elevated, causing an unnecessary re-launch loop.
        self._build_ui()
        self._start_log_pump()
        self.deiconify()
        # Kick off a background update check 2 s after the UI appears
        self.after(2000, self._async_update_check)

    def _async_update_check(self) -> None:
        """Background thread: fetch latest release, compare versions.
        Respects silent_updates from server settings and minVersion guard.
        """
        settings = _load_update_settings()
        channel  = settings.get("channel", "stable")

        def _worker():
            # Fetch server settings (silent_updates) and latest release in parallel
            client_cfg = _fetch_client_settings()
            release    = _fetch_latest_release(channel)
            if release is None:
                return

            latest     = release.get("version", "0.0.0")
            min_ver    = (release.get("minVersion") or "").strip()
            mandatory  = bool(release.get("mandatory", False))
            silent     = bool(client_cfg.get("silent_updates", False))

            # Must be newer than current version
            if _semver_tuple(latest) <= _semver_tuple(CURRENT_VERSION):
                return

            # minVersion: if our version is older than minVersion the update is
            # effectively required (clients cannot keep using this version).
            if min_ver and _semver_tuple(CURRENT_VERSION) < _semver_tuple(min_ver):
                release["mandatory"] = True

            if silent:
                # Silent mode: download and apply immediately, no dialog
                self.after(0, lambda r=release: self._silent_update(r))
            else:
                self.after(0, lambda r=release: self._show_update_dialog(r))

        threading.Thread(target=_worker, daemon=True).start()

    def _silent_update(self, release: dict) -> None:
        """Silently download, verify, and apply an update — no UI prompts."""
        _log("[update] Silent update starting…", "info")
        threading.Thread(
            target=self._run_silent_update,
            args=(release,),
            daemon=True,
        ).start()

    def _run_silent_update(self, release: dict) -> None:
        """Worker thread that performs a complete silent update."""
        url             = release.get("downloadUrl", "")
        filename        = release.get("filename", "GhostConfig.exe")
        expected_sha256 = (release.get("sha256") or "").strip().lower()
        version         = release.get("version", "unknown")

        if not url.startswith("https://"):
            _log("[update] Silent update aborted: download URL is not HTTPS.", "warn")
            return

        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".exe"):
            _log("[update] Silent update aborted: invalid filename.", "warn")
            return

        stage_dir = Path(tempfile.gettempdir()) / "ghost_update_staging"
        stage_dir.mkdir(parents=True, exist_ok=True)
        staged = stage_dir / safe_name

        # Download
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"GhostConfig/{CURRENT_VERSION}"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(staged, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
        except Exception as exc:
            _log(f"[update] Silent download failed: {exc}", "warn")
            return

        # Verify SHA-256
        actual_sha256 = _sha256_file(staged)
        if expected_sha256 and actual_sha256 != expected_sha256:
            try:
                staged.unlink()
            except Exception:
                pass
            _log("[update] Silent update aborted: SHA-256 mismatch.", "warn")
            return

        # Locate updater
        if getattr(sys, "frozen", False):
            current_exe = Path(sys.executable).resolve()
            updater_exe = current_exe.parent / "ghost_updater.exe"
            updater_py  = current_exe.parent / "ghost_updater.py"
        else:
            current_exe = Path(sys.argv[0]).resolve()
            updater_exe = Path(__file__).parent / "ghost_updater.exe"
            updater_py  = Path(__file__).parent / "ghost_updater.py"

        if updater_exe.exists():
            updater_cmd = [str(updater_exe)]
        elif updater_py.exists():
            updater_cmd = [sys.executable, str(updater_py)]
        else:
            _log("[update] ghost_updater not found — silent update aborted.", "warn")
            return

        pid         = os.getpid()
        backup_path = current_exe.with_suffix(".exe.bak")
        cmd         = updater_cmd + [str(pid), str(current_exe), str(staged),
                                     "--backup", str(backup_path)]

        # Notify server
        try:
            if _API_BASE_URL:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{_API_BASE_URL}/api/releases/downloaded",
                        data=json.dumps({"version": version}).encode(),
                        headers={"Content-Type": "application/json",
                                 "User-Agent": f"GhostConfig/{CURRENT_VERSION}"},
                    ), timeout=5)
        except Exception:
            pass

        try:
            subprocess.Popen(
                cmd,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                              getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
        except Exception as exc:
            _log(f"[update] Silent update: could not launch updater: {exc}", "warn")
            return

        # Destroy the main window so the updater can replace the exe
        self.after(300, lambda: self.destroy())

    def _show_update_dialog(self, release: dict) -> None:
        try:
            UpdateDialog(self, release)
        except Exception as exc:
            _log(f"[update] Could not show update dialog: {exc}", "warn")

    def _manual_update_check(self) -> None:
        """Called from the Settings tab 'Check for Updates' button."""
        if hasattr(self, "_upd_latest_var"):
            self._upd_latest_var.set("Checking…")
        settings = _load_update_settings()
        channel  = settings.get("channel", "stable")

        def _worker():
            release = _fetch_latest_release(channel)
            def _done():
                if release is None:
                    if hasattr(self, "_upd_latest_var"):
                        self._upd_latest_var.set("Could not reach server")
                    return
                latest = release.get("version", "—")
                if hasattr(self, "_upd_latest_var"):
                    self._upd_latest_var.set(latest)
                if _semver_tuple(latest) > _semver_tuple(CURRENT_VERSION):
                    self._show_update_dialog(release)
                else:
                    messagebox.showinfo("GhostConfig", "You are running the latest version.",
                                        parent=self)
            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Main shell layout  (HTML: .app > .sidebar + .main > .topbar + .content)
    def _build_ui(self) -> None:
        meta     = self._auth_result
        tier     = meta.get("tier", "")
        username = meta.get("username", "?")
        expiry_str = str(meta.get("expiry") or "Never")
        tier_color = {"TRIAL": WARNING, "PRO": ACCENT, "ADMIN": GOLD}.get(tier, ACCENT)
        initials   = (username[:2].upper()) if username else "??"

        self._meta = meta

        # ────────────────────────────────────────────────────────────────
        # SIDEBAR  220px wide, border-right 1px --border, height 100vh
        # HTML: .sidebar { width: 220px; background: #fff; border-right: 1px solid #e5e7eb }
        # ────────────────────────────────────────────────────────────────
        sidebar = tk.Frame(self, bg=SURFACE, width=SIDEBAR_W)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # Right border of sidebar (1px --border)
        tk.Frame(self, bg=BORDER, width=1).pack(side="left", fill="y")

        # ── .sidebar-header  h=52px, border-bottom 1px, padding 0 16px ──
        sh = tk.Frame(sidebar, bg=SURFACE, height=TOPBAR_H)
        sh.pack(fill="x")
        sh.pack_propagate(False)
        _sep(sh, color=BORDER).pack(side="bottom", fill="x")

        # Logo icon box 28x28, bg --accent, border-radius 5px
        logo_box = tk.Frame(sh, bg=ACCENT, width=28, height=28)
        logo_box.place(x=16, rely=0.5, anchor="w")
        logo_box.pack_propagate(False)
        tk.Label(logo_box, text="G", font=tkfont.Font(family="Segoe UI", size=10,
                 weight="bold"), fg=WHITE, bg=ACCENT).place(relx=0.5, rely=0.5, anchor="center")

        title_f = tk.Frame(sh, bg=SURFACE)
        title_f.place(x=52, rely=0.5, anchor="w")
        tk.Label(title_f, text="GhostConfig",
                 font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                 fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(title_f, text="System Profile Manager",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).pack(anchor="w")

        # ── .sidebar-nav  flex:1, padding 8px 0 ──────────────────────────
        nav_frame = tk.Frame(sidebar, bg=SURFACE)
        nav_frame.pack(fill="both", expand=True, pady=8)

        # .nav-group-label "WORKSPACE" — font 10.5px 700 uppercase --muted-2
        tk.Label(nav_frame, text="WORKSPACE",
                 font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 fg=TEXT_MUTED2, bg=SURFACE, anchor="w"
                 ).pack(fill="x", padx=16, pady=(12, 4))

        # Page defs: (icon, label, build_method, args)
        _pages = [
            ("⊞",  "Dashboard",    self._build_dashboard,    ()),
            ("⟳",  "Spoofer",      self._build_spoofer,      ()),
            ("◫",  "Devices",      self._build_devices,      ()),
            ("▤",  "Task Manager", self._build_task_manager, ()),
        ]
        # .nav-group-label "SYSTEM"
        _system_pages = [
            ("⚙",  "Settings",     self._build_settings,     ()),
            ("◎",  "Support",      self._build_support,      ()),
        ]

        # Build content frames
        content = tk.Frame(self, bg=BG)
        content.pack(side="left", fill="both", expand=True)

        all_pages = _pages + _system_pages
        for i, (icon, label, build_fn, args) in enumerate(all_pages):
            frame = _frame(content, bg=BG)
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)
            build_fn(frame, *args)
            self._page_frames.append(frame)

        # Build nav items for workspace group
        for i, (icon, label, _fn, _args) in enumerate(_pages):
            item = _NavItem(nav_frame, icon, label, self._switch_page, i)
            self._nav_items.append(item)

        # System group label
        tk.Label(nav_frame, text="SYSTEM",
                 font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 fg=TEXT_MUTED2, bg=SURFACE, anchor="w"
                 ).pack(fill="x", padx=16, pady=(12, 4))

        for i, (icon, label, _fn, _args) in enumerate(_system_pages):
            item = _NavItem(nav_frame, icon, label, self._switch_page, len(_pages) + i)
            self._nav_items.append(item)

        # ── .sidebar-footer  border-top 1px, padding 10px 8px ────────────
        tk.Frame(sidebar, bg=BORDER, height=1).pack(side="bottom", fill="x")
        footer = tk.Frame(sidebar, bg=SURFACE)
        footer.pack(side="bottom", fill="x", padx=8, pady=10)

        # .avatar  28x28 circle, gradient bg (approximate with accent)
        av = tk.Frame(footer, bg=ACCENT, width=28, height=28)
        av.pack(side="left")
        av.pack_propagate(False)
        tk.Label(av, text=initials,
                 font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 fg=WHITE, bg=ACCENT).place(relx=0.5, rely=0.5, anchor="center")

        ui = tk.Frame(footer, bg=SURFACE)
        ui.pack(side="left", padx=(9, 0), fill="x", expand=True)
        tk.Label(ui, text=username,
                 font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                 fg=TEXT, bg=SURFACE, anchor="w").pack(anchor="w")
        tk.Label(ui, text=f"{tier}",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE, anchor="w").pack(anchor="w")

        # ────────────────────────────────────────────────────────────────
        # TOPBAR  height 52px, bg white, border-bottom 1px --border
        # HTML: .topbar { height: 52px; background: #fff; border-bottom: 1px }
        # padding: 0 20px; align-items: center; gap: 10px
        # ────────────────────────────────────────────────────────────────
        topbar = tk.Frame(content, bg=SURFACE, height=TOPBAR_H)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)
        _sep(topbar, color=BORDER).pack(side="bottom", fill="x")

        # .topbar-title  15px fw700 --text, letter-spacing -.015em
        self._topbar_title_var = tk.StringVar(value="Dashboard")
        tk.Label(topbar, textvariable=self._topbar_title_var,
                 font=tkfont.Font(family="Segoe UI", size=11, weight="bold"),
                 fg=TEXT, bg=SURFACE).pack(side="left", padx=(20, 0), pady=14)

        # .topbar-subtitle  12px --muted
        self._topbar_sub_var = tk.StringVar(value="")
        tk.Label(topbar, textvariable=self._topbar_sub_var,
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=SURFACE).pack(side="left", padx=(4, 0))

        # Expiry / user info in topbar right area
        exp_lbl = f"Expires: {expiry_str}"
        tk.Label(topbar, text=exp_lbl,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).pack(side="right", padx=(0, 20))

        # Status dot + status message
        self._status_var = tk.StringVar(value="Ready")
        inner_sbar = tk.Frame(topbar, bg=SURFACE)
        inner_sbar.pack(side="right", padx=(0, 10))
        dot = tk.Frame(inner_sbar, bg=SUCCESS, width=7, height=7)
        dot.pack(side="left", padx=(0, 5))
        dot.pack_propagate(False)
        tk.Label(inner_sbar, textvariable=self._status_var,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).pack(side="left")

        # Activate default page
        self._switch_page(0)

    # Page title / subtitle for topbar
    _PAGE_META = [
        ("Dashboard",    "System overview & live hardware identifiers"),
        ("Spoofer",      "Randomise hardware identifiers"),
        ("Devices",      "Detailed hardware component information"),
        ("Task Manager", "Live process monitor"),
        ("Settings",     "Application preferences and account"),
        ("Support",      "Help, documentation, and FAQ"),
    ]

    def _switch_page(self, index: int) -> None:
        """Show the page at *index* and update the sidebar active indicator."""
        for i, frame in enumerate(self._page_frames):
            frame.lower()
        self._page_frames[index].lift()
        for i, item in enumerate(self._nav_items):
            item.set_active(i == index)
        self._active_page = index
        # Update topbar title / subtitle
        if hasattr(self, "_topbar_title_var") and index < len(self._PAGE_META):
            title, sub = self._PAGE_META[index]
            self._topbar_title_var.set(title)
            self._topbar_sub_var.set(f"— {sub}")
        # Re-bind mouse-wheel to the active page's canvas (if it has one)
        canvas = self._page_canvases.get(index)
        if canvas:
            self.bind_all("<MouseWheel>",
                lambda e, c=canvas: c.yview_scroll(-1*(e.delta//120), "units"))

    # ─────────────────────────────────────────────────────────────────────
    # TAB 1 — Dashboard  (redesigned)
    # ─────────────────────────────────────────────────────────────────────
    def _build_dashboard(self, parent: tk.Frame) -> None:
        PAGE_IDX = 0
        meta = self._meta
        tier = meta.get("tier", "")
        key  = meta.get("raw_key", "—")
        tier_color = {"TRIAL": WARNING, "PRO": ACCENT, "ADMIN": GOLD}.get(tier, ACCENT)
        expiry_txt = str(meta.get("expiry") or "Never")

        # ── StringVars (backend-facing names must stay identical) ─────────
        self._dash_guid_var   = tk.StringVar(value="Loading…")
        self._dash_vol_var    = tk.StringVar(value="Loading…")
        self._dash_status_var = tk.StringVar(value="Active")
        if not hasattr(self, "_adapter_var"):
            self._adapter_var = tk.StringVar(value="—")

        # ── Outer scroll canvas ────────────────────────────────────────────
        outer_canvas = tk.Canvas(parent, bg=BG, bd=0, highlightthickness=0)
        outer_vsb    = ttk.Scrollbar(parent, orient="vertical",
                                     command=outer_canvas.yview)
        outer_canvas.configure(yscrollcommand=outer_vsb.set)
        outer_vsb.pack(side="right", fill="y")
        outer_canvas.pack(side="left", fill="both", expand=True)
        scroll_inner = tk.Frame(outer_canvas, bg=BG)
        _scroll_wid  = outer_canvas.create_window((0, 0), window=scroll_inner, anchor="nw")
        outer_canvas.bind("<Configure>",
            lambda e: outer_canvas.itemconfig(_scroll_wid, width=e.width))
        scroll_inner.bind("<Configure>",
            lambda _e: outer_canvas.configure(scrollregion=outer_canvas.bbox("all")))
        self._page_canvases[PAGE_IDX] = outer_canvas

        p = scroll_inner   # brevity alias

        # ── Page header row ────────────────────────────────────────────────
        hdr = tk.Frame(p, bg=BG)
        hdr.pack(fill="x", padx=PAD+12, pady=(PAD+8, 0))
        left_hdr = tk.Frame(hdr, bg=BG)
        left_hdr.pack(side="left")
        tk.Label(left_hdr, text="Dashboard",
                 font=tkfont.Font(family="Segoe UI Semibold", size=22, weight="bold"),
                 fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(left_hdr, text="System overview & live hardware identifiers",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=BG).pack(anchor="w", pady=(2, 0))
        # Tier pill
        tier_pill = tk.Frame(hdr, bg=ACCENT_DIM,
                             highlightthickness=1, highlightbackground=tier_color)
        tier_pill.pack(side="right", anchor="n", pady=(4, 0))
        tk.Label(tier_pill, text=f"  ● {tier}  ",
                 font=tkfont.Font(family="Segoe UI Semibold", size=8, weight="bold"),
                 fg=tier_color, bg=ACCENT_DIM, pady=5).pack()
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", padx=PAD+12, pady=(14, 0))

        # ── License hero card ──────────────────────────────────────────────
        self._build_license_card(p, key, tier, tier_color, expiry_txt)

        # ── 4-column stat strip ────────────────────────────────────────────
        _stat_defs = [
            ("Machine GUID",    self._dash_guid_var, ACCENT_HOV, "◎"),
            ("Volume Serial",   self._dash_vol_var,  "#a78bfa",  "◈"),
            ("Network Adapter", self._adapter_var,   INFO,       "⊞"),
            ("Spoof Status",    "Active",            SUCCESS,    "◆"),
        ]
        stats_row = tk.Frame(p, bg=BG)
        stats_row.pack(fill="x", padx=PAD+12, pady=(16, 0))
        for i in range(4):
            stats_row.columnconfigure(i, weight=1, uniform="sc")
        for col, (label, value, color, icon) in enumerate(_stat_defs):
            self._make_stat_mini_card(stats_row, icon, label, value, color, col)

        # ── 3 hardware detail cards ────────────────────────────────────────
        _card_defs = [
            ("◎", "Machine GUID",    self._dash_guid_var, ACCENT_HOV,
             lambda: self._dash_guid_var.get()),
            ("◈", "Volume Serial",   self._dash_vol_var,  "#a78bfa",
             lambda: self._dash_vol_var.get()),
            ("⊞", "Network Adapter", self._adapter_var,   INFO,
             lambda: self._adapter_var.get()),
        ]
        cards_row = tk.Frame(p, bg=BG)
        cards_row.pack(fill="x", padx=PAD+8, pady=(12, 4))
        for i in range(3):
            cards_row.columnconfigure(i, weight=1, uniform="dc")
        for col, (icon, label, value, color, copy_fn) in enumerate(_card_defs):
            self._make_modern_dash_card(cards_row, icon, label, value,
                                        color, copy_fn, col)

        # ── Quick-action row ───────────────────────────────────────────────
        qa_frame = tk.Frame(p, bg=BG)
        qa_frame.pack(fill="x", padx=PAD+12, pady=(14, 0))

        def _qa_btn(parent_f, icon, title, subtitle, color, cmd):
            card = tk.Frame(parent_f, bg=SURFACE2,
                            highlightthickness=1, highlightbackground=BORDER,
                            cursor="hand2")
            card.pack(side="left", padx=(0, 10), ipadx=0)
            top_strip = tk.Frame(card, bg=color, height=2)
            top_strip.pack(fill="x")
            inner = tk.Frame(card, bg=SURFACE2, padx=14, pady=10)
            inner.pack(fill="x")
            icon_l = tk.Label(inner, text=icon,
                              font=tkfont.Font(family="Segoe UI Emoji", size=12),
                              fg=color, bg=SURFACE2, cursor="hand2")
            icon_l.pack(side="left", padx=(0, 10))
            txt_f = tk.Frame(inner, bg=SURFACE2)
            txt_f.pack(side="left")
            tk.Label(txt_f, text=title,
                     font=tkfont.Font(family="Segoe UI Semibold", size=9, weight="bold"),
                     fg=TEXT, bg=SURFACE2, cursor="hand2").pack(anchor="w")
            tk.Label(txt_f, text=subtitle,
                     font=tkfont.Font(family="Segoe UI", size=7),
                     fg=TEXT_MUTED, bg=SURFACE2, cursor="hand2").pack(anchor="w")
            all_w = [card, top_strip, inner, icon_l, txt_f]
            def _e(_ev, aw=all_w, c=card, co=color):
                for w in aw: w.configure(bg=SURFACE3 if w is not top_strip else co)
                c.configure(highlightbackground=co)
            def _l(_ev, aw=all_w, c=card):
                for w in aw: w.configure(bg=SURFACE2 if w is not top_strip else color)
                c.configure(highlightbackground=BORDER)
            for w in all_w:
                w.bind("<Enter>", _e)
                w.bind("<Leave>", _l)
                w.bind("<Button-1>", lambda _ev, fn=cmd: fn())

        _qa_btn(qa_frame, "⟳", "Refresh Info",
                "Reload GUID, serial & adapter", ACCENT, self._dash_refresh)
        _qa_btn(qa_frame, "⊟", "Devices",
                "Browse hardware components", INFO,
                lambda: self._switch_page(2))
        _qa_btn(qa_frame, "⟳", "Spoofer",
                "Randomise hardware identifiers", DANGER,
                lambda: self._switch_page(1))
        # Timestamp label
        self._dash_refresh_lbl = tk.Label(
            qa_frame, text="",
            font=tkfont.Font(family="Segoe UI", size=8),
            fg=TEXT_MUTED, bg=BG)
        self._dash_refresh_lbl.pack(side="left", padx=16, anchor="s", pady=(0, 6))

        # ── Terminal log ───────────────────────────────────────────────────
        log_outer = tk.Frame(p, bg=BG)
        log_outer.pack(fill="both", expand=True, padx=28, pady=(16, 24))

        chrome = tk.Frame(log_outer, bg=SURFACE2,
                          highlightthickness=1, highlightbackground=BORDER)
        chrome.pack(fill="x")

        # Traffic-light dots
        dots = tk.Frame(chrome, bg=SURFACE2)
        dots.pack(side="left", padx=(12, 6), pady=10)
        for dot_color in ("#ef4444", "#f59e0b", "#22c55e"):
            tk.Label(dots, text="●",
                     font=tkfont.Font(family="Segoe UI", size=7),
                     fg=dot_color, bg=SURFACE2).pack(side="left", padx=2)
        tk.Label(chrome, text="Activity Log  —  GhostConfig Terminal",
                 font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 fg=TEXT_MUTED, bg=SURFACE2).pack(side="left", padx=(4, 0))

        # Search bar
        self._log_search_visible = False
        self._log_search_var     = tk.StringVar()
        self._log_search_frame   = tk.Frame(chrome, bg=SURFACE2)
        search_entry = tk.Entry(
            self._log_search_frame,
            textvariable=self._log_search_var,
            bg=BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, font=F_MONO, width=22,
            highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        search_entry.pack(side="left", ipady=4, padx=(0, 4))
        search_entry.bind("<Return>", lambda _e: self._log_search_next())
        tk.Label(self._log_search_frame, text="find:",
                 font=F_SMALL, fg=TEXT_MUTED, bg=SURFACE2).pack(
                     side="left", before=search_entry)

        def _mk_chrome_btn(text: str, cmd, fg_col: str = TEXT_MUTED):
            b = tk.Label(chrome, text=text,
                         font=tkfont.Font(family="Segoe UI", size=8),
                         fg=fg_col, bg=SURFACE2, padx=8, pady=8, cursor="hand2")
            b.pack(side="right")
            b.bind("<Enter>",    lambda _e: b.configure(fg=WHITE))
            b.bind("<Leave>",    lambda _e: b.configure(fg=fg_col))
            b.bind("<Button-1>", lambda _e: cmd())
            return b

        _mk_chrome_btn("✕ Clear",      self._clear_log,       DANGER)
        _mk_chrome_btn("⎘ Copy Log",   self._copy_log)
        self._log_search_btn = _mk_chrome_btn("⌕ Search", self._toggle_log_search)
        _mk_chrome_btn("⏷ Scroll End", self._log_scroll_end)

        self._log_autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(
            chrome, text="Auto",
            variable=self._log_autoscroll,
            font=tkfont.Font(family="Segoe UI", size=8),
            fg=TEXT_MUTED, bg=SURFACE2,
            selectcolor=SURFACE2, activebackground=SURFACE2,
            activeforeground=TEXT, bd=0, highlightthickness=0,
            cursor="hand2").pack(side="right", padx=(0, 2))

        log_body = tk.Frame(log_outer, bg="#080b14",
                            highlightthickness=1, highlightbackground=BORDER)
        log_body.pack(fill="both", expand=True)

        self._log_gutter = tk.Text(
            log_body, width=4, bg="#0d0f1a", fg="#3d4466",
            font=F_MONO, relief="flat", bd=0,
            highlightthickness=0, state="disabled",
            padx=4, pady=6, selectbackground="#0d0f1a")
        self._log_gutter.pack(side="left", fill="y")
        tk.Frame(log_body, bg=BORDER, width=1).pack(side="left", fill="y")

        vsb_log = ttk.Scrollbar(log_body, orient="vertical")
        vsb_log.pack(side="right", fill="y")

        self._log_box = tk.Text(
            log_body,
            bg="#080b14", fg=TEXT, font=F_MONO,
            relief="flat", bd=0, highlightthickness=0,
            state="disabled", wrap="word",
            padx=10, pady=6,
            selectbackground=ACCENT_DIM,
            yscrollcommand=vsb_log.set)
        self._log_box.pack(side="left", fill="both", expand=True)
        vsb_log.configure(command=self._log_box.yview)

        _tag_styles = [
            ("info",    TEXT,       None,  False),
            ("ok",      SUCCESS,    None,  False),
            ("warn",    WARNING,    None,  False),
            ("error",   DANGER,     None,  False),
            ("section", ACCENT_HOV, None,  True),
            ("muted",   TEXT_MUTED, None,  False),
        ]
        for tag, fg_col, bg_col, bold in _tag_styles:
            kw: dict = {"foreground": fg_col}
            if bg_col:
                kw["background"] = bg_col
            if bold:
                kw["font"] = tkfont.Font(family="Consolas", size=9, weight="bold")
            self._log_box.tag_configure(tag, **kw)
        self._log_box.tag_configure("search_hi", background=GOLD, foreground=BG)

        def _on_log_scroll(*args):
            vsb_log.set(*args)
            self._sync_log_gutter()
        self._log_box.configure(yscrollcommand=_on_log_scroll)

        self._log_line_count = 0
        self.after(400, self._dash_refresh)

    # ── Stat mini-card (top row — icon + label + value pill) ──────────────────
    def _make_stat_mini_card(self, parent: tk.Frame, icon: str, label: str,
                             value, color: str, col: int) -> None:
        """Compact horizontal stat card: coloured icon box + label + live value."""
        card = tk.Frame(parent, bg=SURFACE2,
                        highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=0, column=col, padx=5, pady=0, sticky="nsew")

        # Thin top accent bar
        tk.Frame(card, bg=color, height=2).pack(fill="x")

        inner = tk.Frame(card, bg=SURFACE2, padx=12, pady=10)
        inner.pack(fill="x")

        icon_box = tk.Frame(inner, bg=ACCENT_DIM, width=30, height=30)
        icon_box.pack(side="left", padx=(0, 10))
        icon_box.pack_propagate(False)
        tk.Label(icon_box, text=icon,
                 font=tkfont.Font(family="Segoe UI Emoji", size=11),
                 fg=color, bg=ACCENT_DIM).place(relx=0.5, rely=0.5, anchor="center")

        right = tk.Frame(inner, bg=SURFACE2)
        right.pack(side="left", fill="x", expand=True)
        tk.Label(right, text=label,
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=TEXT_MUTED, bg=SURFACE2,
                 anchor="w").pack(anchor="w")
        if isinstance(value, tk.StringVar):
            tk.Label(right, textvariable=value,
                     font=tkfont.Font(family="Consolas", size=8, weight="bold"),
                     fg=color, bg=SURFACE2,
                     anchor="w", wraplength=140).pack(anchor="w", pady=(2, 0))
        else:
            tk.Label(right, text=str(value),
                     font=tkfont.Font(family="Consolas", size=8, weight="bold"),
                     fg=color, bg=SURFACE2,
                     anchor="w").pack(anchor="w", pady=(2, 0))

        def _enter(_e=None):
            card.configure(highlightbackground=color)
        def _leave(_e=None):
            card.configure(highlightbackground=BORDER)
        card.bind("<Enter>", _enter)
        card.bind("<Leave>", _leave)
        inner.bind("<Enter>", _enter)
        inner.bind("<Leave>", _leave)

    # ── Dashboard card helper ──────────────────────────────────────────────────
    def _make_modern_dash_card(
            self, parent: tk.Frame, icon: str, label: str,
            value, color: str, copy_fn, col: int) -> None:
        """Rounded-look info card with icon, live value, copy button, hover anim."""

        BG_CARD   = SURFACE2
        BG_HOVER  = SURFACE3

        card = tk.Frame(parent, bg=BG_CARD,
                        highlightthickness=1, highlightbackground=BORDER,
                        padx=0, pady=0, cursor="hand2")
        card.grid(row=0, column=col, padx=7, pady=4, sticky="nsew")

        # ── Top accent strip (colour per card) ────────────────────────────
        strip = tk.Frame(card, bg=color, height=3)
        strip.pack(fill="x")

        # ── Body ──────────────────────────────────────────────────────────
        body = tk.Frame(card, bg=BG_CARD, padx=14, pady=12)
        body.pack(fill="both", expand=True)

        # Row 1: icon + label + copy btn
        top_row = tk.Frame(body, bg=BG_CARD)
        top_row.pack(fill="x")

        icon_lbl = tk.Label(top_row, text=icon,
                            font=tkfont.Font(family="Segoe UI Emoji", size=13),
                            fg=color, bg=BG_CARD)
        icon_lbl.pack(side="left")

        label_lbl = tk.Label(top_row, text=f"  {label}",
                             font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                             fg=TEXT_MUTED, bg=BG_CARD)
        label_lbl.pack(side="left")

        copy_btn = tk.Label(top_row, text="⎘",
                            font=tkfont.Font(family="Segoe UI Emoji", size=9),
                            fg=TEXT_MUTED, bg=BG_CARD,
                            padx=6, cursor="hand2")
        copy_btn.pack(side="right")

        def _do_copy(_e=None):
            val = copy_fn()
            card.clipboard_clear()
            card.clipboard_append(val)
            copy_btn.configure(fg=SUCCESS)
            card.after(1200, lambda: copy_btn.configure(fg=TEXT_MUTED))

        copy_btn.bind("<Button-1>", _do_copy)
        copy_btn.bind("<Enter>", lambda _e: copy_btn.configure(fg=WHITE))
        copy_btn.bind("<Leave>", lambda _e: copy_btn.configure(fg=TEXT_MUTED))

        # Row 2: value
        if isinstance(value, tk.StringVar):
            val_lbl = tk.Label(body, textvariable=value,
                               font=tkfont.Font(family="Consolas", size=10,
                                                weight="bold"),
                               fg=color, bg=BG_CARD,
                               wraplength=180, justify="left",
                               anchor="w")
        else:
            val_lbl = tk.Label(body, text=str(value),
                               font=tkfont.Font(family="Consolas", size=10,
                                                weight="bold"),
                               fg=color, bg=BG_CARD,
                               wraplength=180, justify="left",
                               anchor="w")
        val_lbl.pack(fill="x", pady=(8, 0))

        # ── Hover animation ───────────────────────────────────────────────
        all_widgets = [card, body, top_row, icon_lbl, label_lbl, val_lbl]

        def _enter(_e=None):
            for w in all_widgets:
                w.configure(bg=BG_HOVER)
            card.configure(highlightbackground=color)

        def _leave(_e=None):
            for w in all_widgets:
                w.configure(bg=BG_CARD)
            card.configure(highlightbackground=BORDER)

        for w in all_widgets:
            w.bind("<Enter>", _enter)
            w.bind("<Leave>", _leave)

    # ── Ghost key display formatter ───────────────────────────────────────────
    @staticmethod
    def _ghost_key_display(raw_key: str, tier: str) -> str:
        """
        Reformat the raw GHOST-XXXXX-XXXXX-XXXXX-XXXXX key into Ghost branding.

        Strategy (display-only — raw_key is never modified):
          • Extract the 4 payload segments (drop the "GHOST-" or legacy "QA-" prefix).
          • Rebuild as  GHOST-<seg1>-<seg2>-<seg3>-<seg4>  for TRIAL/unknown
                        GHOST-PRO-<seg1>-<seg2>-<seg3>      for PRO   (3 payload segs)
                        GHOST-ADM-<seg1>-<seg2>-<seg3>      for ADMIN (3 payload segs)
          • Each segment is uppercased; the key is never re-validated.
        """
        # raw_key format: "GHOST-AAAAA-BBBBB-CCCCC-DDDDD" (or legacy "QA-…")
        # strip leading prefix
        clean = raw_key.strip().upper()
        parts = [s for s in clean.split("-") if s]

        # remove leading "GHOST" or legacy "QA" prefix token if present
        if parts and parts[0] in ("GHOST", "QA"):
            parts = parts[1:]

        # pad or trim so we always have 4 segments to work with
        while len(parts) < 4:
            parts.append("XXXXX")
        parts = parts[:4]
        s1, s2, s3, s4 = parts

        if tier == "PRO":
            return f"GHOST-PRO-{s1}-{s2}-{s3}"
        elif tier == "ADMIN":
            return f"GHOST-ADM-{s1}-{s2}-{s3}"
        else:
            # TRIAL or unknown — show all four segments
            return f"GHOST-{s1}-{s2}-{s3}-{s4}"

    # ── Premium license hero card ─────────────────────────────────────────────
    def _build_license_card(
            self, parent: tk.Frame,
            raw_key: str, tier: str,
            tier_color: str, expiry_txt: str) -> None:
        """
        Full-width premium license card.  Displays:
          - Ghost-branded display key
          - Copy button (copies the *raw* key so it remains valid)
          - Status badge  (ACTIVE / EXPIRED)
          - Expiration badge
          - Role badge
        No backend data is read or written here.
        """
        # ── colour shortcuts ──────────────────────────────────────────────
        CARD_BG    = "#101428"   # slightly lighter than page BG for depth
        CARD_INNER = "#141830"
        GLOW_CLR   = tier_color  # accent border matches tier

        # Determine status
        import datetime as _dt
        is_expired = False
        if expiry_txt.lower() not in ("never", "—", ""):
            try:
                exp_date = _dt.datetime.strptime(expiry_txt, "%Y-%m-%d").date()
                is_expired = exp_date < _dt.date.today()
            except ValueError:
                pass
        status_text  = "EXPIRED" if is_expired else "ACTIVE"
        status_color = DANGER    if is_expired else SUCCESS

        # Ghost-branded display key (pure visual)
        display_key = self._ghost_key_display(raw_key, tier)

        # ── Outer wrapper with glow border ───────────────────────────────
        outer = tk.Frame(
            parent, bg=CARD_BG,
            highlightthickness=1,
            highlightbackground=BORDER2,
        )
        outer.pack(fill="x", padx=28, pady=(20, 0))

        # Animated glow: border brightens to tier_color on hover
        def _card_enter(_e=None):
            outer.configure(highlightbackground=GLOW_CLR)
        def _card_leave(_e=None):
            outer.configure(highlightbackground=BORDER2)
        outer.bind("<Enter>", _card_enter)
        outer.bind("<Leave>", _card_leave)

        # ── Top accent bar (4 px, tier colour + animated shimmer via width) ──
        accent_bar = tk.Frame(outer, bg=tier_color, height=4)
        accent_bar.pack(fill="x")

        # ── Card body ─────────────────────────────────────────────────────
        body = tk.Frame(outer, bg=CARD_BG, padx=24, pady=20)
        body.pack(fill="x")

        # Row 1: icon + "LICENSE KEY" label + badges (right-aligned)
        row1 = tk.Frame(body, bg=CARD_BG)
        row1.pack(fill="x")

        # Left: icon + section label
        left1 = tk.Frame(row1, bg=CARD_BG)
        left1.pack(side="left")
        tk.Label(left1, text="◆",
                 font=tkfont.Font(family="Segoe UI", size=11),
                 fg=tier_color, bg=CARD_BG).pack(side="left")
        tk.Label(left1, text="  LICENSE KEY",
                 font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 fg=TEXT_MUTED, bg=CARD_BG).pack(side="left")

        # Right: status + expiry + role badges
        badges = tk.Frame(row1, bg=CARD_BG)
        badges.pack(side="right")

        def _badge(text: str, fg_col: str, bg_col: str, parent_frame=badges):
            f = tk.Frame(parent_frame, bg=bg_col,
                         highlightthickness=1, highlightbackground=bg_col)
            f.pack(side="left", padx=(6, 0))
            tk.Label(f, text=text,
                     font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                     fg=fg_col, bg=bg_col,
                     padx=8, pady=3).pack()
            return f

        # Status badge
        st_bg = "#0f2a1a" if not is_expired else "#2a0f0f"
        _badge(f"● {status_text}", status_color, st_bg)

        # Expiry badge
        exp_bg = "#1a1428"
        exp_label = f"EXPIRES {expiry_txt.upper()}" if expiry_txt.lower() not in ("never", "—", "") else "NO EXPIRY"
        _badge(exp_label, TEXT_MUTED, exp_bg)

        # Role badge
        role_bg = {"TRIAL": "#1a1508", "PRO": "#1a0f2e", "ADMIN": "#1a1408"}.get(tier, "#1a0f2e")
        _badge(tier, tier_color, role_bg)

        # Row 2: the key itself (large mono) + copy button
        row2 = tk.Frame(body, bg=CARD_BG)
        row2.pack(fill="x", pady=(16, 0))

        key_lbl = tk.Label(
            row2, text=display_key,
            font=tkfont.Font(family="Cascadia Code", size=18, weight="bold"),
            fg=WHITE, bg=CARD_BG,
            anchor="w",
        )
        key_lbl.pack(side="left")

        # Copy button — copies raw_key (the actual valid key, not display)
        copy_state = {"copied": False}
        copy_btn = tk.Frame(
            row2, bg=SURFACE2,
            highlightthickness=1, highlightbackground=BORDER,
            cursor="hand2",
        )
        copy_btn.pack(side="right", padx=(12, 0), anchor="center")
        copy_lbl = tk.Label(
            copy_btn, text="⎘  Copy Key",
            font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
            fg=TEXT_MUTED, bg=SURFACE2,
            padx=12, pady=7, cursor="hand2",
        )
        copy_lbl.pack()

        def _do_copy(_e=None):
            outer.clipboard_clear()
            outer.clipboard_append(raw_key)   # raw key — always valid
            copy_lbl.configure(text="✓  Copied!", fg=SUCCESS)
            copy_btn.configure(highlightbackground=SUCCESS)
            outer.after(1800, lambda: (
                copy_lbl.configure(text="⎘  Copy Key", fg=TEXT_MUTED),
                copy_btn.configure(highlightbackground=BORDER),
            ))

        for w in (copy_btn, copy_lbl):
            w.bind("<Button-1>", _do_copy)
            w.bind("<Enter>", lambda _e: (
                copy_btn.configure(highlightbackground=ACCENT_LIT),
                copy_lbl.configure(fg=WHITE),
            ))
            w.bind("<Leave>", lambda _e: (
                copy_btn.configure(highlightbackground=BORDER),
                copy_lbl.configure(fg=TEXT_MUTED),
            ))

        # Row 3: raw key hint (smaller, muted) + issuer tag
        row3 = tk.Frame(body, bg=CARD_BG)
        row3.pack(fill="x", pady=(10, 0))

        tk.Label(row3, text=f"  Raw: {raw_key}",
                 font=tkfont.Font(family="Cascadia Code", size=7),
                 fg=TEXT_MUTED, bg=CARD_BG).pack(side="left")

        tk.Label(row3, text="GhostConfig  •  Offline HMAC-SHA256",
                 font=tkfont.Font(family="Segoe UI", size=7),
                 fg=TEXT_MUTED, bg=CARD_BG).pack(side="right")

        # Bottom divider inside card
        tk.Frame(outer, bg=BORDER, height=1).pack(fill="x", padx=24)

        # ── Bind hover propagation to all interior frames ─────────────────
        for w in (body, row1, row2, row3, left1, badges, key_lbl):
            w.bind("<Enter>", _card_enter)
            w.bind("<Leave>", _card_leave)

    # ── Terminal log helpers ───────────────────────────────────────────────────
    def _sync_log_gutter(self) -> None:
        """Keep the line-number gutter in sync with the text widget."""
        self._log_gutter.configure(state="normal")
        self._log_gutter.delete("1.0", "end")
        total = int(self._log_box.index("end-1c").split(".")[0])
        nums  = "\n".join(f"{i:>3}" for i in range(1, total + 1))
        self._log_gutter.insert("1.0", nums)
        # Sync scroll position
        self._log_gutter.yview_moveto(self._log_box.yview()[0])
        self._log_gutter.configure(state="disabled")

    def _toggle_log_search(self) -> None:
        self._log_search_visible = not self._log_search_visible
        if self._log_search_visible:
            self._log_search_frame.pack(side="left", padx=(8, 0), pady=6)
        else:
            self._log_search_frame.pack_forget()
            self._log_box.tag_remove("search_hi", "1.0", "end")

    def _log_search_next(self) -> None:
        term = self._log_search_var.get()
        if not term:
            return
        self._log_box.tag_remove("search_hi", "1.0", "end")
        start = "1.0"
        while True:
            pos = self._log_box.search(term, start, stopindex="end",
                                       nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(term)}c"
            self._log_box.tag_add("search_hi", pos, end)
            start = end
        # Scroll to first hit
        first = self._log_box.search(term, "1.0", nocase=True)
        if first:
            self._log_box.see(first)

    def _log_scroll_end(self) -> None:
        self._log_box.see("end")

    def _copy_log(self) -> None:
        content = self._log_box.get("1.0", "end")
        self.clipboard_clear()
        self.clipboard_append(content)

    def _dash_refresh(self) -> None:
        # Stamp last-refresh time on the button's companion label
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._dash_refresh_lbl.configure(
            text=f"Last refreshed  {ts}")
        def _t():
            try:
                g = cu.read_machine_guid()
                self.after(0, lambda: self._dash_guid_var.set(g))
                _log(f"MachineGuid: {g}", "info")
            except Exception as e:
                self.after(0, lambda: self._dash_guid_var.set("Error"))
                _log(f"GUID error: {e}", "error")
            try:
                info = cu.get_volume_info("C:\\")
                self.after(0, lambda: self._dash_vol_var.set(info["serial_hex"]))
                _log(f"Volume C:\\: {info['serial_hex']}", "info")
            except Exception as e:
                self.after(0, lambda: self._dash_vol_var.set("Error"))
                _log(f"Volume error: {e}", "error")
        threading.Thread(target=_t, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────
    # TAB 2 — Spoofer  (hardware cards + Temp / Perm mode selector)
    # ─────────────────────────────────────────────────────────────────────
    def _build_spoofer(self, parent: tk.Frame) -> None:
        PAGE_IDX = 1
        # ── outer 2-col layout: cards left, log right ─────────────────────
        parent.columnconfigure(0, weight=3, minsize=420)
        parent.columnconfigure(1, weight=2, minsize=320)
        parent.rowconfigure(0, weight=1)

        left_outer  = _frame(parent, bg=BG)
        left_outer.grid(row=0, column=0, sticky="nsew")
        right_outer = _frame(parent, bg=BG)
        right_outer.grid(row=0, column=1, sticky="nsew")
        tk.Frame(parent, bg=BORDER, width=1).grid(row=0, column=0,
            sticky="nse", padx=(0,0))

        # ── left: scrollable card area ────────────────────────────────────
        canvas = tk.Canvas(left_outer, bg=BG, bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(left_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        wid   = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._page_canvases[PAGE_IDX] = canvas

        P = PAD

        # ── page header ───────────────────────────────────────────────────
        hdr_row = tk.Frame(inner, bg=BG)
        hdr_row.pack(fill="x", padx=P, pady=(P+4, 0))
        tk.Label(hdr_row, text="Spoofer",
                 font=tkfont.Font(family="Segoe UI Semibold", size=18, weight="bold"),
                 fg=TEXT, bg=BG).pack(side="left")
        tk.Label(hdr_row, text="  Randomise hardware identifiers",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=BG).pack(side="left", pady=(4, 0))
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=P, pady=(8, 0))

        # ── mode selector ─────────────────────────────────────────────────
        mode_card = tk.Frame(inner, bg=SURFACE2,
                             highlightthickness=1, highlightbackground=BORDER)
        mode_card.pack(fill="x", padx=P, pady=(10, 0))

        tk.Label(mode_card, text="  Spoof Mode",
                 font=tkfont.Font(family="Segoe UI Semibold", size=9, weight="bold"),
                 fg=TEXT_MUTED, bg=SURFACE2).pack(anchor="w", padx=12, pady=(10, 4))
        tk.Frame(mode_card, bg=BORDER, height=1).pack(fill="x", padx=12)

        mode_inner = tk.Frame(mode_card, bg=SURFACE2)
        mode_inner.pack(fill="x", padx=12, pady=10)

        self._spoof_mode = tk.StringVar(value="temp")

        _MODE_DEFS = [
            ("temp",
             "⏱  Temporary",
             "Changes reset on next reboot. Safe for testing.",
             WARNING),
            ("perm",
             "🔒  Permanent",
             "Changes survive reboots. Wrote to registry permanently.",
             DANGER),
        ]

        self._mode_btn_refs: dict[str, tk.Frame] = {}
        for mode_val, label, desc, color in _MODE_DEFS:
            btn_frame = tk.Frame(mode_inner, bg=SURFACE,
                                 highlightthickness=2,
                                 highlightbackground=BORDER,
                                 cursor="hand2")
            btn_frame.pack(side="left", padx=(0, 10), ipadx=4, ipady=4)
            self._mode_btn_refs[mode_val] = btn_frame

            icon_lbl = tk.Label(btn_frame, text=label,
                                font=tkfont.Font(family="Segoe UI Semibold",
                                                 size=10, weight="bold"),
                                fg=color, bg=SURFACE, padx=14, pady=8,
                                cursor="hand2")
            icon_lbl.pack(anchor="w")
            desc_lbl = tk.Label(btn_frame, text=desc,
                                font=tkfont.Font(family="Segoe UI", size=8),
                                fg=TEXT_MUTED, bg=SURFACE,
                                padx=14, pady=4, cursor="hand2",
                                wraplength=220, justify="left")
            desc_lbl.pack(anchor="w", pady=(0, 6))

            def _select(mv=mode_val, c=color):
                self._spoof_mode.set(mv)
                self._sp_update_mode_btns()
            for w in (btn_frame, icon_lbl, desc_lbl):
                w.bind("<Button-1>", lambda _e, fn=_select: fn())

        self._sp_update_mode_btns()

        # ── global action buttons ─────────────────────────────────────────
        glob_row = tk.Frame(inner, bg=BG)
        glob_row.pack(fill="x", padx=P, pady=(12, 0))

        if self._pm.has_permission(Permission.SPOOFER_ROTATE_GUID):
            spoof_all_btn = tk.Button(
                glob_row, text="  ⟳  SPOOF EVERYTHING  ",
                command=self._spoof_everything,
                bg=DANGER, fg=WHITE,
                activebackground=DANGER_HOV, activeforeground=WHITE,
                relief="flat", bd=0, padx=14, pady=11,
                font=tkfont.Font(family="Segoe UI Semibold", size=11,
                                 weight="bold"),
                cursor="hand2",
                highlightthickness=1, highlightbackground=DANGER,
            )
            spoof_all_btn.pack(side="left")
            spoof_all_btn.bind("<Enter>", lambda _e: spoof_all_btn.configure(
                bg=DANGER_HOV, highlightbackground=DANGER_HOV))
            spoof_all_btn.bind("<Leave>", lambda _e: spoof_all_btn.configure(
                bg=DANGER, highlightbackground=DANGER))

            restore_btn = _btn(glob_row, "↩  Restore Temps",
                               self._restore_all_temp,
                               color=SURFACE2, fg=TEXT_MUTED)
            restore_btn.pack(side="left", padx=(10, 0))
        else:
            _locked_btn(glob_row, "  ⟳  SPOOF EVERYTHING",
                        Permission.SPOOFER_ROTATE_GUID,
                        self._pm, self, color=DANGER).pack(side="left")

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=P, pady=(12, 0))

        # ── hardware cards ────────────────────────────────────────────────
        # Each card shows live device info + a per-card Spoof button.
        # _sp_hw_sections stores the same structure as _dev_sections so we
        # can reuse _dev_populate_section logic.
        self._sp_hw_sections: dict[str, dict] = {}
        self._adapter_map:    dict[str, str]  = {}
        # Ensure _dev_sections exists before any sp_make_hw_card call registers
        # into it — Devices tab may not have built yet at this point.
        if not hasattr(self, "_dev_sections"):
            self._dev_sections = {}

        _SP_DEFS: list[tuple[str, str, str, list[str],
                             "Callable", str]] = [
            # (key, icon, title, display_fields, spoof_fn, spoof_label)
            ("motherboard", "⊞", "Motherboard",
             ["Manufacturer","Product Name","Serial Number","UUID","Form Factor"],
             self._sp_spoof_uuid,        "Spoof UUID"),
            ("bios",        "◈", "BIOS / Firmware",
             ["BIOS Vendor","BIOS Version","Release Date","UEFI Status","Secure Boot"],
             None,                       "Read Only"),
            ("cpu",         "◉", "Processor (CPU)",
             ["Processor Name","Architecture","Physical Cores",
              "Logical Threads","Base Clock","CPU ID"],
             self._sp_spoof_cpu,         "Spoof CPU Name"),
            ("gpu",         "◎", "Graphics (GPU)",
             ["GPU Name","Manufacturer","Driver Version","Dedicated VRAM","DirectX"],
             self._sp_spoof_gpu,         "Spoof GPU Name"),
            ("memory",      "▤", "Memory (RAM)",
             ["Total RAM","Used Memory","Available Memory","RAM Type","Speed"],
             None,                       "Read Only"),
            ("storage",     "◫", "Storage Drives",
             ["Drive","Volume Label","Capacity","Free Space","File System",
              "Drive Type","Volume Serial"],
             self._sp_spoof_storage,     "Spoof Volume Serials"),
            ("network",     "◌", "Network Adapters",
             ["Name","Status","MAC","IPv4","Speed","Type"],
             self._sp_spoof_mac,         "Spoof MAC"),
            ("usb",         "⊕", "USB Devices",
             ["Name","Manufacturer","Type","Status","USB Version"],
             None,                       "Read Only"),
            ("monitors",    "▣", "Monitors / Displays",
             ["Name","Resolution","Refresh Rate","Connection","Primary"],
             None,                       "Read Only"),
        ]

        for key, icon, title, fields, spoof_fn, spoof_label in _SP_DEFS:
            self._sp_make_hw_card(inner, key, icon, title, fields,
                                  spoof_fn, spoof_label)

        # ── GPU custom input row (injected into GPU card body) ────────────────
        self._gpu_custom_var = tk.StringVar(value="")
        gpu_body = self._sp_hw_sections["gpu"]["body"]
        gpu_input_row = tk.Frame(gpu_body, bg=SURFACE2)
        gpu_input_row.pack(fill="x", padx=14, pady=(0, 10))

        tk.Label(gpu_input_row, text="GPU:",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE2).pack(side="left")

        preset_names = list(dv._GPU_PRESETS.keys())
        self._gpu_preset_var = tk.StringVar(value="— Random —")
        preset_box = ttk.Combobox(
            gpu_input_row, textvariable=self._gpu_preset_var,
            values=["— Random —"] + preset_names,
            state="readonly", width=26, font=F_MONO,
        )
        preset_box.pack(side="left", padx=(6, 8), ipady=3)

        tk.Label(gpu_input_row, text="or custom:",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE2).pack(side="left")

        _entry(gpu_input_row, self._gpu_custom_var, width=24).pack(
            side="left", padx=(6, 8), ipady=3)

        def _on_preset_change(_e=None):
            # Clear custom field when a preset is chosen
            sel = self._gpu_preset_var.get()
            if sel and sel != "— Random —":
                self._gpu_custom_var.set("")

        preset_box.bind("<<ComboboxSelected>>", _on_preset_change)

        if self._pm.has_permission(Permission.SPOOFER_ROTATE_GUID):
            _btn(gpu_input_row, "Apply", self._sp_spoof_gpu,
                 color=ACCENT, small=True).pack(side="left")

        # Backup label
        self._backup_lbl_var = tk.StringVar(value="No backups yet.")
        backup_row = tk.Frame(inner, bg=BG)
        backup_row.pack(fill="x", padx=P, pady=(10, P))
        tk.Label(backup_row, text="◈  Backup: ",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=BG).pack(side="left")
        tk.Label(backup_row, textvariable=self._backup_lbl_var,
                 font=F_MONO, fg=TEXT_MUTED, bg=BG).pack(side="left")

        tk.Frame(inner, bg=BG, height=20).pack()

        # ── right: log panel ──────────────────────────────────────────────
        log_hdr = tk.Frame(right_outer, bg=BG)
        log_hdr.pack(fill="x", padx=PAD, pady=(PAD+4, 6))
        tk.Label(log_hdr, text="Activity Log",
                 font=tkfont.Font(family="Segoe UI Semibold", size=10,
                                  weight="bold"),
                 fg=TEXT, bg=BG).pack(side="left")
        _btn(log_hdr, "✕ Clear", self._clear_log,
             color=SURFACE2, fg=TEXT_MUTED, small=True).pack(side="right")

        # Kick off background device scan
        self.after(300, self._sp_refresh_all_hw)

    # ── spoofer helpers ────────────────────────────────────────────────────────

    def _sp_update_mode_btns(self) -> None:
        """Highlight the active mode button."""
        mode = self._spoof_mode.get()
        colors = {"temp": WARNING, "perm": DANGER}
        for mv, frame in self._mode_btn_refs.items():
            c = colors.get(mv, ACCENT)
            if mv == mode:
                frame.configure(highlightbackground=c, bg=SURFACE3)
                for w in frame.winfo_children():
                    w.configure(bg=SURFACE3)
            else:
                frame.configure(highlightbackground=BORDER, bg=SURFACE)
                for w in frame.winfo_children():
                    w.configure(bg=SURFACE)

    def _sp_make_hw_card(self, parent: tk.Frame,
                          key: str, icon: str, title: str,
                          fields: list[str],
                          spoof_fn, spoof_label: str) -> None:
        """Build one hardware info + spoof card for the Spoofer tab."""
        COLOURS = {
            "motherboard": "#06b6d4", "bios": "#8b5cf6",
            "cpu":    ACCENT_HOV,     "gpu":  "#a78bfa",
            "memory": "#10b981",      "storage": "#f59e0b",
            "network": "#3b82f6",     "usb": "#ec4899",
            "monitors": "#14b8a6",
        }
        stripe_col = COLOURS.get(key, ACCENT_HOV)

        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="x", padx=PAD, pady=(10, 0))

        card = tk.Frame(wrapper, bg=SURFACE2,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")

        # ── header ────────────────────────────────────────────────────────
        hdr = tk.Frame(card, bg=SURFACE, cursor="hand2")
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=stripe_col, width=4).pack(side="left", fill="y")

        icon_lbl = tk.Label(hdr, text=icon,
                            font=tkfont.Font(family="Segoe UI Emoji", size=13),
                            fg=stripe_col, bg=SURFACE,
                            padx=12, pady=10, cursor="hand2")
        icon_lbl.pack(side="left")

        title_lbl = tk.Label(hdr, text=title,
                             font=tkfont.Font(family="Segoe UI Semibold",
                                              size=9, weight="bold"),
                             fg=TEXT, bg=SURFACE, cursor="hand2")
        title_lbl.pack(side="left")

        # Status badge
        tag_var = tk.StringVar(value="  scanning…  ")
        tag_lbl = tk.Label(hdr, textvariable=tag_var,
                           font=tkfont.Font(family="Segoe UI", size=7,
                                            weight="bold"),
                           fg=TEXT_MUTED, bg=SURFACE2, padx=6, pady=2)
        tag_lbl.pack(side="left", padx=(8, 0))

        # Right: spoof button + collapse
        right_hdr = tk.Frame(hdr, bg=SURFACE)
        right_hdr.pack(side="right", padx=8)

        if spoof_fn is not None and self._pm.has_permission(
                Permission.SPOOFER_ROTATE_GUID):
            sp_btn = tk.Button(
                right_hdr, text=f"⟳  {spoof_label}",
                command=lambda fn=spoof_fn: fn(),
                bg=ACCENT_DIM, fg=ACCENT_LIT,
                activebackground=ACCENT, activeforeground=WHITE,
                relief="flat", bd=0, padx=10, pady=5,
                font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                cursor="hand2",
                highlightthickness=1, highlightbackground=ACCENT_DIM,
            )
            sp_btn.pack(side="left", padx=(0, 6))
            sp_btn.bind("<Enter>", lambda _e, b=sp_btn: b.configure(
                bg=ACCENT, highlightbackground=ACCENT_LIT))
            sp_btn.bind("<Leave>", lambda _e, b=sp_btn: b.configure(
                bg=ACCENT_DIM, highlightbackground=ACCENT_DIM))
        elif spoof_fn is None:
            tk.Label(right_hdr, text="read only",
                     font=tkfont.Font(family="Segoe UI", size=7),
                     fg=TEXT_MUTED2, bg=SURFACE, padx=8).pack(side="left")
        else:
            _locked_btn(right_hdr, spoof_label,
                        Permission.SPOOFER_ROTATE_GUID,
                        self._pm, self, small=True).pack(side="left",
                                                         padx=(0, 6))

        toggle_var = tk.StringVar(value="▼")
        toggle_lbl = tk.Label(right_hdr, textvariable=toggle_var,
                              font=tkfont.Font(family="Segoe UI", size=9),
                              fg=TEXT_MUTED, bg=SURFACE,
                              padx=8, pady=8, cursor="hand2")
        toggle_lbl.pack(side="left")

        tk.Frame(card, bg=BORDER, height=1).pack(fill="x")

        # ── body ─────────────────────────────────────────────────────────
        body = tk.Frame(card, bg=SURFACE2)
        body.pack(fill="x")

        shimmer = tk.Frame(body, bg=SURFACE2)
        shimmer.pack(fill="x", padx=14, pady=8)
        self._dev_shimmer_anim(shimmer, f"sp_{key}")

        data_vars = {f: tk.StringVar(value="—") for f in fields}
        grid_frame = tk.Frame(body, bg=SURFACE2)

        self._sp_hw_sections[key] = {
            "collapsed": False,
            "body":      body,
            "shimmer":   shimmer,
            "grid":      grid_frame,
            "toggle_var": toggle_var,
            "tag_var":   tag_var,
            "tag_lbl":   tag_lbl,
            "data_vars": data_vars,
            "fields":    fields,
            "copy_lbl":  tag_lbl,   # reuse tag_lbl for compat
            "stripe_col": stripe_col,
            "loaded":    False,
        }
        # Register under combined key for shimmer anim
        self._dev_sections[f"sp_{key}"] = self._sp_hw_sections[key]

        def _toggle(_, k=key):
            sec = self._sp_hw_sections[k]
            sec["collapsed"] = not sec["collapsed"]
            if sec["collapsed"]:
                sec["body"].pack_forget()
                sec["toggle_var"].set("▶")
            else:
                sec["body"].pack(fill="x")
                sec["toggle_var"].set("▼")

        for w in (hdr, icon_lbl, title_lbl, toggle_lbl, right_hdr):
            w.bind("<Button-1>", _toggle)

        def _ch(_e, c=card, sc=stripe_col):
            c.configure(highlightbackground=sc)
        def _cl(_e, c=card):
            c.configure(highlightbackground=BORDER)
        for w in (hdr, icon_lbl, title_lbl):
            w.bind("<Enter>", _ch)
            w.bind("<Leave>", _cl)

    def _sp_populate(self, key: str, data) -> None:
        """Populate a spoofer hardware card with fetched data."""
        sec = self._sp_hw_sections.get(key)
        if not sec:
            return
        try:
            sec["shimmer"].pack_forget()
        except Exception:
            pass
        grid = sec["grid"]
        for w in grid.winfo_children():
            w.destroy()

        rows = data if isinstance(data, list) else [data]
        fields = sec["fields"]

        for ridx, row_dict in enumerate(rows):
            if ridx > 0:
                tk.Frame(grid, bg=BORDER, height=1).pack(
                    fill="x", padx=14, pady=(4, 0))
                lbl = (row_dict.get("Drive") or row_dict.get("Name") or
                       row_dict.get("GPU Name") or f"#{ridx+1}")
                tk.Label(grid, text=f"  — {lbl} —",
                         font=tkfont.Font(family="Segoe UI", size=7,
                                          weight="bold"),
                         fg=TEXT_MUTED, bg=SURFACE2).pack(
                    anchor="w", padx=14, pady=(4, 0))

            g = tk.Frame(grid, bg=SURFACE2)
            g.pack(fill="x", padx=14, pady=(6, 8))
            g.columnconfigure(0, weight=1, uniform="sk")
            g.columnconfigure(1, weight=2, uniform="sv")
            g.columnconfigure(2, weight=1, uniform="sk")
            g.columnconfigure(3, weight=2, uniform="sv")

            for fi, field in enumerate(fields):
                value = str(row_dict.get(field, "N/A") or "N/A")
                col_pair = fi % 2
                row_i    = fi // 2
                tk.Label(g, text=field,
                         font=tkfont.Font(family="Segoe UI", size=8),
                         fg=TEXT_MUTED, bg=SURFACE2,
                         anchor="w").grid(row=row_i, column=col_pair*2,
                                          sticky="w", padx=(0,4), pady=2)
                val_fg = self._dev_value_color(field, value)
                tk.Label(g, text=value,
                         font=tkfont.Font(family="Cascadia Code", size=8),
                         fg=val_fg, bg=SURFACE2,
                         anchor="w", wraplength=200).grid(
                    row=row_i, column=col_pair*2+1,
                    sticky="w", padx=(0,16), pady=2)
                sv = sec["data_vars"].get(field)
                if sv:
                    sv.set(value)

        grid.pack(fill="x")
        sec["loaded"] = True
        count = len(rows)
        sec["tag_var"].set(f"  {count} item{'s' if count>1 else ''}  ")
        sec["tag_lbl"].configure(fg=SUCCESS, bg=SURFACE2)
        # Also update dev_sections shimmer tracker
        sp_key = f"sp_{key}"
        if sp_key in self._dev_sections:
            self._dev_sections[sp_key]["loaded"] = True

    def _sp_refresh_all_hw(self) -> None:
        """Background-scan all hardware and populate spoofer cards."""
        COLLECTORS = {
            "motherboard": dv.get_motherboard,
            "bios":        dv.get_bios,
            "cpu":         dv.get_cpu,
            "gpu":         dv.get_gpu,
            "memory":      dv.get_memory,
            "storage":     dv.get_storage,
            "network":     dv.get_network,
            "usb":         dv.get_usb,
            "monitors":    dv.get_monitors,
        }
        def _worker():
            for key, fn in COLLECTORS.items():
                try:
                    data = fn()
                except Exception as exc:
                    data = {f: f"Error: {exc}"
                            for f in self._sp_hw_sections.get(
                                key, {}).get("fields", [])}
                self.after(0, lambda k=key, d=data: self._sp_populate(k, d))
            # Also refresh adapters for MAC spoofing
            try:
                adapters = cu.list_network_adapter_subkeys()
                self._adapter_map = {d: p for p, d in adapters}
            except Exception:
                pass
            self._upd_backup()
        threading.Thread(target=_worker, daemon=True).start()

    # ── spoof action methods ───────────────────────────────────────────────────

    def _sp_run(self, fn, *args) -> None:
        """Permission-guarded async runner for spoof actions."""
        try:
            self._pm.require_permission(Permission.SPOOFER_ROTATE_GUID)
        except (PermissionDeniedError, LicenseExpiredError,
                LicenseRevokedError) as exc:
            _log(f"[permission] {exc}", "error"); return

        mode = self._spoof_mode.get()
        mode_label = "TEMPORARY" if mode == "temp" else "PERMANENT"
        _log(f"[spoof] {fn.__name__} — mode={mode_label}", "section")

        def _t():
            try:
                ok, msg = fn(mode, *args)
                for line in msg.splitlines():
                    _log(f"  {line}", "ok" if ok else "error")
                if ok:
                    self._upd_backup()
                    self.after(0, self._sp_refresh_all_hw)
            except Exception as exc:
                _log(f"  Error: {exc}", "error")
        threading.Thread(target=_t, daemon=True).start()

    def _sp_spoof_uuid(self)        -> None: self._sp_run(dv.spoof_guid)
    def _sp_spoof_cpu(self)         -> None: self._sp_run(dv.spoof_cpu_id)
    def _sp_spoof_gpu(self)         -> None:
        custom  = getattr(self, "_gpu_custom_var", None)
        preset  = getattr(self, "_gpu_preset_var", None)
        name    = (custom.get().strip() if custom else "") or None
        if not name and preset:
            sel = preset.get()
            if sel and sel != "— Random —":
                name = sel
        self._sp_run(dv.spoof_gpu_name, name)
    def _sp_spoof_storage(self)     -> None:
        """Spoof volume serial on every mounted drive."""
        mode = self._spoof_mode.get()
        try:
            self._pm.require_permission(Permission.SPOOFER_ROTATE_GUID)
        except Exception as exc:
            _log(f"[permission] {exc}", "error"); return
        def _t():
            import string as _s
            drives = [f"{c}:" for c in _s.ascii_uppercase
                      if (Path(f"{c}:\\")).exists()]
            _log(f"[spoof] spoof_volume_serial — mode={'TEMPORARY' if mode=='temp' else 'PERMANENT'}", "section")
            for drive in drives:
                try:
                    ok, msg = dv.spoof_volume_serial(drive, mode)
                    for line in msg.splitlines():
                        _log(f"  {line}", "ok" if ok else "error")
                except Exception as exc:
                    _log(f"  [{drive}] ERROR: {exc}", "error")
            self.after(0, self._sp_refresh_all_hw)
        threading.Thread(target=_t, daemon=True).start()
    def _sp_spoof_mac(self)         -> None: self._sp_run(dv.spoof_mac)

    def _spoof_everything(self) -> None:
        try:
            self._pm.require_permission(Permission.SPOOFER_ROTATE_GUID)
        except (PermissionDeniedError, LicenseExpiredError,
                LicenseRevokedError) as exc:
            _log(f"[permission] {exc}", "error"); return

        mode = self._spoof_mode.get()
        mode_label = "TEMPORARY" if mode == "temp" else "PERMANENT"

        if not messagebox.askyesno(
            "Spoof Everything",
            f"Mode: {mode_label}\n\n"
            "This will spoof ALL identifiers:\n"
            "  • Machine GUID · Computer Name · Product/Installation ID\n"
            "  • Hardware Profile GUID · System UUID\n"
            "  • BIOS version/serial · Baseboard · Chassis serial\n"
            "  • CPU ID · GPU identifier\n"
            "  • Disk serials · Volume serials · Partition IDs\n"
            "  • MAC addresses (all adapters) · Adapter GUIDs\n"
            "  • Monitor EDID · USB serials\n"
            "  • Telemetry IDs · Device Instance IDs · Registry IDs\n\n"
            "Also clears: Temp files · Event logs · Prefetch\n"
            "  DNS cache · Network cache · App cache · Recent files\n\n"
            + ("Changes are restorable via 'Restore Temps'." if mode == "temp"
               else "⚠  Changes are PERMANENT and survive reboots.\n"
                    "A full .reg backup is created before every write."),
            icon="warning"
        ):
            return

        def _progress(msg: str, tag: str = "ok") -> None:
            _log(f"  {msg}", tag)

        def _t():
            try:
                cu.require_admin()
                _log(f"=== SPOOF EVERYTHING ({mode_label}) ===", "section")
                if mode == "temp":
                    cu.full_spoof_temporary(log=_progress)
                else:
                    cu.full_spoof_permanent(log=_progress)
                self._upd_backup()
                self.after(0, self._sp_refresh_all_hw)
                _log("=== Done — reboot recommended ===", "ok")
            except Exception as exc:
                _log(f"  ERROR: {exc}", "error")
        threading.Thread(target=_t, daemon=True).start()

    def _restore_all_temp(self) -> None:
        try:
            self._pm.require_permission(Permission.SPOOFER_ROTATE_GUID)
        except (PermissionDeniedError, LicenseExpiredError,
                LicenseRevokedError) as exc:
            _log(f"[permission] {exc}", "error"); return

        def _progress(msg: str, tag: str = "ok") -> None:
            _log(f"  {msg}", tag)

        def _t():
            _log("[restore] Restoring temp spoof originals…", "section")
            try:
                cu.require_admin()
                cu.restore_temp_spoof(log=_progress)
            except Exception as exc:
                _log(f"  restore error: {exc}", "error")
            # Also restore via dv helpers for devices tab cards
            try:
                ok, msg = dv.restore_all_temp()
                for line in msg.splitlines():
                    _log(f"  {line}", "ok" if ok else "warn")
            except Exception:
                pass
            try:
                ok2, msg2 = dv.restore_mac()
                for line in msg2.splitlines():
                    _log(f"  {line}", "ok")
            except Exception:
                pass
            self.after(0, self._sp_refresh_all_hw)
        threading.Thread(target=_t, daemon=True).start()

    @staticmethod
    def _sp_section(parent: tk.Frame, title: str, subtitle: str) -> None:
        tk.Label(parent, text=title,
                 font=tkfont.Font(family="Segoe UI Semibold", size=10,
                                  weight="bold"),
                 bg=SURFACE, fg=TEXT).pack(anchor="w", padx=PAD, pady=(PAD+2, 0))
        tk.Label(parent, text=subtitle,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 bg=SURFACE, fg=TEXT_MUTED).pack(anchor="w", padx=PAD, pady=(2, 6))

    # ─────────────────────────────────────────────────────────────────────
    # TAB — Task Manager
    # ─────────────────────────────────────────────────────────────────────
    def _build_task_manager(self, parent: tk.Frame) -> None:
        """Live process list with CPU/RAM bars, search, and kill button."""
        import psutil as _ps
        PAGE_IDX = 3

        # ── Layout ────────────────────────────────────────────────────────
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        # Header
        hdr = tk.Frame(parent, bg=BG)
        hdr.grid(row=0, column=0, sticky="ew", padx=PAD+12, pady=(PAD+8, 0))
        tk.Label(hdr, text="Task Manager",
                 font=tkfont.Font(family="Segoe UI Semibold", size=22, weight="bold"),
                 fg=TEXT, bg=BG).pack(side="left", anchor="w")
        tk.Label(hdr, text="  Live process monitor",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=BG).pack(side="left", anchor="sw", pady=(6, 0))

        tk.Frame(parent, bg=BORDER, height=1).grid(
            row=0, column=0, sticky="ew", padx=PAD+12, pady=(46, 0))

        # Toolbar
        toolbar = tk.Frame(parent, bg=BG)
        toolbar.grid(row=0, column=0, sticky="ew", padx=PAD+12, pady=(60, 0))

        self._tm_search_var = tk.StringVar()
        search_frame = tk.Frame(toolbar, bg=SURFACE2,
                                highlightthickness=1, highlightbackground=BORDER)
        search_frame.pack(side="left")
        tk.Label(search_frame, text="⌕",
                 font=tkfont.Font(family="Segoe UI Emoji", size=10),
                 fg=TEXT_MUTED, bg=SURFACE2, padx=8).pack(side="left")
        tk.Entry(search_frame, textvariable=self._tm_search_var,
                 bg=SURFACE2, fg=TEXT, insertbackground=ACCENT_LIT,
                 relief="flat", bd=0, font=F_MONO, width=22,
                 highlightthickness=0).pack(side="left", ipady=6, padx=(0, 8))

        _btn(toolbar, "⟳  Refresh", lambda: self._tm_refresh(_ps),
             color=ACCENT, small=True).pack(side="left", padx=(10, 0))
        _btn(toolbar, "✕  Kill Process", lambda: self._tm_kill(_ps),
             color=DANGER, small=True).pack(side="left", padx=(8, 0))

        self._tm_status_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self._tm_status_var,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=BG).pack(side="left", padx=14)

        # Summary chips
        self._tm_cpu_var = tk.StringVar(value="CPU  —")
        self._tm_ram_var = tk.StringVar(value="RAM  —")
        for v in (self._tm_cpu_var, self._tm_ram_var):
            chip = tk.Frame(toolbar, bg=SURFACE2,
                            highlightthickness=1, highlightbackground=BORDER)
            chip.pack(side="right", padx=(0, 8))
            tk.Label(chip, textvariable=v,
                     font=tkfont.Font(family="Cascadia Code", size=8),
                     fg=ACCENT, bg=SURFACE2, padx=10, pady=4).pack()

        # Process table
        table_outer = tk.Frame(parent, bg=BG)
        table_outer.grid(row=1, column=0, sticky="nsew", padx=PAD+12, pady=(10, PAD))
        table_outer.rowconfigure(0, weight=1)
        table_outer.columnconfigure(0, weight=1)

        cols = ("pid", "name", "cpu", "ram", "status", "user")
        self._tm_tree = ttk.Treeview(
            table_outer, columns=cols, show="headings",
            selectmode="browse")
        col_defs = [
            ("pid",    "PID",     55),
            ("name",   "Process", 220),
            ("cpu",    "CPU %",   80),
            ("ram",    "RAM MB",  80),
            ("status", "Status",  90),
            ("user",   "User",    130),
        ]
        for cid, hdr_txt, w in col_defs:
            self._tm_tree.heading(cid, text=hdr_txt,
                command=lambda c=cid: self._tm_sort(c))
            self._tm_tree.column(cid, width=w, minwidth=40, anchor="w")

        vsb_tm = ttk.Scrollbar(table_outer, orient="vertical",
                                command=self._tm_tree.yview)
        self._tm_tree.configure(yscrollcommand=vsb_tm.set)
        vsb_tm.grid(row=0, column=1, sticky="ns")
        self._tm_tree.grid(row=0, column=0, sticky="nsew")

        # Colour tags
        self._tm_tree.tag_configure("high_cpu", foreground=DANGER)
        self._tm_tree.tag_configure("med_cpu",  foreground=WARNING)
        self._tm_tree.tag_configure("normal",   foreground=TEXT)

        # Search filter
        self._tm_search_var.trace_add("write",
            lambda *_: self._tm_filter())

        self._tm_sort_col     = "cpu"
        self._tm_sort_rev     = True
        self._tm_all_rows: list = []

        # Initial load
        self.after(300, lambda: self._tm_refresh(_ps))
        # Auto-refresh every 4 s
        self._tm_auto_refresh(_ps)

    def _tm_refresh(self, _ps) -> None:
        self._tm_status_var.set("Refreshing…")
        def _worker():
            rows = []
            try:
                cpu_total = _ps.cpu_percent(interval=None)
                ram       = _ps.virtual_memory()
                self.after(0, lambda: self._tm_cpu_var.set(
                    f"CPU  {cpu_total:.1f}%"))
                self.after(0, lambda: self._tm_ram_var.set(
                    f"RAM  {ram.percent:.1f}%"))
                for proc in _ps.process_iter(
                        ["pid","name","cpu_percent","memory_info",
                         "status","username"]):
                    try:
                        info = proc.info
                        ram_mb = round(info["memory_info"].rss / 1048576, 1) \
                            if info.get("memory_info") else 0.0
                        rows.append((
                            info["pid"],
                            info["name"] or "—",
                            info["cpu_percent"] or 0.0,
                            ram_mb,
                            info["status"] or "—",
                            (info["username"] or "—").split("\\")[-1],
                        ))
                    except Exception:
                        pass
            except Exception as exc:
                self.after(0, lambda: self._tm_status_var.set(f"Error: {exc}"))
                return
            self.after(0, lambda r=rows: self._tm_populate(r))
        threading.Thread(target=_worker, daemon=True).start()

    def _tm_populate(self, rows: list) -> None:
        self._tm_all_rows = rows
        self._tm_filter()
        n = len(rows)
        self._tm_status_var.set(f"{n} processes")

    def _tm_filter(self) -> None:
        term = self._tm_search_var.get().lower()
        filtered = [r for r in self._tm_all_rows
                    if not term or term in str(r[1]).lower()
                    or term in str(r[0])]
        # Sort
        col_idx = {"pid":0,"name":1,"cpu":2,"ram":3,"status":4,"user":5}.get(
            self._tm_sort_col, 2)
        filtered.sort(key=lambda r: r[col_idx],
                      reverse=self._tm_sort_rev)
        self._tm_tree.delete(*self._tm_tree.get_children())
        for pid, name, cpu, ram, status, user in filtered:
            tag = ("high_cpu" if cpu > 30 else
                   "med_cpu"  if cpu > 10 else "normal")
            self._tm_tree.insert("", "end",
                values=(pid, name, f"{cpu:.1f}", f"{ram:.1f}", status, user),
                tags=(tag,))

    def _tm_sort(self, col: str) -> None:
        if self._tm_sort_col == col:
            self._tm_sort_rev = not self._tm_sort_rev
        else:
            self._tm_sort_col = col
            self._tm_sort_rev = True
        self._tm_filter()

    def _tm_kill(self, _ps) -> None:
        sel = self._tm_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection",
                "Select a process to kill.", parent=self)
            return
        vals = self._tm_tree.item(sel[0], "values")
        pid  = int(vals[0])
        name = vals[1]
        if not messagebox.askyesno("Kill Process",
                f"Terminate '{name}' (PID {pid})?", parent=self):
            return
        try:
            _ps.Process(pid).kill()
            _log(f"[task-mgr] Killed {name} (PID {pid})", "warn")
            self.after(600, lambda: self._tm_refresh(_ps))
        except Exception as exc:
            messagebox.showerror("Kill Failed", str(exc), parent=self)

    def _tm_auto_refresh(self, _ps) -> None:
        try:
            import psutil as _p2
            self._tm_refresh(_p2)
        except Exception:
            pass
        self.after(4000, lambda: self._tm_auto_refresh(_ps))

    # ─────────────────────────────────────────────────────────────────────
    # TAB 5 — Settings  (redesigned with toggles, sliders & icon rows)
    # ─────────────────────────────────────────────────────────────────────
    def _build_settings(self, parent: tk.Frame) -> None:
        PAGE_IDX = 4   # Dashboard=0,Spoofer=1,Devices=2,TaskMgr=3,Settings=4
        canvas = tk.Canvas(parent, bg=BG, bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = _frame(canvas, bg=BG)
        wid   = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._page_canvases[PAGE_IDX] = canvas

        p = inner
        meta = self._meta

        # ── Page header ────────────────────────────────────────────────────
        hdr = tk.Frame(p, bg=BG)
        hdr.pack(fill="x", padx=PAD+12, pady=(PAD+8, 0))
        tk.Label(hdr, text="Settings",
                 font=tkfont.Font(family="Segoe UI Semibold", size=22, weight="bold"),
                 fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(hdr, text="Application preferences and account information.",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=BG).pack(anchor="w", pady=(2, 0))
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", padx=PAD+12, pady=(14, 0))

        # ── Helpers ────────────────────────────────────────────────────────
        def _sh(title: str, icon: str = ""):
            row = tk.Frame(p, bg=BG)
            row.pack(fill="x", padx=PAD+12, pady=(18, 6))
            if icon:
                ib = tk.Frame(row, bg=ACCENT_DIM, width=22, height=22)
                ib.pack(side="left", padx=(0, 8))
                ib.pack_propagate(False)
                tk.Label(ib, text=icon,
                         font=tkfont.Font(family="Segoe UI Emoji", size=9),
                         fg=ACCENT_LIT, bg=ACCENT_DIM).place(relx=0.5, rely=0.5, anchor="center")
            tk.Label(row, text=title.upper(),
                     font=tkfont.Font(family="Segoe UI Semibold", size=8, weight="bold"),
                     fg=TEXT_MUTED, bg=BG).pack(side="left")
            tk.Frame(row, bg=BORDER, height=1).pack(
                side="left", fill="x", expand=True, padx=(10, 0))

        def _sc() -> tk.Frame:
            c = tk.Frame(p, bg=SURFACE2,
                         highlightthickness=1, highlightbackground=BORDER)
            c.pack(fill="x", padx=PAD+12, pady=(0, 2))
            return c

        def _sr(card: tk.Frame, label: str, sub: str = "",
                val: str = "", val_color=None, last: bool = False):
            row = tk.Frame(card, bg=SURFACE2)
            row.pack(fill="x", padx=16, pady=11)
            lc = tk.Frame(row, bg=SURFACE2)
            lc.pack(side="left", fill="x", expand=True)
            tk.Label(lc, text=label,
                     font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                     fg=TEXT, bg=SURFACE2, anchor="w").pack(anchor="w")
            if sub:
                tk.Label(lc, text=sub,
                         font=tkfont.Font(family="Segoe UI", size=8),
                         fg=TEXT_MUTED, bg=SURFACE2,
                         anchor="w").pack(anchor="w", pady=(1, 0))
            if val:
                tk.Label(row, text=val,
                         font=F_MONO, fg=val_color or ACCENT,
                         bg=SURFACE2).pack(side="right", padx=(0, 2))
            if not last:
                _sep(card, color=BORDER).pack(fill="x")

        def _toggle_row(card: tk.Frame, label: str, sub: str,
                        var: tk.BooleanVar, on_color: str = ACCENT,
                        last: bool = False):
            """Settings row with an animated on/off toggle pill."""
            row = tk.Frame(card, bg=SURFACE2)
            row.pack(fill="x", padx=16, pady=11)
            lc = tk.Frame(row, bg=SURFACE2)
            lc.pack(side="left", fill="x", expand=True)
            tk.Label(lc, text=label,
                     font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                     fg=TEXT, bg=SURFACE2, anchor="w").pack(anchor="w")
            tk.Label(lc, text=sub,
                     font=tkfont.Font(family="Segoe UI", size=8),
                     fg=TEXT_MUTED, bg=SURFACE2).pack(anchor="w", pady=(1, 0))
            # Toggle pill (canvas-based)
            pill_w, pill_h = 44, 22
            pill = tk.Canvas(row, width=pill_w, height=pill_h,
                             bg=SURFACE2, bd=0, highlightthickness=0,
                             cursor="hand2")
            pill.pack(side="right", padx=(0, 2))

            def _draw():
                pill.delete("all")
                on = var.get()
                bg_c = on_color if on else BORDER2
                r = pill_h // 2
                # Track
                pill.create_oval(0, 0, pill_h, pill_h, fill=bg_c, outline="")
                pill.create_oval(pill_w - pill_h, 0, pill_w, pill_h, fill=bg_c, outline="")
                pill.create_rectangle(r, 0, pill_w - r, pill_h, fill=bg_c, outline="")
                # Knob
                kx = (pill_w - r - 3) if on else (r + 3)
                pill.create_oval(kx - r + 4, 3, kx + r - 4, pill_h - 3,
                                 fill=WHITE, outline="")

            _draw()
            def _toggle(_e=None):
                var.set(not var.get())
                _draw()
            pill.bind("<Button-1>", _toggle)
            if not last:
                _sep(card, color=BORDER).pack(fill="x")

        # ── ACCOUNT ────────────────────────────────────────────────────────
        _sh("Account", "⊙")
        ac = _sc()
        tier_color = {"TRIAL": WARNING, "PRO": ACCENT_HOV, "ADMIN": GOLD}.get(
            meta.get("tier", ""), ACCENT_HOV)
        _sr(ac, "Username",    val=meta.get("username", "—"), val_color=TEXT)
        _sr(ac, "Tier",        val=meta.get("tier", "—"),     val_color=tier_color)
        _sr(ac, "License Key", val=meta.get("raw_key", "—"),  val_color=ACCENT)
        _sr(ac, "Expires",     val=str(meta.get("expiry") or "Never"),
            val_color=TEXT_MUTED, last=True)

        # ── PREFERENCES ────────────────────────────────────────────────────
        _sh("Preferences", "⚙")
        pref = _sc()
        self._pref_autoscroll_var  = tk.BooleanVar(value=True)
        self._pref_startup_refresh = tk.BooleanVar(value=True)
        self._pref_dark_log        = tk.BooleanVar(value=True)
        _toggle_row(pref, "Auto-scroll log",
                    "Keep the activity log pinned to the latest entry",
                    self._pref_autoscroll_var, on_color=ACCENT)
        _toggle_row(pref, "Refresh on startup",
                    "Auto-load GUID, volume serial & adapters when opening",
                    self._pref_startup_refresh, on_color=SUCCESS)
        _toggle_row(pref, "Dark terminal",
                    "Use dark background for the activity log terminal",
                    self._pref_dark_log, on_color=ACCENT_HOV, last=True)

        # ── BACKUP ─────────────────────────────────────────────────────────
        _sh("Backup", "◈")
        bk = _sc()
        self._backup_dir_var = tk.StringVar(value=str(cu.BACKUP_DIR))
        _sr(bk, "Backup Directory",
            sub="Registry snapshots saved before every write",
            val=str(cu.BACKUP_DIR), val_color=TEXT_MUTED)
        _count_var = tk.StringVar(value="—")
        bk2_row = tk.Frame(bk, bg=SURFACE2)
        bk2_row.pack(fill="x", padx=16, pady=11)
        lc2 = tk.Frame(bk2_row, bg=SURFACE2)
        lc2.pack(side="left", fill="x", expand=True)
        tk.Label(lc2, text="Backup Count",
                 font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                 fg=TEXT, bg=SURFACE2, anchor="w").pack(anchor="w")
        tk.Label(lc2, text="Number of .reg snapshot files currently stored",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE2, anchor="w").pack(anchor="w", pady=(1, 0))
        tk.Label(bk2_row, textvariable=_count_var,
                 font=F_MONO, fg=TEXT_MUTED, bg=SURFACE2).pack(side="right", padx=(0, 2))

        open_btn = tk.Label(bk, text="  ⎘  Open Backup Folder  ",
                            font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                            fg=TEXT_MUTED, bg=SURFACE,
                            highlightthickness=1, highlightbackground=BORDER,
                            cursor="hand2", padx=6, pady=6)
        open_btn.pack(anchor="w", padx=16, pady=(4, 12))

        def _open_backup_folder(_e=None):
            import subprocess as _sp
            try:
                _sp.Popen(["explorer", str(cu.BACKUP_DIR)])
            except Exception:
                pass
        open_btn.bind("<Button-1>", _open_backup_folder)
        open_btn.bind("<Enter>", lambda _e: open_btn.configure(fg=WHITE, bg=SURFACE2))
        open_btn.bind("<Leave>", lambda _e: open_btn.configure(fg=TEXT_MUTED, bg=SURFACE))

        def _refresh_count():
            bd = cu.BACKUP_DIR
            _count_var.set(str(len(list(bd.glob("*.reg")))) if bd.exists() else "0")
        self.after(600, _refresh_count)

        # ── SPOOF DEFAULTS ──────────────────────────────────────────────────
        _sh("Spoof Defaults", "⟳")
        sp = _sc()
        _sr(sp, "Default Mode",
            sub="Temporary — changes reset on next reboot",
            val="Temporary", val_color=WARNING)
        _sr(sp, "Concurrent Operations",
            sub="One identifier spoofed at a time (background thread)",
            val="1", val_color=TEXT_MUTED, last=True)

        # ── APP UPDATES ────────────────────────────────────────────────────
        _sh("App Updates", "↑")
        upd = _sc()
        # Current Version row
        _sr(upd, "Current Version",
            sub="The version of Ghost currently installed on this machine",
            val=f"v{CURRENT_VERSION}", val_color=ACCENT)
        _sep(upd, color=BORDER).pack(fill="x")

        # Latest Version row (populated by background check)
        upd_latest_row = tk.Frame(upd, bg=SURFACE2)
        upd_latest_row.pack(fill="x", padx=16, pady=11)
        lc_ul = tk.Frame(upd_latest_row, bg=SURFACE2)
        lc_ul.pack(side="left", fill="x", expand=True)
        tk.Label(lc_ul, text="Latest Version",
                 font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                 fg=TEXT, bg=SURFACE2, anchor="w").pack(anchor="w")
        tk.Label(lc_ul, text="The newest release available from the update server",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE2, anchor="w").pack(anchor="w", pady=(1, 0))
        self._upd_latest_var = tk.StringVar(value="—")
        tk.Label(upd_latest_row, textvariable=self._upd_latest_var,
                 font=F_MONO, fg=INFO, bg=SURFACE2).pack(side="right", padx=(0, 2))

        _sep(upd, color=BORDER).pack(fill="x")

        # Update Channel row with combobox
        ch_row = tk.Frame(upd, bg=SURFACE2)
        ch_row.pack(fill="x", padx=16, pady=11)
        lc_ch = tk.Frame(ch_row, bg=SURFACE2)
        lc_ch.pack(side="left", fill="x", expand=True)
        tk.Label(lc_ch, text="Update Channel",
                 font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                 fg=TEXT, bg=SURFACE2, anchor="w").pack(anchor="w")
        tk.Label(lc_ch, text="Stable: tested releases.   Beta: early access.   Development: internal builds.",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE2, anchor="w").pack(anchor="w", pady=(1, 0))
        _upd_settings = _load_update_settings()
        self._upd_channel_var = tk.StringVar(value=_upd_settings.get("channel", "stable"))
        ch_combo = ttk.Combobox(ch_row, textvariable=self._upd_channel_var,
                                values=["stable", "beta", "development"],
                                state="readonly", width=12, font=F_BODY)
        ch_combo.pack(side="right", padx=(0, 2), ipady=4)

        def _on_channel_change(_e=None):
            ch = self._upd_channel_var.get()
            _s = _load_update_settings()
            _s["channel"] = ch
            _save_update_settings(_s)

        ch_combo.bind("<<ComboboxSelected>>", _on_channel_change)

        # Check for Updates button
        check_btn_row = tk.Frame(upd, bg=SURFACE2)
        check_btn_row.pack(fill="x", padx=16, pady=(4, 12))
        _btn(check_btn_row, "↺  Check for Updates", self._manual_update_check,
             color=SURFACE3, fg=TEXT2, small=True).pack(anchor="w")

        # ── ABOUT ──────────────────────────────────────────────────────────
        _sh("About", "◆")
        ab = _sc()
        _sr(ab, "Application", val="GhostConfig",          val_color=TEXT)
        _sr(ab, "Version",     val=f"v{CURRENT_VERSION}",  val_color=ACCENT)
        _sr(ab, "Platform",    val="Windows 10 / 11",       val_color=TEXT)
        _sr(ab, "Python",      val=sys.version.split()[0],  val_color=TEXT_MUTED,
            last=True)

        tk.Frame(p, bg=BG, height=32).pack()

    # ─────────────────────────────────────────────────────────────────────
    # TAB 5 — Support
    # ─────────────────────────────────────────────────────────────────────
    def _build_support(self, parent: tk.Frame) -> None:
        # Page header
        hdr = tk.Frame(parent, bg=BG)
        hdr.pack(fill="x", padx=PAD+12, pady=(PAD+8, 0))
        tk.Label(hdr, text="Support",
                 font=tkfont.Font(family="Segoe UI Semibold", size=22, weight="bold"),
                 fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(hdr, text="Help, documentation, and frequently asked questions.",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=BG).pack(anchor="w", pady=(3, 0))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=PAD+12, pady=(14, 0))

        faq = [
            ("What does GhostConfig do?",
             "Modifies Windows system identifiers (Machine GUID, MAC address, Volume Serials)\n"
             "for QA, testing, or privacy purposes. Every change is backed up as a .reg file."),
            ("Do I need Administrator rights?",
             "Yes. Registry writes require elevation. Right-click the exe and choose\n"
             "'Run as administrator', or the UAC prompt will appear automatically."),
            ("How do I manage licenses and releases?",
             "Log in to the Ghost web admin panel at your domain/admin\n"
             "to manage keys, publish updates, view orders, and monitor customers."),
            ("Where are my backups stored?",
             f"Default: {cu.BACKUP_DIR}\n"
             "They are created automatically before every registry write."),
            ("How do I restore a backup?",
             "Double-click any .reg file in the backups folder, or run:\n"
             "  regedit /s backups\\MachineGuid_YYYYMMDD_HHMMSS.reg"),
            ("Is a reboot required after MAC change?",
             "The registry is updated immediately, but takes effect only after\n"
             "disabling and re-enabling the adapter, or rebooting the machine."),
            ("How do license keys work?",
             "Keys are HMAC-SHA256 signed, offline-verifiable tokens in the format\n"
             "GHOST-XXXXX-XXXXX-XXXXX-XXXXX. They encode tier (TRIAL/PRO/ADMIN) and expiry."),
        ]

        canvas = tk.Canvas(parent, bg=BG, bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = _frame(canvas, bg=BG)
        wid   = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(wid, width=e.width))
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._page_canvases[5] = canvas   # Support is page 5 (Admin Panel removed)

        _section_label(inner, "Frequently Asked Questions").pack(
            fill="x", padx=PAD+12, pady=(16, 8))

        for i, (q, a) in enumerate(faq):
            card = tk.Frame(inner, bg=SURFACE2,
                            highlightthickness=1, highlightbackground=BORDER)
            card.pack(fill="x", padx=PAD+12, pady=(0, 8))

            # Question row with left accent strip
            q_row = tk.Frame(card, bg=SURFACE2)
            q_row.pack(fill="x")
            tk.Frame(q_row, bg=ACCENT, width=3).pack(side="left", fill="y")
            tk.Label(q_row, text=f"  {q}",
                     font=tkfont.Font(family="Segoe UI Semibold", size=9,
                                      weight="bold"),
                     fg=TEXT, bg=SURFACE2,
                     anchor="w").pack(anchor="w", padx=(10, 12), pady=(11, 6))

            # Answer
            tk.Label(card, text=f"  {a}",
                     font=tkfont.Font(family="Segoe UI", size=8),
                     fg=TEXT_MUTED, bg=SURFACE2,
                     justify="left", wraplength=800,
                     anchor="w").pack(anchor="w", padx=16, pady=(0, 12))

            # Hover — border glows accent
            def _enter(_, c=card):
                c.configure(highlightbackground=ACCENT)
            def _leave(_, c=card):
                c.configure(highlightbackground=BORDER)
            card.bind("<Enter>", _enter)
            card.bind("<Leave>", _leave)

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=PAD+12, pady=14)
        tk.Label(inner, text="  GhostConfig v4.0  •  Windows 10/11  •  Python 3.8+",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=BG).pack(anchor="w", padx=PAD+12, pady=(0, 24))

    # ─────────────────────────────────────────────────────────────────────
    # TAB — Devices
    # ─────────────────────────────────────────────────────────────────────
    def _build_devices(self, parent: tk.Frame) -> None:
        PAGE_IDX = 2
        # ── outer scroll canvas ───────────────────────────────────────────
        outer = tk.Frame(parent, bg=BG)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=BG, bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        scroll_frame = tk.Frame(canvas, bg=BG)
        _swid = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(_swid, width=e.width))
        scroll_frame.bind("<Configure>",
                          lambda _e: canvas.configure(
                              scrollregion=canvas.bbox("all")))
        self._page_canvases[PAGE_IDX] = canvas

        # ── page header ───────────────────────────────────────────────────
        hdr = tk.Frame(scroll_frame, bg=BG)
        hdr.pack(fill="x", padx=PAD+12, pady=(PAD+8, 0))

        left_h = tk.Frame(hdr, bg=BG)
        left_h.pack(side="left")
        tk.Label(left_h, text="Devices",
                 font=tkfont.Font(family="Segoe UI Semibold", size=22, weight="bold"),
                 fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(left_h, text="Detailed hardware component information",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=BG).pack(anchor="w", pady=(3, 0))

        # Live clock label top-right
        self._dev_clock_var = tk.StringVar(value="")
        tk.Label(hdr, textvariable=self._dev_clock_var,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=BG).pack(side="right", anchor="ne")

        tk.Frame(scroll_frame, bg=BORDER, height=1).pack(
            fill="x", padx=PAD+12, pady=(14, 0))

        # ── toolbar ───────────────────────────────────────────────────────
        toolbar = tk.Frame(scroll_frame, bg=BG)
        toolbar.pack(fill="x", padx=PAD+12, pady=(10, 6))

        self._dev_refresh_btn = _btn(
            toolbar, "⟳  Refresh All", self._devices_refresh,
            color=ACCENT)
        self._dev_refresh_btn.pack(side="left")

        self._dev_status_var = tk.StringVar(value="")
        tk.Label(toolbar, textvariable=self._dev_status_var,
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=BG).pack(side="left", padx=14)

        # ── section card registry ─────────────────────────────────────────
        # Each entry: { "collapsed": bool, "body": Frame,
        #               "toggle_lbl": Label, "data_vars": {field: StringVar} }
        self._dev_sections: dict[str, dict] = {}
        self._dev_scroll_frame = scroll_frame
        self._dev_canvas       = canvas

        # Section definitions: (key, icon, title, fields_order)
        _DEFS: list[tuple[str, str, str, list[str]]] = [
            ("motherboard", "⊞", "Motherboard",
             ["Manufacturer","Product Name","Model","Chipset",
              "Serial Number","UUID","Form Factor"]),
            ("bios",        "◈", "BIOS / Firmware",
             ["BIOS Vendor","BIOS Version","Release Date",
              "SMBIOS Version","UEFI Status","Secure Boot"]),
            ("cpu",         "◉", "Processor (CPU)",
             ["Processor Name","Manufacturer","Architecture",
              "Physical Cores","Logical Threads","Base Clock",
              "Max Boost Clock","Virtualization","Instruction Sets","CPU ID"]),
            ("gpu",         "◎", "Graphics (GPU)",
             ["GPU Name","Manufacturer","Driver Version","Dedicated VRAM",
              "Shared Memory","DirectX","Resolution","Refresh Rate"]),
            ("memory",      "▤", "Memory (RAM)",
             ["Total RAM","Used Memory","Available Memory","RAM Type",
              "Speed","Installed Modules","Slots Used / Total","Max Supported"]),
            ("storage",     "◫", "Storage Drives",
             ["Drive","Volume Label","Model","Capacity","Free Space",
              "Used Space","File System","Drive Type","Health","Volume Serial"]),
            ("network",     "◌", "Network Adapters",
             ["Name","Manufacturer","Status","MAC","IPv4","IPv6",
              "Gateway","DNS","Speed","Type"]),
            ("usb",         "⊕", "USB Devices",
             ["Name","Manufacturer","Type","Status","USB Version","Device ID"]),
            ("monitors",    "▣", "Monitors / Displays",
             ["Name","Manufacturer","Resolution","Refresh Rate",
              "Connection","HDR Support","Orientation","Primary"]),
        ]

        for key, icon, title, fields in _DEFS:
            self._dev_make_section(scroll_frame, key, icon, title, fields)

        # Footer spacer
        tk.Frame(scroll_frame, bg=BG, height=36).pack()

        # ── initial load ──────────────────────────────────────────────────
        self.after(200, self._devices_refresh)
        self._dev_tick()

    # ── devices helpers ────────────────────────────────────────────────────────

    def _dev_tick(self) -> None:
        """Update the live clock every second."""
        try:
            self._dev_clock_var.set(
                datetime.datetime.now().strftime("Last updated: %H:%M:%S"))
        except Exception:
            pass
        self.after(1000, self._dev_tick)

    def _dev_make_section(self, parent: tk.Frame, key: str,
                          icon: str, title: str, fields: list[str]) -> None:
        """Build one collapsible hardware card and register it."""
        wrapper = tk.Frame(parent, bg=BG)
        wrapper.pack(fill="x", padx=PAD+12, pady=(10, 0))

        # Card shell
        card = tk.Frame(wrapper, bg=SURFACE2,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")

        # ── header bar ────────────────────────────────────────────────────
        hdr = tk.Frame(card, bg=SURFACE, cursor="hand2")
        hdr.pack(fill="x")

        # Left accent stripe (colour per section)
        colours = {
            "motherboard": "#06b6d4", "bios": "#8b5cf6",
            "cpu":    ACCENT_HOV,     "gpu":  "#a78bfa",
            "memory": "#10b981",      "storage": "#f59e0b",
            "network": "#3b82f6",     "usb": "#ec4899",
            "monitors": "#14b8a6",
        }
        stripe_col = colours.get(key, ACCENT_HOV)
        tk.Frame(hdr, bg=stripe_col, width=4).pack(side="left", fill="y")

        icon_lbl = tk.Label(hdr, text=icon,
                            font=tkfont.Font(family="Segoe UI Emoji", size=14),
                            fg=stripe_col, bg=SURFACE, padx=14, pady=12,
                            cursor="hand2")
        icon_lbl.pack(side="left")

        title_lbl = tk.Label(hdr, text=title,
                             font=tkfont.Font(family="Segoe UI Semibold",
                                              size=10, weight="bold"),
                             fg=TEXT, bg=SURFACE, cursor="hand2")
        title_lbl.pack(side="left")

        # Spinner / status tag
        tag_var = tk.StringVar(value="  loading…  ")
        tag_lbl = tk.Label(hdr, textvariable=tag_var,
                           font=tkfont.Font(family="Segoe UI", size=7,
                                            weight="bold"),
                           fg=TEXT_MUTED, bg=SURFACE2,
                           padx=6, pady=2)
        tag_lbl.pack(side="left", padx=(8, 0))

        # Right side: copy + collapse
        right = tk.Frame(hdr, bg=SURFACE, cursor="hand2")
        right.pack(side="right", padx=8)

        copy_lbl = tk.Label(right, text="⎘  Copy",
                            font=tkfont.Font(family="Segoe UI", size=8),
                            fg=TEXT_MUTED, bg=SURFACE,
                            padx=8, pady=10, cursor="hand2")
        copy_lbl.pack(side="left")

        toggle_var = tk.StringVar(value="▼")
        toggle_lbl = tk.Label(right, textvariable=toggle_var,
                              font=tkfont.Font(family="Segoe UI", size=9),
                              fg=TEXT_MUTED, bg=SURFACE,
                              padx=8, pady=10, cursor="hand2")
        toggle_lbl.pack(side="left")

        # Separator under header
        tk.Frame(card, bg=BORDER, height=1).pack(fill="x")

        # ── body (data grid) ──────────────────────────────────────────────
        body = tk.Frame(card, bg=SURFACE2)
        body.pack(fill="x")

        # Shimmer placeholder shown while loading
        shimmer_frame = tk.Frame(body, bg=SURFACE2)
        shimmer_frame.pack(fill="x", padx=16, pady=10)
        self._dev_shimmer_anim(shimmer_frame, key)

        # Data vars — created now, populated on refresh
        data_vars: dict[str, tk.StringVar] = {
            f: tk.StringVar(value="—") for f in fields}
        grid_frame = tk.Frame(body, bg=SURFACE2)
        # grid_frame is NOT packed yet — shown after data arrives

        # ── register ──────────────────────────────────────────────────────
        self._dev_sections[key] = {
            "collapsed":   False,
            "body":        body,
            "shimmer":     shimmer_frame,
            "grid":        grid_frame,
            "toggle_var":  toggle_var,
            "tag_var":     tag_var,
            "tag_lbl":     tag_lbl,
            "data_vars":   data_vars,
            "fields":      fields,
            "copy_lbl":    copy_lbl,
            "stripe_col":  stripe_col,
        }

        # ── wire collapse / copy ──────────────────────────────────────────
        def _toggle(_, k=key):
            sec = self._dev_sections[k]
            sec["collapsed"] = not sec["collapsed"]
            if sec["collapsed"]:
                sec["body"].pack_forget()
                sec["toggle_var"].set("▶")
            else:
                sec["body"].pack(fill="x")
                sec["toggle_var"].set("▼")

        def _copy(_, k=key):
            sec = self._dev_sections[k]
            lines = []
            for f in sec["fields"]:
                v = sec["data_vars"][f].get()
                lines.append(f"{f}: {v}")
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            sec["copy_lbl"].configure(fg=SUCCESS)
            self.after(1400, lambda: sec["copy_lbl"].configure(fg=TEXT_MUTED))

        for w in (hdr, icon_lbl, title_lbl, toggle_lbl, right):
            w.bind("<Button-1>", _toggle)
        copy_lbl.bind("<Button-1>", _copy)
        copy_lbl.bind("<Enter>", lambda _e, l=copy_lbl: l.configure(fg=WHITE))
        copy_lbl.bind("<Leave>", lambda _e, l=copy_lbl: l.configure(fg=TEXT_MUTED))
        toggle_lbl.bind("<Enter>", lambda _e, l=toggle_lbl: l.configure(fg=WHITE))
        toggle_lbl.bind("<Leave>", lambda _e, l=toggle_lbl: l.configure(fg=TEXT_MUTED))

        # hover glow on card
        def _ch(_e, c=card):
            c.configure(highlightbackground=stripe_col)
        def _cl(_e, c=card):
            c.configure(highlightbackground=BORDER)
        for w in (hdr, icon_lbl, title_lbl):
            w.bind("<Enter>", _ch)
            w.bind("<Leave>", _cl)

    def _dev_shimmer_anim(self, parent: tk.Frame, key: str) -> None:
        """Pulse a loading bar inside *parent* to indicate work in progress."""
        bar_bg = tk.Frame(parent, bg=SURFACE, height=6,
                          highlightthickness=1, highlightbackground=BORDER)
        bar_bg.pack(fill="x", pady=4)

        bar = tk.Frame(bar_bg, bg=ACCENT_DIM, height=6)
        bar.place(relx=0, rely=0, relwidth=0.0, relheight=1)

        _state = [0.0, 1]   # [position, direction]

        def _step():
            if not parent.winfo_exists():
                return
            dev_sections = getattr(self, "_dev_sections", {})
            sec = dev_sections.get(key, {})
            if sec.get("loaded"):
                return
            pos  = _state[0]
            _state[0] = max(0.0, min(1.0, pos + 0.04 * _state[1]))
            if _state[0] >= 1.0:
                _state[1] = -1
            elif _state[0] <= 0.0:
                _state[1] =  1
            try:
                bar.place(relx=max(0.0, _state[0] - 0.35),
                          rely=0,
                          relwidth=min(0.35, _state[0], 1.0 - _state[0] + 0.35),
                          relheight=1)
            except Exception:
                return
            self.after(30, _step)

        _step()

    def _dev_populate_section(self, key: str,
                               data: "dict | list[dict]") -> None:
        """Render fetched data into the grid of a section card (main thread)."""
        sec = self._dev_sections.get(key)
        if not sec:
            return

        # Hide shimmer, show grid
        try:
            sec["shimmer"].pack_forget()
        except Exception:
            pass

        grid = sec["grid"]
        # Clear any previous children
        for w in grid.winfo_children():
            w.destroy()

        rows_data: list[dict[str, str]] = (
            data if isinstance(data, list) else [data])

        fields = sec["fields"]
        COLS = 2  # key-value pairs per visual row

        for ridx, row_dict in enumerate(rows_data):
            if ridx > 0:
                # Sub-item divider
                tk.Frame(grid, bg=BORDER, height=1).pack(
                    fill="x", padx=16, pady=(4, 0))
                lbl_txt = (row_dict.get("Drive") or row_dict.get("Name") or
                           row_dict.get("GPU Name") or f"#{ridx+1}")
                tk.Label(grid,
                         text=f"  ◆ {lbl_txt} ◆",
                         font=tkfont.Font(family="Segoe UI", size=7,
                                          weight="bold"),
                         fg=TEXT_MUTED, bg=SURFACE2).pack(
                    anchor="w", padx=16, pady=(4, 0))

            # Grid of fields
            g = tk.Frame(grid, bg=SURFACE2)
            g.pack(fill="x", padx=16, pady=(6, 10))
            g.columnconfigure(0, weight=1, uniform="fk")
            g.columnconfigure(1, weight=2, uniform="fv")
            g.columnconfigure(2, weight=1, uniform="fk")
            g.columnconfigure(3, weight=2, uniform="fv")

            for fi, field in enumerate(fields):
                value = str(row_dict.get(field, "N/A") or "N/A")
                col_pair = fi % COLS
                row_i    = fi // COLS

                # Key label
                tk.Label(g, text=field,
                         font=tkfont.Font(family="Segoe UI", size=8),
                         fg=TEXT_MUTED, bg=SURFACE2,
                         anchor="w").grid(
                    row=row_i, column=col_pair * 2,
                    sticky="w", padx=(0, 6), pady=3)

                # Value — badge colouring for known status values
                val_fg = self._dev_value_color(field, value)
                v_lbl = tk.Label(g, text=value,
                                 font=tkfont.Font(family="Cascadia Code",
                                                  size=8),
                                 fg=val_fg, bg=SURFACE2,
                                 anchor="w", wraplength=280)
                v_lbl.grid(row=row_i, column=col_pair * 2 + 1,
                           sticky="w", padx=(0, 20), pady=3)

            # Update data_vars for copy
            for field in fields:
                sv = sec["data_vars"].get(field)
                if sv:
                    sv.set(str(row_dict.get(field, "N/A") or "N/A"))

        grid.pack(fill="x")
        sec["loaded"] = True

        # Update tag badge
        count = len(rows_data)
        tag   = f"  {count} item{'s' if count > 1 else ''}  "
        sec["tag_var"].set(tag)
        sec["tag_lbl"].configure(fg=SUCCESS, bg=SURFACE2)

    @staticmethod
    def _dev_value_color(field: str, value: str) -> str:
        """Return a colour for a value based on its field name and content."""
        v  = value.upper()
        fl = field.lower()
        if v in ("N/A", "…", "UNKNOWN", ""):
            return TEXT_MUTED
        if fl in ("status", "health"):
            if v in ("CONNECTED", "OK", "GOOD", "ACTIVE", "YES"):
                return SUCCESS
            if v in ("DISCONNECTED", "DISABLED", "EXPIRED", "NO"):
                return DANGER
            return WARNING
        if fl == "primary" and v == "YES":
            return SUCCESS
        if fl in ("uefi status",) and v == "UEFI":
            return SUCCESS
        if fl == "secure boot":
            return SUCCESS if v == "ENABLED" else WARNING
        if fl == "virtualization":
            return SUCCESS if v == "ENABLED" else WARNING
        if fl == "hdr support" and v == "YES":
            return SUCCESS
        if fl == "drive type":
            if "NVME" in v: return ACCENT_LIT
            if "SSD" in v:  return SUCCESS
            return TEXT
        if fl == "type" and v in ("WI-FI", "WIFI"):
            return ACCENT_LIT
        return TEXT

    def _devices_refresh(self) -> None:
        """Kick off background collection for all sections."""
        self._dev_status_var.set("Refreshing hardware info…")
        self._dev_refresh_btn.configure(state="disabled", text="↻  Refreshing…")

        # Reset loaded flags + shimmer for each section
        for key, sec in self._dev_sections.items():
            sec["loaded"] = False
            try:
                sec["grid"].pack_forget()
            except Exception:
                pass
            try:
                sec["shimmer"].pack(fill="x", padx=16, pady=10)
                self._dev_shimmer_anim(sec["shimmer"], key)
            except Exception:
                pass
            sec["tag_var"].set("  loading…  ")
            sec["tag_lbl"].configure(fg=TEXT_MUTED, bg=SURFACE2)

        def _worker():
            collectors = {
                "motherboard": dv.get_motherboard,
                "bios":        dv.get_bios,
                "cpu":         dv.get_cpu,
                "gpu":         dv.get_gpu,
                "memory":      dv.get_memory,
                "storage":     dv.get_storage,
                "network":     dv.get_network,
                "usb":         dv.get_usb,
                "monitors":    dv.get_monitors,
            }
            for key, fn in collectors.items():
                try:
                    data = fn()
                except Exception as exc:
                    data = {f: f"Error: {exc}" for f in
                            self._dev_sections.get(key, {}).get("fields", [])}
                self.after(0, lambda k=key, d=data:
                           self._dev_populate_section(k, d))

            self.after(0, self._devices_refresh_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _devices_refresh_done(self) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._dev_status_var.set(f"Refreshed at {ts}")
        self._dev_refresh_btn.configure(state="normal", text="↻  Refresh All")
        # Schedule auto-refresh in 60 s
        self.after(60_000, self._devices_refresh)

    # ─────────────────────────────────────────────────────────────────────────
    # Log pump
    # ─────────────────────────────────────────────────────────────────────────
    def _start_log_pump(self) -> None:
        self._pump()

    def _pump(self) -> None:
        try:
            while True:
                lvl, msg = _log_queue.get_nowait()
                self._append(msg, lvl)
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _append(self, msg: str, lvl: str = "info") -> None:
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        # Timestamp prefix in muted colour, then the message in its level colour
        self._log_box.insert("end", f"[{ts}] ", "muted")
        self._log_box.insert("end", msg + "\n", lvl)
        if self._log_autoscroll.get():
            self._log_box.see("end")
        self._log_box.configure(state="disabled")
        self._sync_log_gutter()
        self._status_var.set(msg[:120])

    def _clear_log(self) -> None:
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")
        self._sync_log_gutter()

    # ─────────────────────────────────────────────────────────────────────────
    # Async runner
    # ─────────────────────────────────────────────────────────────────────────
    def _async(self, fn: Callable, *args) -> None:
        def _w():
            try:
                fn(*args)
            except PermissionError as exc:
                _log(f"[permission] {exc}", "error")
            except Exception as exc:
                _log(f"[error] {exc}", "error")
        threading.Thread(target=_w, daemon=True).start()

    # ── legacy GUID / MAC helpers kept for dashboard compatibility ────────────

    def _read_guid(self) -> None:
        def _t():
            _log("Reading MachineGuid...", "section")
            g = cu.read_machine_guid()
            self.after(0, lambda: self._dash_guid_var.set(g))
            _log(f"  {g}", "ok")
        self._async(_t)

    def _rotate_guid(self) -> None:
        try:
            self._pm.require_permission(Permission.SPOOFER_ROTATE_GUID)
        except (PermissionDeniedError, LicenseExpiredError, LicenseRevokedError) as exc:
            _log(f"[permission] {exc}", "error"); return
        def _t():
            _log("Rotating MachineGuid...", "section")
            old, new = cu.update_machine_guid()
            self.after(0, lambda: self._dash_guid_var.set(new))
            _log(f"  {old}  ->  {new}", "ok")
            self._upd_backup()
        self._async(_t)

    def _set_guid(self) -> None:
        try:
            self._pm.require_permission(Permission.SPOOFER_CUSTOM_GUID)
        except (PermissionDeniedError, LicenseExpiredError, LicenseRevokedError) as exc:
            _log(f"[permission] {exc}", "error"); return
        def _t():
            _log("Setting custom GUID...", "section")
            old, new = cu.update_machine_guid()
            self.after(0, lambda: self._dash_guid_var.set(new))
            _log(f"  {old}  ->  {new}", "ok")
            self._upd_backup()
        self._async(_t)

    def _refresh_adapters(self) -> None:
        def _t():
            _log("Enumerating adapters...", "section")
            adapters = cu.list_network_adapter_subkeys()
            self._adapter_map = {d: p for p, d in adapters}
            _log(f"  {len(adapters)} adapter(s) found.", "ok")
        self._async(_t)

    def _random_mac(self) -> None:
        mac = "02" + "".join(f"{random.randint(0,255):02X}" for _ in range(5))
        _log(f"  Random LAA: {mac}", "info")

    def _apply_mac(self) -> None:
        try:
            self._pm.require_permission(Permission.SPOOFER_SET_MAC)
        except (PermissionDeniedError, LicenseExpiredError, LicenseRevokedError) as exc:
            _log(f"[permission] {exc}", "error"); return
        def _t():
            _log("Spoofing MAC (all adapters)...", "section")
            ok, msg = dv.spoof_mac(self._spoof_mode.get())
            for line in msg.splitlines():
                _log(f"  {line}", "ok" if ok else "error")
            self._upd_backup()
        threading.Thread(target=_t, daemon=True).start()

    def _query_volumes(self) -> None:
        def _t():
            _log("Querying volumes...", "section")
            vols = cu.query_all_volumes()
            _log(f"  {len(vols)} volume(s).", "ok")
            for v in vols:
                _log(f"    {v.get('drive','')}  {v.get('serial_hex','')}", "info")
        self._async(_t)

    def _randomise_all(self) -> None:
        self._spoof_everything()

    # ─────────────────────────────────────────────────────────────────────────
    # Backup label helper
    # ─────────────────────────────────────────────────────────────────────────
    def _upd_backup(self) -> None:
        bd = cu.BACKUP_DIR
        if bd.exists():
            files = sorted(bd.glob("*.reg"), reverse=True)
            last  = files[0].name if files else "-"
            self.after(0, lambda: self._backup_lbl_var.set(
                f"{len(files)} backup(s)   Latest: {last}"))


# ── Entry point ───────────────────────────────────────────────────────────────
def _crash_log(exc: BaseException) -> None:
    """Write a crash.log next to the exe and show a messagebox."""
    import traceback
    log_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    log_path = log_dir / "crash.log"
    tb = traceback.format_exc()
    try:
        log_path.write_text(
            f"GhostConfig crash — {datetime.datetime.now()}\n\n{tb}\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    try:
        messagebox.showerror(
            "GhostConfig — Crash",
            f"An unexpected error occurred and the application cannot continue.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"A crash log has been saved to:\n{log_path}"
        )
    except Exception:
        pass


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        App().mainloop()
    except Exception as exc:
        _crash_log(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
