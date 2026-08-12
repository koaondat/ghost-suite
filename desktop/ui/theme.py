"""
ui/theme.py — Design system: colors, fonts, spacing, style helpers
===================================================================
Single source of truth for every visual constant.
The accent color is read from settings_manager at import time but can be
refreshed via Theme.reload_accent() when the user changes it in Settings.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings_manager import Settings

# ── Base palette ──────────────────────────────────────────────────────────────
BG         = "#0d0f11"       # near-black background
SURFACE    = "#161a1f"       # primary card / sidebar surface
SURFACE2   = "#1c2128"       # raised panels
SURFACE3   = "#222831"       # inputs, hover targets
SURFACE4   = "#282e38"       # subtle raised card
BORDER     = "#2a2f3a"       # thin card borders
BORDER2    = "#343c4a"       # slightly stronger
TEXT       = "#f0f2f5"       # primary white text
TEXT2      = "#c5cad4"       # secondary text
TEXT_MUTED = "#8891a0"       # labels, hints
TEXT_DIM   = "#505563"       # very muted, decorative
WHITE      = "#f0f2f5"

# ── Status / semantic ─────────────────────────────────────────────────────────
SUCCESS     = "#22c55e"
SUCCESS_DIM = "#0f3a22"
WARNING     = "#f59e0b"
WARNING_DIM = "#3a2a0a"
DANGER      = "#ef4444"
DANGER_DIM  = "#3a0f0f"
INFO        = "#38bdf8"
INFO_DIM    = "#0a2233"

# ── Accent (default red — overridden by settings) ─────────────────────────────
ACCENT      = "#e53e3e"
ACCENT_HOV  = "#c53030"
ACCENT_DARK = "#7f1d1d"
ACCENT_DIM  = "#2d1212"

# ── Spacing ───────────────────────────────────────────────────────────────────
PAD       = 20          # standard content padding
RADIUS    = 10          # visual card radius (border-radius for canvas arcs)
SIDEBAR_W = 224         # sidebar width
TOPBAR_H  = 48          # custom title bar height

# ── Font handles (populated by init_fonts) ────────────────────────────────────
F_TITLE:  tkfont.Font
F_BODY:   tkfont.Font
F_SMALL:  tkfont.Font
F_BOLD:   tkfont.Font
F_BIG:    tkfont.Font
F_H2:     tkfont.Font
F_LABEL:  tkfont.Font
F_MONO:   tkfont.Font
F_XLARGE: tkfont.Font
F_HUGE:   tkfont.Font


def init_fonts(root: tk.Misc) -> None:
    """Create all font objects.  Must be called once after the Tk root exists."""
    global F_TITLE, F_BODY, F_SMALL, F_BOLD, F_BIG, F_H2, F_LABEL, F_MONO, F_XLARGE, F_HUGE

    def _font(family: str, size: int, weight: str = "normal") -> tkfont.Font:
        return tkfont.Font(root=root, family=family, size=size, weight=weight)

    seg     = "Segoe UI"
    seg_sem = "Segoe UI Semibold"
    mono    = "Cascadia Code"

    # Fall back to Consolas if Cascadia Code isn't installed
    avail = tkfont.families(root)
    if mono not in avail:
        mono = "Consolas"

    F_BODY   = _font(seg,     10)
    F_SMALL  = _font(seg,     9)
    F_BOLD   = _font(seg,     10, "bold")
    F_TITLE  = _font(seg_sem, 11, "bold")
    F_H2     = _font(seg,     9,  "bold")
    F_LABEL  = _font(seg,     8,  "bold")
    F_MONO   = _font(mono,    9)
    F_BIG    = _font(seg,     22, "bold")
    F_XLARGE = _font(seg,     14, "bold")
    F_HUGE   = _font(seg,     32, "bold")


def reload_accent(hex_color: str) -> None:
    """Update the global accent color constants in-place."""
    global ACCENT, ACCENT_HOV, ACCENT_DARK, ACCENT_DIM
    ACCENT     = hex_color
    # Darken by mixing toward black
    ACCENT_HOV = _darken(hex_color, 0.15)
    ACCENT_DIM = _darken(hex_color, 0.75)
    ACCENT_DARK= _darken(hex_color, 0.55)


def _darken(hex_color: str, factor: float) -> str:
    """Mix hex_color with black by `factor` (0=original, 1=black)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = int(r * (1 - factor))
    g = int(g * (1 - factor))
    b = int(b * (1 - factor))
    return f"#{r:02x}{g:02x}{b:02x}"


# ── ttk style sheet ───────────────────────────────────────────────────────────

def apply_ttk_theme(root: tk.Misc) -> None:
    """Apply the Ghost dark theme to ttk widgets."""
    import tkinter.ttk as ttk
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".",
        background=SURFACE, foreground=TEXT,
        fieldbackground=SURFACE2, bordercolor=BORDER,
        troughcolor=BG, lightcolor=BORDER, darkcolor=BORDER,
        font=("Segoe UI", 9), relief="flat")

    # Scrollbar
    s.configure("TScrollbar",
        background=BORDER2, troughcolor=SURFACE,
        arrowcolor=TEXT_DIM, bordercolor=SURFACE,
        relief="flat", arrowsize=0, width=5)
    s.map("TScrollbar", background=[("active", TEXT_MUTED)])

    # Combobox
    s.configure("TCombobox",
        selectbackground=ACCENT_DIM, selectforeground=TEXT,
        fieldbackground=SURFACE2, background=SURFACE2,
        foreground=TEXT, arrowcolor=TEXT_MUTED,
        bordercolor=BORDER2, relief="flat", padding=5)
    s.map("TCombobox",
        fieldbackground=[("readonly", SURFACE2)],
        bordercolor=[("focus", ACCENT)])
