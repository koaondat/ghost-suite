"""
ui/widgets.py — Reusable premium widget primitives
===================================================
All widgets follow the Ghost design system (ui/theme.py).
Never import Tk directly from here — always rely on parent widgets.
"""

from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Optional

from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, SURFACE4, BORDER, BORDER2,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM, ACCENT_DARK,
    SUCCESS, SUCCESS_DIM, DANGER, DANGER_DIM, WARNING, WARNING_DIM,
    PAD, RADIUS,
    F_BODY, F_SMALL, F_BOLD, F_H2, F_LABEL, F_BIG, F_MONO, F_XLARGE,
)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def frame(parent: tk.Widget, bg: str = BG, **kw) -> tk.Frame:
    return tk.Frame(parent, bg=bg, **kw)


def label(parent: tk.Widget, text: str = "", bg: str = BG,
          fg: str = TEXT, font=None, **kw) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg, fg=fg,
                    font=font or F_BODY, **kw)


def sep(parent: tk.Widget, color: str = BORDER) -> tk.Frame:
    """Hairline horizontal divider."""
    return tk.Frame(parent, bg=color, height=1)


# ── Card ──────────────────────────────────────────────────────────────────────

def card(parent: tk.Widget, bg: str = SURFACE, **kw) -> tk.Frame:
    """
    A rounded-looking card — flat Tkinter Frame with a 1px accent border.
    """
    return tk.Frame(
        parent, bg=bg,
        highlightthickness=1,
        highlightbackground=BORDER,
        **kw,
    )


# ── Primary button ────────────────────────────────────────────────────────────

class Button(tk.Button):
    """
    Flat, hover-animated button.
    Variants: primary (accent fill), secondary (surface outline), danger.
    """

    def __init__(self, parent: tk.Widget, text: str = "",
                 command: Callable | None = None,
                 variant: str = "primary",   # "primary" | "secondary" | "danger" | "ghost"
                 small: bool = False,
                 **kw):
        px = 12 if small else 20
        py = 5  if small else 9
        fn = F_SMALL if small else F_BOLD

        colors = {
            "primary":   (ACCENT,    ACCENT_HOV,  WHITE),
            "secondary": (SURFACE3,  SURFACE4,    TEXT2),
            "danger":    (DANGER_DIM, DANGER,      DANGER),
            "ghost":     (BG,        SURFACE3,     TEXT_MUTED),
        }
        bg, hov, fg = colors.get(variant, colors["primary"])

        super().__init__(
            parent, text=text, command=command or (lambda: None),
            bg=bg, fg=fg, activebackground=hov, activeforeground=fg,
            relief="flat", bd=0, padx=px, pady=py, font=fn,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=ACCENT if variant == "primary" else BORDER,
            **kw,
        )
        self._bg  = bg
        self._hov = hov
        self.bind("<Enter>", lambda _e: self.configure(bg=self._hov))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._bg))

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.configure(state="normal", bg=self._bg)
        else:
            self.configure(state="disabled", bg=SURFACE3)


# ── Animated toggle switch ────────────────────────────────────────────────────

class Toggle(tk.Canvas):
    """
    iOS-style toggle switch.  Width 42, height 22.
    Calls `on_change(bool)` when clicked.
    """

    W, H, R = 42, 22, 11

    def __init__(self, parent: tk.Widget,
                 value: bool = True,
                 on_change: Callable[[bool], None] | None = None,
                 **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=kw.pop("bg", SURFACE), bd=0,
                         highlightthickness=0, cursor="hand2", **kw)
        self._value     = value
        self._on_change = on_change
        self._animating = False
        self._thumb_x   = self._target_x(value)
        self._draw()
        self.bind("<Button-1>", self._on_click)

    def _target_x(self, v: bool) -> int:
        return self.W - self.R - 2 if v else self.R + 2

    def _draw(self) -> None:
        self.delete("all")
        fill = ACCENT if self._value else SURFACE3
        # Track
        self.create_rounded_rect(1, 1, self.W - 1, self.H - 1, radius=self.R, fill=fill)
        # Thumb
        cx = self._thumb_x
        cy = self.H // 2
        r  = self.R - 3
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=WHITE, outline="")

    def create_rounded_rect(self, x1: int, y1: int, x2: int, y2: int,
                             radius: int, **kw) -> None:
        d = radius * 2
        self.create_arc(x1,      y1,      x1 + d,  y1 + d,  start=90,  extent=90,  **kw, outline="")
        self.create_arc(x2 - d,  y1,      x2,      y1 + d,  start=0,   extent=90,  **kw, outline="")
        self.create_arc(x1,      y2 - d,  x1 + d,  y2,      start=180, extent=90,  **kw, outline="")
        self.create_arc(x2 - d,  y2 - d,  x2,      y2,      start=270, extent=90,  **kw, outline="")
        self.create_rectangle(x1 + radius, y1,      x2 - radius, y2,      **kw, outline="")
        self.create_rectangle(x1,          y1 + radius, x2, y2 - radius, **kw, outline="")

    def _on_click(self, _event: tk.Event) -> None:
        self._value = not self._value
        if self._on_change:
            self._on_change(self._value)
        self._animate()

    def _animate(self) -> None:
        target = self._target_x(self._value)
        step   = 3 if self._value else -3
        if abs(self._thumb_x - target) > abs(step):
            self._thumb_x += step
            self._draw()
            self.after(12, self._animate)
        else:
            self._thumb_x = target
            self._draw()

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value  = value
        self._thumb_x = self._target_x(value)
        self._draw()


