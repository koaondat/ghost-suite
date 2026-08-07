"""
gui.py — GhostConfig  (GUI entry point)
========================================
Tabbed main window with 5 tabs:
  Dashboard   — live license key, GUID, volume serial, status cards
  Spoofer     — GUID / MAC / volume controls with live log
  Admin Panel — key-gated admin controls (ADMIN tier only)
  Settings    — backup dir, license display
  Support     — FAQ / help

Auth screen is shown as a Toplevel first; main window appears on
successful login.

Requires: Python 3.8+, Windows, run as Administrator.
"""

from __future__ import annotations

import ctypes
import datetime
import os
import queue
import random
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Callable

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
# Admin Panel  (Toplevel, ADMIN tier only)
# ─────────────────────────────────────────────────────────────────────────────

class AdminPanel(tk.Toplevel):
    """
    Admin Panel — improved layout, inline search/filter, row-count badges,
    sortable headings, keyboard shortcuts, and a cleaner toolbar.
    All backend calls (kg.*) are unchanged.
    """

    _TAB_NAMES = ["Keys", "Banned", "Blacklist", "Whitelist", "Users", "Activity"]
    _TAB_ICONS = ["◆", "⛔", "✕", "✓", "⊙", "◉"]

    def __init__(self, master: tk.Tk):
        super().__init__(master)
        self.title("GhostConfig — Admin Panel")
        self.configure(bg=BG)
        self.geometry("980x660")
        self.minsize(820, 520)
        self.resizable(True, True)
        self._active_tab: int = 0
        self._tab_frames: list[tk.Frame] = []
        self._tab_btns:   list[tk.Label] = []
        self._row_count_vars: list[tk.StringVar] = [tk.StringVar(value="0")
                                                     for _ in self._TAB_NAMES]
        self._activity_filter_var = tk.StringVar(value="All")
        self._activity_search_var = tk.StringVar(value="")
        self._build()
        self.grab_set()
        self.update_idletasks()
        x = master.winfo_x() + (master.winfo_width()  - 980) // 2
        y = master.winfo_y() + (master.winfo_height() - 660) // 2
        self.geometry(f"+{x}+{y}")
        # Keyboard: Escape closes, Ctrl+R refreshes active tab
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Control-r>", lambda _e: self._refresh_active())
        self.bind("<Control-R>", lambda _e: self._refresh_active())

    # ── Shell ─────────────────────────────────────────────────────────────
    def _build(self) -> None:
        # ── Header bar ────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=SURFACE, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Accent logo square
        logo_box = tk.Frame(hdr, bg=ACCENT, width=30, height=30)
        logo_box.place(x=16, rely=0.5, anchor="w")
        logo_box.pack_propagate(False)
        tk.Label(logo_box, text="A",
                 font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                 fg=BG, bg=ACCENT).place(relx=0.5, rely=0.5, anchor="center")

        title_f = tk.Frame(hdr, bg=SURFACE)
        title_f.place(x=56, rely=0.5, anchor="w")
        tk.Label(title_f, text="Admin Panel",
                 font=tkfont.Font(family="Segoe UI", size=11, weight="bold"),
                 fg=TEXT, bg=SURFACE).pack(side="left")
        tk.Label(title_f, text="  ·  key, user & list management",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=SURFACE).pack(side="left")

        # Close button in header
        close_btn = tk.Label(hdr, text="✕",
                             font=tkfont.Font(family="Segoe UI", size=11),
                             fg=TEXT_MUTED, bg=SURFACE,
                             padx=18, cursor="hand2")
        close_btn.pack(side="right", fill="y")
        close_btn.bind("<Button-1>", lambda _e: self.destroy())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=DANGER,   bg=DANGER_BG))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_MUTED, bg=SURFACE))

        _sep(self, color=BORDER).pack(fill="x")

        # ── Custom tab bar ────────────────────────────────────────────────
        tab_bar = tk.Frame(self, bg=BG, highlightthickness=0)
        tab_bar.pack(fill="x")
        _sep(self, color=BORDER).pack(fill="x")

        for i, (icon, name) in enumerate(zip(self._TAB_ICONS, self._TAB_NAMES)):
            tf = tk.Frame(tab_bar, bg=BG, cursor="hand2")
            tf.pack(side="left")

            lbl = tk.Label(tf,
                           text=f"  {icon}  {name}  ",
                           font=tkfont.Font(family="Segoe UI", size=9),
                           fg=TEXT_MUTED, bg=BG,
                           padx=6, pady=10, cursor="hand2")
            lbl.pack(side="left")

            # Badge showing row count
            badge = tk.Label(tf, textvariable=self._row_count_vars[i],
                             font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                             fg=BG, bg=TEXT_MUTED2,
                             padx=5, pady=1)
            badge.pack(side="left", pady=0)
            self._tab_badges: list[tk.Label]  # forward declaration
            if i == 0:
                self._tab_badges = []
            self._tab_badges.append(badge)

            # Bottom-border active indicator (3px, hidden when inactive)
            bar = tk.Frame(tf, bg=BG, height=3)
            bar.pack(fill="x", side="bottom")

            for w in (tf, lbl, badge, bar):
                w.bind("<Button-1>", lambda _e, idx=i: self._switch_tab(idx))
                w.bind("<Enter>",    lambda _e, lb=lbl, bb=bar: (
                    lb.configure(fg=TEXT2, bg=SURFACE2) if lb.cget("fg") != str(ACCENT) else None,
                    bb.master.configure(bg=SURFACE2) if lb.cget("fg") != str(ACCENT) else None))
                w.bind("<Leave>",    lambda _e, lb=lbl, bb=bar: (
                    lb.configure(fg=TEXT_MUTED, bg=BG) if lb.cget("fg") != str(ACCENT) else None,
                    bb.master.configure(bg=BG) if lb.cget("fg") != str(ACCENT) else None))

            self._tab_btns.append(lbl)

        # Refresh shortcut hint (right side of tab bar)
        tk.Label(tab_bar,
                 text="Ctrl+R · refresh    Esc · close",
                 font=tkfont.Font(family="Segoe UI", size=7),
                 fg=TEXT_MUTED2, bg=BG, padx=14).pack(side="right")

        # ── Content area ──────────────────────────────────────────────────
        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True)

        self._tab_keys      = _frame(content, bg=BG)
        self._tab_banned    = _frame(content, bg=BG)
        self._tab_blacklist = _frame(content, bg=BG)
        self._tab_whitelist = _frame(content, bg=BG)
        self._tab_users     = _frame(content, bg=BG)
        self._tab_activity  = _frame(content, bg=BG)

        self._tab_frames = [
            self._tab_keys, self._tab_banned,
            self._tab_blacklist, self._tab_whitelist,
            self._tab_users, self._tab_activity,
        ]
        for f in self._tab_frames:
            f.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_keys_tab()
        self._build_banned_tab()
        self._build_users_tab()
        self._build_activity_tab()
        self._build_list_tab(self._tab_blacklist,
            cols=[("entry","Entry",280),("note","Note",200),("date","Date",90)],
            load_fn=kg.load_blacklist, add_fn=self._bl_add,
            remove_fn=self._bl_remove, attr="_bl_tree")
        self._build_list_tab(self._tab_whitelist,
            cols=[("entry","Entry",280),("note","Note",200),("date","Date",90)],
            load_fn=kg.load_whitelist, add_fn=self._wl_add,
            remove_fn=self._wl_remove, attr="_wl_tree")

        self._switch_tab(0)

    # ── Tab switching ─────────────────────────────────────────────────────
    def _switch_tab(self, index: int) -> None:
        self._active_tab = index
        for i, (lbl, frame) in enumerate(zip(self._tab_btns, self._tab_frames)):
            active = (i == index)
            lbl.configure(
                fg=ACCENT if active else TEXT_MUTED,
                bg=SURFACE if active else BG,
                font=tkfont.Font(family="Segoe UI", size=9,
                                 weight="bold" if active else "normal"),
            )
            # Container frame bg — SURFACE when active for raised look
            lbl.master.configure(bg=SURFACE if active else BG)
            # Active bottom bar = accent stripe; inactive = transparent
            for child in lbl.master.winfo_children():
                if isinstance(child, tk.Frame) and child is not lbl.master:
                    child.configure(bg=ACCENT if active else BG)
            self._tab_badges[i].configure(
                bg=ACCENT if active else TEXT_MUTED2,
                fg=BG,
            )
            if active:
                frame.lift()
            else:
                frame.lower()

    def _refresh_active(self) -> None:
        fns = [self._refresh_keys, self._refresh_banned,
               lambda: self._refresh_list(self._bl_tree, kg.load_blacklist,
                   [("entry","Entry",280),("note","Note",200),("date","Date",90)]),
               lambda: self._refresh_list(self._wl_tree, kg.load_whitelist,
                   [("entry","Entry",280),("note","Note",200),("date","Date",90)]),
               self._refresh_users,
               self._refresh_activity]
        fns[self._active_tab]()

    # ── Toolbar builder (shared between all tabs) ─────────────────────────
    def _make_toolbar(self, parent: tk.Frame, bg: str = BG) -> tk.Frame:
        """Returns a left-aligned toolbar row with a right-side search entry."""
        bar = tk.Frame(parent, bg=SURFACE, highlightthickness=0)
        bar.pack(fill="x", padx=0, pady=0)
        _sep(bar, color=BORDER).pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg=SURFACE)
        inner.pack(fill="x", padx=14, pady=9)
        return inner

    def _make_search(self, parent: tk.Frame, tree: ttk.Treeview,
                     load_fn, cols,
                     bg: str = BG) -> tk.StringVar:
        """Search box that filters *tree* rows in real time."""
        sv = tk.StringVar()

        wrap = tk.Frame(parent, bg=SURFACE3,
                        highlightthickness=1,
                        highlightbackground=BORDER2,
                        highlightcolor=ACCENT)
        wrap.pack(side="right")

        tk.Label(wrap, text="⌕",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=SURFACE3, padx=8).pack(side="left")

        e = tk.Entry(wrap, textvariable=sv, width=22,
                     bg=SURFACE3, fg=TEXT,
                     insertbackground=ACCENT,
                     relief="flat", bd=0, font=F_BODY,
                     highlightthickness=0)
        e.pack(side="left", ipady=5, padx=(0, 8))

        e.bind("<FocusIn>",  lambda _ev: wrap.configure(highlightbackground=ACCENT))
        e.bind("<FocusOut>", lambda _ev: wrap.configure(highlightbackground=BORDER2))

        def _filter(*_):
            q = sv.get().strip().lower()
            self._refresh_list(tree, load_fn, cols)
            if not q:
                return
            keep = []
            for iid in tree.get_children():
                vals = " ".join(str(v) for v in tree.item(iid, "values")).lower()
                if q in vals:
                    keep.append(iid)
                else:
                    tree.delete(iid)
            # re-tag survivors
            for iid in tree.get_children():
                tree.selection_remove(iid)

        sv.trace_add("write", _filter)
        return sv

    # ── Keys tab ──────────────────────────────────────────────────────────
    def _build_keys_tab(self) -> None:
        p, bg = self._tab_keys, BG

        # ── Generate form ─────────────────────────────────────────────────
        gen_card = tk.Frame(p, bg=SURFACE,
                            highlightthickness=1, highlightbackground=BORDER2)
        gen_card.pack(fill="x", padx=16, pady=(16, 8))

        top = tk.Frame(gen_card, bg=SURFACE)
        top.pack(fill="x", padx=14, pady=(12, 6))

        # Section label — accent left border accent
        lbl_bar = tk.Frame(top, bg=ACCENT, width=3)
        lbl_bar.pack(side="left", fill="y", padx=(0, 8))
        tk.Label(top, text="GENERATE NEW KEY",
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=ACCENT, bg=SURFACE).pack(side="left", anchor="w")

        form = tk.Frame(gen_card, bg=SURFACE)
        form.pack(fill="x", padx=14, pady=(0, 14))

        # Tier
        tk.Label(form, text="Tier",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=0, sticky="w", pady=(0,3))
        self._gen_tier = tk.StringVar(value="PRO")
        ttk.Combobox(form, textvariable=self._gen_tier,
                     values=["TRIAL","PRO","ADMIN"],
                     state="readonly", width=9,
                     font=F_BODY).grid(row=1, column=0, sticky="w", padx=(0,12), ipady=4)

        # Expires
        tk.Label(form, text="Expires (days, 0=never)",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=1, sticky="w", pady=(0,3))
        self._gen_days = tk.StringVar(value="365")
        _entry(form, self._gen_days, width=7).grid(row=1, column=1, sticky="w",
                                                   padx=(0,12), ipady=4)

        # Note
        tk.Label(form, text="Note (optional)",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=2, sticky="w", pady=(0,3))
        self._gen_note = tk.StringVar()
        _entry(form, self._gen_note, width=24).grid(row=1, column=2, sticky="w",
                                                    padx=(0,12), ipady=4)

        # Generate button
        tk.Label(form, text="",
                 bg=SURFACE).grid(row=0, column=3, pady=(0,3))
        _btn(form, "+ Generate Key", self._generate_key,
             color=SUCCESS, fg=BG).grid(row=1, column=3, sticky="w", pady=0)

        # Result strip — monospace key display with copy
        res = tk.Frame(gen_card, bg=SURFACE2,
                       highlightthickness=1, highlightbackground=BORDER)
        res.pack(fill="x", padx=14, pady=(0, 14))
        self._gen_result_var = tk.StringVar(value="")
        res_lbl = tk.Label(res, textvariable=self._gen_result_var,
                           font=F_MONO, fg=SUCCESS, bg=SURFACE2, anchor="w",
                           padx=10, pady=6)
        res_lbl.pack(side="left", fill="x", expand=True)
        _btn(res, "⎘ Copy", self._copy_result,
             color=SURFACE3, fg=TEXT_MUTED, small=True).pack(side="right", padx=4, pady=4)

        # ── Search + table ────────────────────────────────────────────────
        toolbar = self._make_toolbar(p)
        _btn(toolbar, "↺ Refresh", self._refresh_keys,
             color=SURFACE3, fg=TEXT2, small=True).pack(side="left")
        _btn(toolbar, "✕ Delete", self._delete_key,
             color=DANGER_BG, fg=DANGER, small=True).pack(side="left", padx=(8,0))
        _btn(toolbar, "⛔ Ban Key", self._ban_selected_key,
             color=WARNING_BG, fg=WARNING, small=True).pack(side="left", padx=(8,0))

        cols = [("key","License Key",230),("tier","Tier",60),
                ("created","Created",90),("expiry","Expiry",90),("note","Note",150)]
        self._keys_tree = self._make_tree(p, cols, tag_idx=0)
        self._keys_search = self._make_search(toolbar, self._keys_tree,
                                              kg.load_all_keys, cols)
        self._refresh_keys()

    def _generate_key(self) -> None:
        try:
            days = int(self._gen_days.get())
        except ValueError:
            messagebox.showerror("Input", "Expires days must be an integer.", parent=self)
            return
        key  = kg.generate_key(expires_days=days, tier=self._gen_tier.get())
        meta = kg.validate_key(key)
        meta["note"] = self._gen_note.get().strip()
        kg.save_key_record(key, meta)
        self._gen_result_var.set(key)
        _log(f"[admin] Generated {meta['tier']} key: {key}", "ok")
        self._refresh_keys()

    def _copy_result(self) -> None:
        val = self._gen_result_var.get()
        if val:
            self.clipboard_clear(); self.clipboard_append(val)
            _log(f"[admin] Copied: {val}", "info")

    def _refresh_keys(self) -> None:
        self._keys_tree.delete(*self._keys_tree.get_children())
        rows = kg.load_all_keys()
        for r in rows:
            key = r.get("key","")
            banned = kg.is_banned(key)
            tag = "banned" if banned else ("admin_key" if r.get("tier","") == "ADMIN" else "")
            display_key = key + "  [BANNED]" if banned else key
            self._keys_tree.insert("", "end", tags=(tag,), values=(
                display_key, r.get("tier",""),
                r.get("created",""), r.get("expiry",""), r.get("note","")))
        self._row_count_vars[0].set(str(len(rows)))

    def _sel_key(self, tree: ttk.Treeview) -> str | None:
        sel = tree.selection()
        if not sel:
            return None
        raw = tree.item(sel[0], "values")[0]
        return raw.replace("  [BANNED]","").replace(" [BANNED]","").strip()

    def _delete_key(self) -> None:
        key = self._sel_key(self._keys_tree)
        if not key:
            messagebox.showwarning("No Selection", "Select a key first.", parent=self); return
        if not messagebox.askyesno("Delete Key",
                f"Permanently delete this key?\n\n{key}", parent=self): return
        kg.delete_key_record(key)
        _log(f"[admin] Deleted key: {key}", "warn")
        self._refresh_keys()

    def _ban_selected_key(self) -> None:
        key = self._sel_key(self._keys_tree)
        if not key:
            messagebox.showwarning("No Selection", "Select a key first.", parent=self); return
        reason = simpledialog.askstring("Ban Key",
            f"Reason for banning this key?\n(leave blank for none)\n\n{key}",
            parent=self) or ""
        kg.ban_key(key, reason)
        _log(f"[admin] Banned: {key}", "error")
        self._refresh_keys(); self._refresh_banned()

    # ── Banned tab ────────────────────────────────────────────────────────
    def _build_banned_tab(self) -> None:
        p, bg = self._tab_banned, BG

        add_card = tk.Frame(p, bg=SURFACE,
                            highlightthickness=1, highlightbackground=BORDER2)
        add_card.pack(fill="x", padx=16, pady=(16, 8))

        top = tk.Frame(add_card, bg=SURFACE)
        top.pack(fill="x", padx=14, pady=(12, 6))
        lbl_bar = tk.Frame(top, bg=DANGER, width=3)
        lbl_bar.pack(side="left", fill="y", padx=(0, 8))
        tk.Label(top, text="BAN A KEY MANUALLY",
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=DANGER, bg=SURFACE).pack(side="left")

        form = tk.Frame(add_card, bg=SURFACE)
        form.pack(fill="x", padx=14, pady=(0, 14))

        tk.Label(form, text="License Key",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=0, sticky="w", pady=(0,3))
        self._ban_key_var = tk.StringVar()
        _entry(form, self._ban_key_var, width=34).grid(row=1, column=0, sticky="w",
                                                       padx=(0,12), ipady=4)

        tk.Label(form, text="Reason (optional)",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=1, sticky="w", pady=(0,3))
        self._ban_reason_var = tk.StringVar()
        _entry(form, self._ban_reason_var, width=22).grid(row=1, column=1, sticky="w",
                                                          padx=(0,12), ipady=4)

        tk.Label(form, text="", bg=SURFACE).grid(row=0, column=2, pady=(0,3))
        _btn(form, "⛔ Ban Key", self._ban_manual,
             color=DANGER, fg=BG).grid(row=1, column=2, sticky="w")

        toolbar = self._make_toolbar(p)
        _btn(toolbar, "↺ Refresh", self._refresh_banned,
             color=SURFACE3, fg=TEXT2, small=True).pack(side="left")
        _btn(toolbar, "✓ Unban", self._unban_selected,
             color=SUCCESS_BG, fg=SUCCESS, small=True).pack(side="left", padx=(8,0))

        self._banned_tree = self._make_tree(
            p, [("key","Banned Key",280),("reason","Reason",200),("date","Banned On",100)])
        self._make_search(toolbar, self._banned_tree, kg.load_banned,
            [("key","Banned Key",280),("reason","Reason",200),("date","Banned On",100)])
        self._refresh_banned()

    def _ban_manual(self) -> None:
        key = self._ban_key_var.get().strip()
        if not key:
            messagebox.showwarning("Empty", "Enter a key to ban.", parent=self); return
        kg.ban_key(key, self._ban_reason_var.get().strip())
        self._ban_key_var.set(""); self._ban_reason_var.set("")
        _log(f"[admin] Banned: {key}", "error")
        self._refresh_banned()

    def _refresh_banned(self) -> None:
        self._banned_tree.delete(*self._banned_tree.get_children())
        rows = kg.load_banned()
        for r in rows:
            self._banned_tree.insert("", "end", tags=("banned",),
                values=(r.get("key",""), r.get("reason",""), r.get("date","")))
        self._row_count_vars[1].set(str(len(rows)))

    def _unban_selected(self) -> None:
        key = self._sel_key(self._banned_tree)
        if not key:
            messagebox.showwarning("No Selection", "Select a key to unban.", parent=self); return
        kg.unban_key(key)
        _log(f"[admin] Unbanned: {key}", "ok")
        self._refresh_banned(); self._refresh_keys()

    # ── Users tab ─────────────────────────────────────────────────────────
    def _build_users_tab(self) -> None:
        p, bg = self._tab_users, BG
        cols = [("username","Username",140),("tier","Tier",65),
                ("key","License Key",260),("created","Created",100)]

        toolbar = self._make_toolbar(p)
        _btn(toolbar, "↺ Refresh", self._refresh_users,
             color=SURFACE3, fg=TEXT2, small=True).pack(side="left")
        _btn(toolbar, "✕ Delete User", self._delete_user,
             color=DANGER_BG, fg=DANGER, small=True).pack(side="left", padx=(8,0))

        self._users_tree = self._make_tree(p, cols)
        self._make_search(toolbar, self._users_tree, kg.load_all_users, cols)
        self._refresh_users()

    def _refresh_users(self) -> None:
        self._users_tree.delete(*self._users_tree.get_children())
        rows = kg.load_all_users()
        for u in rows:
            tier = u.get("tier","")
            tag  = "admin_key" if tier == "ADMIN" else ("pro_key" if tier == "PRO" else "")
            self._users_tree.insert("", "end", tags=(tag,), values=(
                u.get("username",""), tier,
                u.get("key",""), u.get("created","")))
        self._row_count_vars[4].set(str(len(rows)))

    def _delete_user(self) -> None:
        sel = self._users_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a user first.", parent=self); return
        username = self._users_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno("Delete User",
                f"Permanently delete user '{username}'?", parent=self): return
        kg.delete_user(username)
        _log(f"[admin] Deleted user: {username}", "warn")
        self._refresh_users()


    # ── Activity tab ──────────────────────────────────────────────────────
    def _build_activity_tab(self) -> None:
        p = self._tab_activity

        # Guard: require VIEW_LOGIN_ACTIVITY permission
        if not _has_activity_perm(self.master):
            tk.Label(p, text="🔒  Admin permission required to view login activity.",
                     font=tkfont.Font(family="Segoe UI", size=10),
                     fg=TEXT_MUTED, bg=BG).pack(expand=True)
            return

        # ── Info card ─────────────────────────────────────────────────────
        info_card = tk.Frame(p, bg=SURFACE,
                             highlightthickness=1, highlightbackground=BORDER2)
        info_card.pack(fill="x", padx=16, pady=(16, 8))

        top = tk.Frame(info_card, bg=SURFACE)
        top.pack(fill="x", padx=14, pady=(12, 8))

        lbl_bar = tk.Frame(top, bg=INFO, width=3)
        lbl_bar.pack(side="left", fill="y", padx=(0, 8))
        tk.Label(top, text="LOGIN ACTIVITY",
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=INFO, bg=SURFACE).pack(side="left")

        tk.Label(info_card,
                 text="Records every login and registration attempt.  "
                      "No passwords, tokens or secret keys are stored.",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE, anchor="w",
                 padx=14, pady=(0)).pack(fill="x", padx=14, pady=(0, 10))

        # ── Filter bar ────────────────────────────────────────────────────
        filter_frame = tk.Frame(info_card, bg=SURFACE)
        filter_frame.pack(fill="x", padx=14, pady=(0, 14))

        tk.Label(filter_frame, text="Filter:",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).pack(side="left", padx=(0, 6))

        _filter_opts = [
            "All", "Login Success", "Login Fail",
            "Register Success", "Register Fail",
            "Admin Key OK", "Admin Key Fail",
        ]
        _filter_map = {
            "All":              None,
            "Login Success":    "login_success",
            "Login Fail":       "login_fail",
            "Register Success": "register_success",
            "Register Fail":    "register_fail",
            "Admin Key OK":     "admin_key_login_success",
            "Admin Key Fail":   "admin_key_login_fail",
        }

        for opt in _filter_opts:
            is_active = (opt == "All")
            btn = tk.Button(
                filter_frame, text=opt,
                font=tkfont.Font(family="Segoe UI", size=8),
                fg=TEXT_MUTED if not is_active else BG,
                bg=SURFACE3 if not is_active else ACCENT,
                activebackground=ACCENT, activeforeground=BG,
                relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                highlightthickness=1, highlightbackground=BORDER,
            )
            btn.pack(side="left", padx=(0, 4))

            def _make_filter_cmd(o=opt, b=btn, m=_filter_map[opt]):
                def _cmd():
                    self._activity_filter_var.set(o)
                    # Recolour all filter buttons
                    for child in filter_frame.winfo_children():
                        if isinstance(child, tk.Button):
                            active = (child.cget("text") == o)
                            child.configure(
                                bg=ACCENT if active else SURFACE3,
                                fg=BG     if active else TEXT_MUTED,
                            )
                    self._refresh_activity()
                return _cmd
            btn.configure(command=_make_filter_cmd())

        # ── Toolbar ────────────────────────────────────────────────────────
        toolbar = self._make_toolbar(p)
        _btn(toolbar, "↺ Refresh", self._refresh_activity,
             color=SURFACE3, fg=TEXT2, small=True).pack(side="left")
        _btn(toolbar, "👤 User Details", self._open_user_details,
             color=SURFACE3, fg=TEXT2, small=True).pack(side="left", padx=(8, 0))
        _btn(toolbar, "🗑 Clear All", self._clear_activity,
             color=DANGER_BG, fg=DANGER, small=True).pack(side="left", padx=(8, 0))

        # Search box (right side of toolbar, before _make_tree so we hook it up later)
        search_wrap = tk.Frame(toolbar, bg=SURFACE3,
                               highlightthickness=1,
                               highlightbackground=BORDER2,
                               highlightcolor=ACCENT)
        search_wrap.pack(side="right")
        tk.Label(search_wrap, text="⌕",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=SURFACE3, padx=8).pack(side="left")
        search_entry = tk.Entry(search_wrap, textvariable=self._activity_search_var,
                                width=22,
                                bg=SURFACE3, fg=TEXT,
                                insertbackground=ACCENT,
                                relief="flat", bd=0, font=F_BODY,
                                highlightthickness=0)
        search_entry.pack(side="left", ipady=5, padx=(0, 8))
        search_entry.bind("<FocusIn>",  lambda _e: search_wrap.configure(highlightbackground=ACCENT))
        search_entry.bind("<FocusOut>", lambda _e: search_wrap.configure(highlightbackground=BORDER2))

        # ── Activity table ────────────────────────────────────────────────
        _act_cols = [
            ("timestamp",   "Date / Time",  145),
            ("event_type",  "Event",         105),
            ("username",    "Username",       100),
            ("ip",          "IP Address",      110),
            ("geo",         "Location",        130),
            ("device_name", "Device",          120),
            ("os_info",     "OS",              140),
            ("license_key", "License Key",     100),
            ("result",      "Result",          180),
        ]
        self._act_tree = self._make_tree(p, _act_cols)

        # Wire search to filter
        self._activity_search_var.trace_add("write", lambda *_: self._refresh_activity())

        self._refresh_activity()

    def _refresh_activity(self) -> None:
        """Reload the activity log table, applying current filter + search."""
        self._act_tree.delete(*self._act_tree.get_children())
        if al is None:
            self._row_count_vars[5].set("0")
            return

        _filter_map = {
            "All":              None,
            "Login Success":    "login_success",
            "Login Fail":       "login_fail",
            "Register Success": "register_success",
            "Register Fail":    "register_fail",
            "Admin Key OK":     "admin_key_login_success",
            "Admin Key Fail":   "admin_key_login_fail",
        }
        event_filter = _filter_map.get(self._activity_filter_var.get())
        search_q     = self._activity_search_var.get().strip().lower()

        rows = al.load_all()
        # Newest first
        rows = list(reversed(rows))

        displayed = 0
        for r in rows:
            if event_filter and r.get("event_type") != event_filter:
                continue
            if search_q:
                haystack = " ".join(str(v) for v in r.values()).lower()
                if search_q not in haystack:
                    continue

            ev = r.get("event_type", "")
            if ev.startswith("admin_key_login_success"):
                tag = "admin_key_ok_row"
            elif ev.startswith("admin_key_login_fail"):
                tag = "admin_key_fail_row"
            elif "success" in ev:
                tag = "success_row"
            elif "fail" in ev:
                tag = "fail_row"
            elif "register" in ev:
                tag = "reg_row"
            else:
                tag = ""

            self._act_tree.insert("", "end", tags=(tag,), values=(
                r.get("timestamp",   ""),
                ev,
                r.get("username",    ""),
                r.get("ip",          ""),
                r.get("geo",         ""),
                r.get("device_name", ""),
                r.get("os_info",     ""),
                r.get("license_key", ""),
                r.get("result",      ""),
            ))
            displayed += 1

        # Tag colours
        self._act_tree.tag_configure("success_row",
            background=SUCCESS_BG, foreground=SUCCESS)
        self._act_tree.tag_configure("fail_row",
            background=DANGER_BG,  foreground=DANGER)
        self._act_tree.tag_configure("reg_row",
            background=PURPLE_BG,  foreground=PURPLE)
        self._act_tree.tag_configure("admin_key_ok_row",
            background=WARNING_BG, foreground=GOLD)
        self._act_tree.tag_configure("admin_key_fail_row",
            background=DANGER_BG,  foreground=DANGER)

        self._row_count_vars[5].set(str(displayed))

    def _clear_activity(self) -> None:
        if al is None:
            messagebox.showinfo("Not available",
                "Activity log module not loaded.", parent=self); return
        if not messagebox.askyesno(
                "Clear Activity Log",
                "Permanently delete ALL activity log records?\n\nThis cannot be undone.",
                parent=self):
            return
        al.clear_all()
        _log("[admin] Activity log cleared.", "warn")
        self._refresh_activity()

    def _open_user_details(self) -> None:
        """Open a detail window for the currently selected activity-log row."""
        sel = self._act_tree.selection()
        if not sel:
            # Fall back to the Users tab selection if nothing selected in activity
            sel_u = self._users_tree.selection()
            if not sel_u:
                messagebox.showwarning(
                    "No Selection",
                    "Select a row in the Activity tab (or Users tab) first.",
                    parent=self)
                return
            username = self._users_tree.item(sel_u[0], "values")[0]
        else:
            username = self._act_tree.item(sel[0], "values")[2]  # username column

        if not username:
            messagebox.showwarning("No Username",
                "Could not determine username from selection.", parent=self); return
        _UserDetailsWindow(self, username)


    # ── Generic list tab (Blacklist / Whitelist) ──────────────────────────
    def _build_list_tab(self, parent, cols, load_fn, add_fn, remove_fn, attr) -> None:
        bg = BG
        tab_idx = 2 if attr == "_bl_tree" else 3
        heading = "BLACKLIST ENTRY" if attr == "_bl_tree" else "WHITELIST ENTRY"
        add_color = DANGER if attr == "_bl_tree" else SUCCESS
        bar_color = DANGER if attr == "_bl_tree" else SUCCESS
        add_fg    = BG

        add_card = tk.Frame(parent, bg=SURFACE,
                            highlightthickness=1, highlightbackground=BORDER2)
        add_card.pack(fill="x", padx=16, pady=(16, 8))

        top = tk.Frame(add_card, bg=SURFACE)
        top.pack(fill="x", padx=14, pady=(12, 6))
        lbl_bar = tk.Frame(top, bg=bar_color, width=3)
        lbl_bar.pack(side="left", fill="y", padx=(0, 8))
        tk.Label(top, text=f"ADD {heading}",
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=bar_color, bg=SURFACE).pack(side="left")

        form = tk.Frame(add_card, bg=SURFACE)
        form.pack(fill="x", padx=14, pady=(0, 14))

        tk.Label(form, text="Entry (HWID / IP / key)",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=0, sticky="w", pady=(0,3))
        ev = tk.StringVar(); setattr(self, attr+"_entry_var", ev)
        _entry(form, ev, width=30).grid(row=1, column=0, sticky="w",
                                        padx=(0,12), ipady=4)

        tk.Label(form, text="Note (optional)",
                 font=tkfont.Font(family="Segoe UI", size=8),
                 fg=TEXT_MUTED, bg=SURFACE).grid(row=0, column=1, sticky="w", pady=(0,3))
        nv = tk.StringVar(); setattr(self, attr+"_note_var", nv)
        _entry(form, nv, width=22).grid(row=1, column=1, sticky="w",
                                        padx=(0,12), ipady=4)

        tk.Label(form, text="", bg=SURFACE).grid(row=0, column=2, pady=(0,3))
        add_label = "+ Blacklist" if attr == "_bl_tree" else "+ Whitelist"
        _btn(form, add_label, lambda: add_fn(ev, nv),
             color=add_color, fg=add_fg).grid(row=1, column=2, sticky="w")

        toolbar = self._make_toolbar(parent)
        _btn(toolbar, "↺ Refresh",
             lambda: self._refresh_list(getattr(self, attr), load_fn, cols),
             color=SURFACE3, fg=TEXT2, small=True).pack(side="left")
        _btn(toolbar, "✕ Remove",
             lambda: remove_fn(getattr(self, attr), load_fn, cols),
             color=DANGER_BG, fg=DANGER, small=True).pack(side="left", padx=(8,0))

        tree = self._make_tree(parent, cols); setattr(self, attr, tree)
        self._make_search(toolbar, tree, load_fn, cols)
        self._refresh_list(tree, load_fn, cols)

        # Update badge
        self._row_count_vars[tab_idx].set(str(len(tree.get_children())))

    def _bl_add(self, ev, nv):
        e = ev.get().strip()
        if not e:
            messagebox.showwarning("Empty", "Entry cannot be blank.", parent=self); return
        kg.blacklist_add(e, nv.get().strip()); ev.set(""); nv.set("")
        _log(f"[admin] Blacklisted: {e}", "warn")
        cols = [("entry","Entry",280),("note","Note",200),("date","Date",90)]
        self._refresh_list(self._bl_tree, kg.load_blacklist, cols)
        self._row_count_vars[2].set(str(len(self._bl_tree.get_children())))

    def _bl_remove(self, tree, load_fn, cols):
        sel = tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select an entry first.", parent=self); return
        kg.blacklist_remove(tree.item(sel[0],"values")[0])
        self._refresh_list(tree, load_fn, cols)
        self._row_count_vars[2].set(str(len(tree.get_children())))

    def _wl_add(self, ev, nv):
        e = ev.get().strip()
        if not e:
            messagebox.showwarning("Empty", "Entry cannot be blank.", parent=self); return
        kg.whitelist_add(e, nv.get().strip()); ev.set(""); nv.set("")
        _log(f"[admin] Whitelisted: {e}", "ok")
        cols = [("entry","Entry",280),("note","Note",200),("date","Date",90)]
        self._refresh_list(self._wl_tree, kg.load_whitelist, cols)
        self._row_count_vars[3].set(str(len(self._wl_tree.get_children())))

    def _wl_remove(self, tree, load_fn, cols):
        sel = tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select an entry first.", parent=self); return
        kg.whitelist_remove(tree.item(sel[0],"values")[0])
        self._refresh_list(tree, load_fn, cols)
        self._row_count_vars[3].set(str(len(tree.get_children())))

    @staticmethod
    def _refresh_list(tree, load_fn, cols):
        tree.delete(*tree.get_children())
        keys = [c[0] for c in cols]
        for r in load_fn():
            tree.insert("", "end", values=tuple(r.get(k,"") for k in keys))

    @staticmethod
    def _make_tree(parent, cols, tag_idx: int = -1) -> ttk.Treeview:
        """Builds a scrollable Treeview with sortable headings and coloured tags."""
        wrap = _frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        ids = [c[0] for c in cols]
        tree = ttk.Treeview(wrap, columns=ids, show="headings", selectmode="browse")

        # Scrollbars
        vsb = ttk.Scrollbar(wrap, orient="vertical",   command=tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # Column headings with sort-on-click
        for cid, hdr, w in cols:
            tree.heading(cid, text=hdr,
                         command=lambda c=cid, t=tree: AdminPanel._sort_tree(t, c, False))
            tree.column(cid, width=w, minwidth=40, anchor="w", stretch=True)

        # Row tag colours — dark theme
        tree.tag_configure("odd",      background=SURFACE)
        tree.tag_configure("even",     background=SURFACE2)
        tree.tag_configure("banned",   background=DANGER_BG,  foreground=DANGER)
        tree.tag_configure("admin_key",background=PURPLE_BG,  foreground=PURPLE)
        tree.tag_configure("pro_key",  background=ACCENT_LIT, foreground=ACCENT)

        # Alternating row recolouring on insert (via virtual event isn't available — do on pack)
        def _recolour():
            for i, iid in enumerate(tree.get_children()):
                existing = tree.item(iid, "tags")
                if not existing or existing == ("",):
                    tree.item(iid, tags=("even" if i % 2 == 0 else "odd",))
        tree.bind("<<TreeviewSelect>>", lambda _e: None)

        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        tree.pack(side="left",  fill="both", expand=True)

        # Rebind double-click to copy selected row to clipboard
        def _copy_row(_e):
            sel = tree.selection()
            if sel:
                vals = "\t".join(str(v) for v in tree.item(sel[0], "values"))
                tree.clipboard_clear(); tree.clipboard_append(vals)
        tree.bind("<Double-1>", _copy_row)

        return tree

    @staticmethod
    def _sort_tree(tree: ttk.Treeview, col: str, reverse: bool) -> None:
        """Sort tree contents by column when header is clicked."""
        data = [(tree.item(iid, "values"), iid) for iid in tree.get_children()]
        col_idx = tree["columns"].index(col)
        try:
            data.sort(key=lambda x: x[0][col_idx].lower(), reverse=reverse)
        except Exception:
            data.sort(key=lambda x: x[0][col_idx], reverse=reverse)
        for i, (_, iid) in enumerate(data):
            tree.move(iid, "", i)
            existing = tree.item(iid, "tags")
            # Only recolour rows without a semantic tag (banned / admin_key etc.)
            if not existing or set(existing) <= {"odd", "even", ""}:
                tree.item(iid, tags=("even" if i % 2 == 0 else "odd",))
        tree.heading(col, command=lambda c=col, t=tree:
                     AdminPanel._sort_tree(t, c, not reverse))



# ─────────────────────────────────────────────────────────────────────────────
# User Details Window  (shown from Activity tab → User Details button)
# ─────────────────────────────────────────────────────────────────────────────

class _UserDetailsWindow(tk.Toplevel):
    """
    Shows the recent login history and registered devices for one user.
    Only accessible from AdminPanel (which is already ADMIN-gated).
    No passwords, hashes, tokens, or secret keys are displayed.
    """

    def __init__(self, master: tk.Widget, username: str) -> None:
        super().__init__(master)
        self.title(f"User Details — {username}")
        self.configure(bg=BG)
        self.geometry("860x580")
        self.minsize(640, 400)
        self.resizable(True, True)
        self.grab_set()
        self._username = username
        self._build()
        self.update_idletasks()
        x = master.winfo_x() + (master.winfo_width()  - 860) // 2
        y = master.winfo_y() + (master.winfo_height() - 580) // 2
        self.geometry(f"+{x}+{y}")

    def _build(self) -> None:
        # ── Header ────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=SURFACE, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        logo = tk.Frame(hdr, bg=ACCENT, width=30, height=30)
        logo.place(x=16, rely=0.5, anchor="w")
        logo.pack_propagate(False)
        tk.Label(logo, text="U",
                 font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                 fg=BG, bg=ACCENT).place(relx=0.5, rely=0.5, anchor="center")

        title_f = tk.Frame(hdr, bg=SURFACE)
        title_f.place(x=56, rely=0.5, anchor="w")
        tk.Label(title_f, text=self._username,
                 font=tkfont.Font(family="Segoe UI", size=11, weight="bold"),
                 fg=TEXT, bg=SURFACE).pack(side="left")
        tk.Label(title_f, text="  ·  recent activity & devices",
                 font=tkfont.Font(family="Segoe UI", size=9),
                 fg=TEXT_MUTED, bg=SURFACE).pack(side="left")

        close_lbl = tk.Label(hdr, text="✕",
                             font=tkfont.Font(family="Segoe UI", size=11),
                             fg=TEXT_MUTED, bg=SURFACE, padx=18, cursor="hand2")
        close_lbl.pack(side="right", fill="y")
        close_lbl.bind("<Button-1>", lambda _e: self.destroy())
        close_lbl.bind("<Enter>",    lambda _e: close_lbl.configure(fg=DANGER, bg=DANGER_BG))
        close_lbl.bind("<Leave>",    lambda _e: close_lbl.configure(fg=TEXT_MUTED, bg=SURFACE))

        _sep(self, color=BORDER).pack(fill="x")

        # ── Summary row from users.json ───────────────────────────────────
        users = kg.load_all_users()
        user_rec = next((u for u in users
                         if u.get("username", "").lower() == self._username.lower()),
                        None)

        summary_f = tk.Frame(self, bg=BG)
        summary_f.pack(fill="x", padx=16, pady=(12, 4))

        def _kv(parent, key, val):
            row = tk.Frame(parent, bg=SURFACE,
                           highlightthickness=1, highlightbackground=BORDER,
                           padx=12, pady=8)
            row.pack(side="left", padx=(0, 8))
            tk.Label(row, text=key.upper(),
                     font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                     fg=TEXT_MUTED, bg=SURFACE).pack(anchor="w")
            tk.Label(row, text=val,
                     font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                     fg=TEXT, bg=SURFACE).pack(anchor="w")

        if user_rec:
            _kv(summary_f, "Tier",    user_rec.get("tier", "—"))
            _kv(summary_f, "Joined",  user_rec.get("created", "—"))
            key_masked = user_rec.get("key", "")
            if len(key_masked) > 8:
                key_masked = key_masked[:8] + "…"
            _kv(summary_f, "Key",     key_masked)
        else:
            tk.Label(summary_f, text="No user record found in users.json",
                     font=F_SMALL, fg=TEXT_MUTED, bg=BG).pack(anchor="w")

        _sep(self, color=BORDER).pack(fill="x", pady=(8, 0))

        # ── Two-panel split: login history | devices ──────────────────────
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        # Left: login history
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(left, text="RECENT LOGIN HISTORY",
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=TEXT_MUTED, bg=BG, anchor="w").pack(fill="x", pady=(0, 6))

        hist_cols = [
            ("timestamp",  "Date / Time",  140),
            ("event_type", "Event",          100),
            ("ip",         "IP",              100),
            ("geo",        "Location",        120),
            ("result",     "Result",          150),
        ]
        hist_tree = self._make_tree(left, hist_cols)

        # Right: devices
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right, text="REGISTERED DEVICES",
                 font=tkfont.Font(family="Segoe UI", size=7, weight="bold"),
                 fg=TEXT_MUTED, bg=BG, anchor="w").pack(fill="x", pady=(0, 6))

        dev_cols = [
            ("device_name", "Device",  130),
            ("os_info",     "OS",       140),
            ("ip",          "IP",        90),
            ("last_seen",   "Last Seen", 130),
        ]
        dev_tree = self._make_tree(right, dev_cols)

        # Populate
        if al is not None:
            history = al.load_for_user(self._username)
            history = list(reversed(history))[:200]  # newest 200
            for r in history:
                ev = r.get("event_type", "")
                tag = ("success_row" if "success" in ev
                       else "fail_row" if "fail" in ev else "")
                hist_tree.insert("", "end", tags=(tag,), values=(
                    r.get("timestamp",  ""),
                    ev,
                    r.get("ip",         ""),
                    r.get("geo",        ""),
                    r.get("result",     ""),
                ))

            hist_tree.tag_configure("success_row",
                background=SUCCESS_BG, foreground=SUCCESS)
            hist_tree.tag_configure("fail_row",
                background=DANGER_BG,  foreground=DANGER)

            devices = al.load_devices_for_user(self._username)
            for d in devices:
                dev_tree.insert("", "end", values=(
                    d.get("device_name", ""),
                    d.get("os_info",     ""),
                    d.get("ip",          ""),
                    d.get("last_seen",   ""),
                ))
        else:
            hist_tree.insert("", "end", values=(
                "activity_log module not loaded", "", "", "", ""))

    @staticmethod
    def _make_tree(parent: tk.Widget, cols: list) -> ttk.Treeview:
        """Lightweight scrollable tree for the details window."""
        wrap = _frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True)

        ids  = [c[0] for c in cols]
        tree = ttk.Treeview(wrap, columns=ids, show="headings", selectmode="browse")

        vsb = ttk.Scrollbar(wrap, orient="vertical",   command=tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        for cid, hdr, w in cols:
            tree.heading(cid, text=hdr)
            tree.column(cid, width=w, minwidth=40, anchor="w", stretch=True)

        tree.tag_configure("odd",  background=SURFACE)
        tree.tag_configure("even", background=SURFACE2)

        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        tree.pack(side="left",  fill="both", expand=True)
        return tree


# ─────────────────────────────────────────────────────────────────────────────
# Auth Screen
# ─────────────────────────────────────────────────────────────────────────────

_CHANGELOG = """\
GhostConfig
Build 4.0  |  Windows Only

  \u2022 Tabbed UI: Dashboard, Spoofer, Admin, Settings, Support
  \u2022 Login & Register screen
  \u2022 User accounts bound to license keys
  \u2022 HMAC-SHA256 offline key validation
  \u2022 ADMIN key unlocks Admin Panel
  \u2022 Ban / blacklist / whitelist system
  \u2022 Registry GUID rotate & MAC spoof
  \u2022 Volume serial query via ctypes
  \u2022 Auto .reg backup before every write
  \u2022 PyInstaller single-file exe (UAC)
  \u2022 Full dark theme
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
            ("◆",  "Admin Panel",  self._build_admin_tab,    (tier,)),
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

        # Right side: status indicator + admin button (if ADMIN)
        if tier == "ADMIN":
            ab = tk.Button(
                topbar, text="Admin Panel",
                command=self._open_admin,
                bg=ACCENT, fg=WHITE,
                activebackground=ACCENT_HOV, activeforeground=WHITE,
                relief="flat", bd=0, padx=14, pady=6,
                font=tkfont.Font(family="Segoe UI", size=9, weight="bold"),
                cursor="hand2", highlightthickness=0,
            )
            ab.pack(side="right", padx=(0, 20), pady=12)
            ab.bind("<Enter>", lambda _e: ab.configure(bg=ACCENT_HOV))
            ab.bind("<Leave>", lambda _e: ab.configure(bg=ACCENT))

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
        ("Admin Panel",  "Key and user management"),
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

    def _open_admin(self) -> None:
        AdminPanel(self)

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
    # TAB 3 — Admin Panel tab (in-app, ADMIN tier shows button; others see message)
    # ─────────────────────────────────────────────────────────────────────
    def _build_admin_tab(self, parent: tk.Frame, tier: str) -> None:
        pad = _frame(parent, bg=BG)
        pad.pack(expand=True, fill="both")

        if tier == "ADMIN":
            # Glowing icon
            tk.Label(pad, text="◆",
                     font=tkfont.Font(family="Segoe UI", size=44),
                     fg=GOLD, bg=BG).pack(pady=(56, 6))
            tk.Label(pad, text="Admin Panel",
                     font=tkfont.Font(family="Segoe UI Semibold", size=20,
                                      weight="bold"),
                     fg=GOLD, bg=BG).pack()
            tk.Label(pad, text="Open the full admin panel to manage keys, users, bans and lists.",
                     font=F_BODY, fg=TEXT_MUTED, bg=BG).pack(pady=8)
            _sep(pad, color=BORDER).pack(fill="x", padx=100, pady=14)
            _btn(pad, "⚙  Open Admin Panel", self._open_admin,
                 color=GOLD, fg=BG).pack(pady=6)
        else:
            tk.Label(pad, text="◎",
                     font=tkfont.Font(family="Segoe UI", size=44),
                     fg=TEXT_MUTED, bg=BG).pack(pady=(56, 6))
            tk.Label(pad, text="Admin Panel",
                     font=tkfont.Font(family="Segoe UI Semibold", size=20,
                                      weight="bold"),
                     fg=TEXT_MUTED, bg=BG).pack()
            tk.Label(pad, text="This area requires an ADMIN-tier license key.",
                     font=F_BODY, fg=TEXT_MUTED, bg=BG).pack(pady=8)

    # ─────────────────────────────────────────────────────────────────────
    # TAB 5 — Settings  (redesigned with toggles, sliders & icon rows)
    # ─────────────────────────────────────────────────────────────────────
    def _build_settings(self, parent: tk.Frame) -> None:
        PAGE_IDX = 5   # Dashboard=0,Spoofer=1,Devices=2,TaskMgr=3,Admin=4,Settings=5
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

        # ── ABOUT ──────────────────────────────────────────────────────────
        _sh("About", "◆")
        ab = _sc()
        _sr(ab, "Application", val="GhostConfig",          val_color=TEXT)
        _sr(ab, "Version",     val="4.0",                   val_color=ACCENT)
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
            ("What is the admin key / master key?",
             f"Admin master key: {kg.ADMIN_MASTER_KEY}\n"
             "Use it to register/login as ADMIN. It unlocks the Admin Panel."),
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
        self._page_canvases[6] = canvas   # Support is page 6

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
                         text=f"  — {lbl_txt} —",
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
        if v in ("N/A", "—", "UNKNOWN", ""):
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
        if fl == "type" and v in ("WI-FI", "WI-FI"):
            return ACCENT_LIT
        return TEXT

    def _devices_refresh(self) -> None:
        """Kick off background collection for all sections."""
        self._dev_status_var.set("Refreshing hardware info…")
        self._dev_refresh_btn.configure(state="disabled", text="⟳  Refreshing…")

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
        self._dev_refresh_btn.configure(state="normal", text="⟳  Refresh All")
        # Schedule auto-refresh in 60 s
        self.after(60_000, self._devices_refresh)

    # ─────────────────────────────────────────────────────────────────────
    # Log pump
    # ─────────────────────────────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────
    # Async runner
    # ─────────────────────────────────────────────────────────────────────
    def _async(self, fn: Callable, *args) -> None:
        def _w():
            try:
                fn(*args)
            except PermissionError as exc:
                _log(f"[permission] {exc}", "error")
            except Exception as exc:
                _log(f"[error] {exc}", "error")
        threading.Thread(target=_w, daemon=True).start()

    # ── legacy GUID / MAC helpers kept for dashboard compatibility ─────────────

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
    # ─────────────────────────────────────────────────────────────────────
    # Backup label helper
    # ─────────────────────────────────────────────────────────────────────
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