# ── Progress bar ──────────────────────────────────────────────────────────────

class ProgressBar(tk.Canvas):
    """Smooth animated horizontal progress bar."""

    def __init__(self, parent: tk.Widget, height: int = 6,
                 color: str = ACCENT, **kw):
        super().__init__(parent, height=height, bd=0,
                         bg=kw.pop("bg", SURFACE3),
                         highlightthickness=0, **kw)
        self._color   = color
        self._pct     = 0
        self._target  = 0
        self._bind_configure()

    def _bind_configure(self) -> None:
        self.bind("<Configure>", lambda _e: self._redraw())

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width() or 200
        h = self.winfo_height() or 6
        filled = int(w * self._pct / 100)
        if filled > 0:
            self.create_rectangle(0, 0, filled, h, fill=self._color, outline="")

    def set_progress(self, pct: int) -> None:
        self._target = max(0, min(100, pct))
        self._animate_to()

    def _animate_to(self) -> None:
        if abs(self._pct - self._target) > 1:
            self._pct += (self._target - self._pct) * 0.18
            self._redraw()
            self.after(16, self._animate_to)
        else:
            self._pct = self._target
            self._redraw()


# ── Status dot ────────────────────────────────────────────────────────────────

class StatusDot(tk.Canvas):
    """Pulsing colored dot: 12×12."""

    def __init__(self, parent: tk.Widget, color: str = SUCCESS,
                 pulse: bool = False, **kw):
        super().__init__(parent, width=12, height=12, bd=0,
                         bg=kw.pop("bg", BG),
                         highlightthickness=0, **kw)
        self._color  = color
        self._pulse  = pulse
        self._bright = True
        self._draw()
        if pulse:
            self._do_pulse()

    def _draw(self) -> None:
        self.delete("all")
        c = self._color if self._bright else _dim_color(self._color)
        self.create_oval(2, 2, 10, 10, fill=c, outline="")

    def _do_pulse(self) -> None:
        self._bright = not self._bright
        self._draw()
        self.after(900, self._do_pulse)

    def set_color(self, color: str, pulse: bool | None = None) -> None:
        self._color = color
        if pulse is not None:
            self._pulse = pulse
        self._bright = True
        self._draw()


def _dim_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"#{r//3:02x}{g//3:02x}{b//3:02x}"


# ── Toast notification ────────────────────────────────────────────────────────

class Toast:
    """
    Small non-blocking notification that auto-dismisses after `duration_ms`.
    Creates a Toplevel anchored to the bottom-right of `root`.
    """

    _ACTIVE: list["Toast"] = []

    def __init__(self, root: tk.Tk | tk.Toplevel,
                 message: str,
                 variant: str = "success",   # "success"|"error"|"info"|"warning"
                 duration_ms: int = 3000):
        colors = {
            "success": (SUCCESS_DIM, SUCCESS, "✓"),
            "error":   (DANGER_DIM,  DANGER,  "✕"),
            "info":    (SURFACE2,    TEXT2,   "ℹ"),
            "warning": (WARNING_DIM, WARNING, "⚠"),
        }
        bg, fg, icon = colors.get(variant, colors["success"])

        self._root = root
        self._win  = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.configure(bg=bg)

        inner = tk.Frame(self._win, bg=bg)
        inner.pack(padx=0, pady=0)
        tk.Label(inner, text=icon, bg=bg, fg=fg,
                 font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                 padx=10, pady=10).pack(side="left")
        tk.Label(inner, text=message, bg=bg, fg=fg,
                 font=tkfont.Font(family="Segoe UI", size=9),
                 pady=10, padx=0).pack(side="left")
        tk.Label(inner, text="  ", bg=bg).pack(side="left")

        self._position()
        Toast._ACTIVE.append(self)
        root.after(duration_ms, self._dismiss)

    def _position(self) -> None:
        self._win.update_idletasks()
        rw = self._root.winfo_width()
        rh = self._root.winfo_height()
        rx = self._root.winfo_x()
        ry = self._root.winfo_y()
        tw = self._win.winfo_reqwidth()
        th = self._win.winfo_reqheight()
        offset_y = sum(26 for t in Toast._ACTIVE if t is not self) + 12
        x = rx + rw - tw - 16
        y = ry + rh - th - 60 - offset_y
        self._win.geometry(f"+{x}+{y}")

    def _dismiss(self) -> None:
        try:
            self._win.destroy()
        except Exception:
            pass
        try:
            Toast._ACTIVE.remove(self)
        except ValueError:
            pass


# ── Skeleton loader ───────────────────────────────────────────────────────────

class Skeleton(tk.Canvas):
    """Animated shimmer placeholder while content loads."""

    def __init__(self, parent: tk.Widget, width: int, height: int, **kw):
        super().__init__(parent, width=width, height=height, bd=0,
                         bg=kw.pop("bg", SURFACE),
                         highlightthickness=0, **kw)
        self._offset = 0
        self._w      = width
        self._h      = height
        self._create_base()
        self._shimmer()

    def _create_base(self) -> None:
        self.create_rectangle(0, 0, self._w, self._h,
                               fill=SURFACE3, outline="")

    def _shimmer(self) -> None:
        self.delete("shimmer")
        x = self._offset % (self._w + 60) - 60
        self.create_rectangle(x, 0, x + 60, self._h,
                               fill=BORDER2, outline="", tags="shimmer")
        self._offset += 4
        self.after(30, self._shimmer)


# ── Section label ─────────────────────────────────────────────────────────────

def section_label(parent: tk.Widget, text: str, bg: str = SURFACE) -> tk.Label:
    """UPPERCASE muted section header."""
    return tk.Label(
        parent, text=text.upper(),
        bg=bg, fg=TEXT_DIM,
        font=F_LABEL, anchor="w",
    )


# ── Stat card (small metric card) ─────────────────────────────────────────────

def stat_card(parent: tk.Widget,
              label_text: str,
              value_text: str,
              sub_text:   str = "",
              accent_color: str = ACCENT,
              bg: str = SURFACE) -> tk.Frame:
    """
    Small card showing:
      LABEL
      Value (large)
      sub text (muted)
    """
    c = card(parent, bg=bg)
    inner = frame(c, bg=bg)
    inner.pack(fill="both", padx=18, pady=14)

    tk.Label(inner, text=label_text.upper(), bg=bg, fg=TEXT_DIM,
             font=F_LABEL, anchor="w").pack(anchor="w")
    tk.Label(inner, text=value_text, bg=bg, fg=accent_color,
             font=tkfont.Font(family="Segoe UI", size=18, weight="bold"),
             anchor="w").pack(anchor="w", pady=(2, 0))
    if sub_text:
        tk.Label(inner, text=sub_text, bg=bg, fg=TEXT_MUTED,
                 font=F_SMALL, anchor="w").pack(anchor="w")
    return c


# ── Confirmation dialog ───────────────────────────────────────────────────────

def confirm_dialog(root: tk.Tk | tk.Toplevel,
                   title: str,
                   message: str,
                   confirm_label: str = "Confirm",
                   cancel_label:  str = "Cancel",
                   danger: bool = False) -> bool:
    """
    Blocking modal confirmation dialog.
    Returns True if the user confirmed, False otherwise.
    """
    result: list[bool] = [False]
    dlg = tk.Toplevel(root)
    dlg.title(title)
    dlg.configure(bg=SURFACE)
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.transient(root)

    # Header accent stripe
    tk.Frame(dlg, bg=DANGER if danger else ACCENT, height=3).pack(fill="x")

    body = frame(dlg, bg=SURFACE)
    body.pack(fill="both", padx=24, pady=20)

    tk.Label(body, text=title, bg=SURFACE, fg=TEXT,
             font=tkfont.Font(family="Segoe UI", size=12, weight="bold")).pack(anchor="w")
    tk.Label(body, text=message, bg=SURFACE, fg=TEXT_MUTED,
             font=F_BODY, wraplength=340, justify="left").pack(anchor="w", pady=(6, 16))

    sep(body, BORDER).pack(fill="x", pady=(0, 14))

    btn_row = frame(body, bg=SURFACE)
    btn_row.pack(fill="x")

    def _confirm():
        result[0] = True
        dlg.destroy()

    confirm_color = DANGER if danger else ACCENT
    confirm_hov   = "#c53030" if danger else ACCENT_HOV

    cb = tk.Button(btn_row, text=confirm_label,
                   bg=confirm_color, fg=WHITE,
                   activebackground=confirm_hov, activeforeground=WHITE,
                   relief="flat", bd=0, cursor="hand2", padx=16, pady=8,
                   font=F_BOLD, command=_confirm)
    cb.pack(side="right")
    cb.bind("<Enter>", lambda _e: cb.configure(bg=confirm_hov))
    cb.bind("<Leave>", lambda _e: cb.configure(bg=confirm_color))

    tk.Button(btn_row, text=cancel_label,
              bg=SURFACE3, fg=TEXT_MUTED,
              activebackground=BORDER, activeforeground=TEXT,
              relief="flat", bd=0, cursor="hand2", padx=16, pady=8,
              font=F_BODY, command=dlg.destroy).pack(side="right", padx=(0, 8))

    # Center over parent
    dlg.update_idletasks()
    w, h = 380, dlg.winfo_reqheight() + 10
    px = root.winfo_x() + (root.winfo_width()  - w) // 2
    py = root.winfo_y() + (root.winfo_height() - h) // 2
    dlg.geometry(f"{w}x{h}+{px}+{py}")

    root.wait_window(dlg)
    return result[0]
