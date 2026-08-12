"""
ui/main_window.py — Application shell
=======================================
Custom frameless window with:
  • Custom title bar (drag, minimize, maximize, close)
  • Left sidebar (Dashboard, Spoofer, Settings + footer items)
  • Right page area (swaps pages without destroying them)
  • Connection status indicator in sidebar footer
  • Toast / update dialog integration
"""

from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

import license_service
import updater
from app_config import (
    APP_VERSION, PRODUCT_NAME, WINDOW_MIN_W, WINDOW_MIN_H, WINDOW_DEFAULT,
    DISCORD_URL, SUPPORT_URL,
)
from settings_manager import settings
from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, SURFACE4, BORDER, BORDER2,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM, ACCENT_DARK,
    SUCCESS, SUCCESS_DIM, DANGER, DANGER_DIM, WARNING,
    TOPBAR_H, SIDEBAR_W, PAD,
    F_BODY, F_BOLD, F_SMALL, F_H2, F_LABEL, F_TITLE, F_BIG,
)
from ui.widgets import frame, label, sep, Toast


# ── Sidebar nav item ──────────────────────────────────────────────────────────

class _NavItem:
    """Animated sidebar navigation item with left-accent indicator."""

    def __init__(self, parent: tk.Widget, icon: str, text: str,
                 on_click: Callable[[], None], bg: str = SURFACE) -> None:
        self._active  = False
        self._hovered = False
        self._bg      = bg
        self._click   = on_click

        self._outer = tk.Frame(parent, bg=bg, cursor="hand2")
        self._outer.pack(fill="x", padx=8, pady=1)

        self._bar = tk.Frame(self._outer, width=3, bg=bg)
        self._bar.pack(side="left", fill="y")

        self._inner = tk.Frame(self._outer, bg=bg, cursor="hand2")
        self._inner.pack(side="left", fill="both", expand=True)

        self._icon_lbl = tk.Label(
            self._inner, text=icon,
            font=tkfont.Font(family="Segoe UI Emoji", size=10),
            bg=bg, fg=TEXT_MUTED, padx=9, pady=7, cursor="hand2",
        )
        self._icon_lbl.pack(side="left")

        self._text_lbl = tk.Label(
            self._inner, text=text,
            font=tkfont.Font(family="Segoe UI", size=9),
            bg=bg, fg=TEXT_MUTED, anchor="w", cursor="hand2",
        )
        self._text_lbl.pack(side="left", fill="x", expand=True, pady=7)

        for w in (self._outer, self._inner, self._icon_lbl, self._text_lbl, self._bar):
            w.bind("<Button-1>", lambda _e: on_click())
            w.bind("<Enter>",    lambda _e: self._hover(True))
            w.bind("<Leave>",    lambda _e: self._hover(False))

    def _hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._refresh()

    def _refresh(self) -> None:
        if self._active:
            bg  = ACCENT_DIM
            fg  = ACCENT
            bar = ACCENT
            weight = "bold"
        elif self._hovered:
            bg  = SURFACE3
            fg  = TEXT2
            bar = self._bg
            weight = "normal"
        else:
            bg  = self._bg
            fg  = TEXT_MUTED
            bar = self._bg
            weight = "normal"

        for w in (self._outer, self._inner, self._icon_lbl, self._text_lbl):
            w.configure(bg=bg)
        self._icon_lbl.configure(fg=fg)
        self._text_lbl.configure(fg=fg,
            font=tkfont.Font(family="Segoe UI", size=9, weight=weight))
        self._bar.configure(bg=bar)

    def set_active(self, active: bool) -> None:
        self._active = active
        self._refresh()


# ── Connection badge ──────────────────────────────────────────────────────────

class _ConnectionBadge(tk.Frame):
    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, bg=SURFACE, **kw)
        self._dot = tk.Canvas(self, width=8, height=8, bg=SURFACE,
                              bd=0, highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 4))
        self._lbl = tk.Label(self, text="Online", bg=SURFACE,
                             fg=SUCCESS, font=F_SMALL)
        self._lbl.pack(side="left")
        self._set_dot(SUCCESS)

    def _set_dot(self, color: str) -> None:
        self._dot.delete("all")
        self._dot.create_oval(0, 0, 8, 8, fill=color, outline="")

    def set_online(self) -> None:
        self._set_dot(SUCCESS)
        self._lbl.configure(text="Online", fg=SUCCESS)

    def set_offline(self) -> None:
        self._set_dot(WARNING)
        self._lbl.configure(text="Offline", fg=WARNING)

    def set_error(self) -> None:
        self._set_dot(DANGER)
        self._lbl.configure(text="Error", fg=DANGER)


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(tk.Tk):
    """
    Root application window.
    Frameless with custom title bar, sidebar, and page container.
    """

    PAGE_DASHBOARD = 0
    PAGE_SPOOFER   = 1
    PAGE_SETTINGS  = 2

    def __init__(self,
                 token:   str,
                 username: str,
                 lic_info: license_service.LicenseInfo):
        super().__init__()
        self._token    = token
        self._username = username
        self._lic      = lic_info
        self._pages:     list[tk.Frame] = []
        self._nav_items: list[_NavItem] = []
        self._active_page = -1
        self._drag_x = self._drag_y = 0

        self._configure_window()
        self._build_ui()
        self._show_page(self.PAGE_DASHBOARD)

        # Background tasks
        self.after(2000, self._check_updates_bg)
        self.after(1000, self._refresh_conn_status)

    # ── Window setup ──────────────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self.title(PRODUCT_NAME)
        self.configure(bg=BG)
        self.overrideredirect(True)          # custom frame
        self.minsize(WINDOW_MIN_W, WINDOW_MIN_H)
        self.geometry(WINDOW_DEFAULT)
        # Center on screen
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w, h = int(WINDOW_DEFAULT.split("x")[0]), int(WINDOW_DEFAULT.split("x")[1].split("+")[0])
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        # Allow resize by binding to window edges
        self.bind("<Configure>", self._on_configure)
        # Tray icon support (best-effort)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Title bar ──────────────────────────────────────────────────
        self._titlebar = self._build_titlebar()

        # ── Body (sidebar + pages) ─────────────────────────────────────
        body = frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        sidebar = self._build_sidebar(body)
        sidebar.pack(side="left", fill="y")

        # Thin separator line between sidebar and content
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        self._content = frame(body, bg=BG)
        self._content.pack(side="left", fill="both", expand=True)

        self._build_pages()

    def _build_titlebar(self) -> tk.Frame:
        bar = tk.Frame(self, bg=SURFACE, height=TOPBAR_H)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Drag
        bar.bind("<ButtonPress-1>",   self._drag_start)
        bar.bind("<B1-Motion>",       self._drag_move)

        # Logo + name
        logo_row = frame(bar, bg=SURFACE)
        logo_row.pack(side="left", padx=16, fill="y")

        # Small diamond logo
        logo_c = tk.Canvas(logo_row, width=18, height=18, bg=SURFACE,
                           bd=0, highlightthickness=0)
        logo_c.pack(side="left", padx=(0, 8))
        pts = [9, 1, 17, 9, 9, 17, 1, 9]
        logo_c.create_polygon(pts, fill=ACCENT, outline="")
        inner = [9, 5, 13, 9, 9, 13, 5, 9]
        logo_c.create_polygon(inner, fill=SURFACE, outline="")

        tk.Label(logo_row, text=PRODUCT_NAME, bg=SURFACE, fg=WHITE,
                 font=tkfont.Font(family="Segoe UI", size=10, weight="bold")
                 ).pack(side="left")

        # Separator
        tk.Frame(logo_row, bg=BORDER, width=1, height=18).pack(side="left", padx=12)

        self._title_ver = tk.Label(logo_row, text=f"v{APP_VERSION}",
                                   bg=SURFACE, fg=TEXT_DIM, font=F_SMALL)
        self._title_ver.pack(side="left")

        # Window controls (right side)
        ctrl = frame(bar, bg=SURFACE)
        ctrl.pack(side="right")

        def _wbtn(parent: tk.Frame, symbol: str, cmd: Callable,
                  hover: str = SURFACE3) -> tk.Label:
            lbl = tk.Label(parent, text=symbol, bg=SURFACE, fg=TEXT_MUTED,
                           font=tkfont.Font(family="Segoe UI", size=11),
                           padx=14, pady=0, height=1, cursor="hand2")
            lbl.pack(side="left", fill="y", ipady=15)
            lbl.bind("<Button-1>", lambda _e: cmd())
            lbl.bind("<Enter>",    lambda _e: lbl.configure(bg=hover, fg=TEXT))
            lbl.bind("<Leave>",    lambda _e: lbl.configure(bg=SURFACE, fg=TEXT_MUTED))
            return lbl

        _wbtn(ctrl, "─",  self._minimize)
        _wbtn(ctrl, "□",  self._toggle_maximize)
        _wbtn(ctrl, "✕",  self._on_close, hover="#c0392b")

        return bar

    def _build_sidebar(self, parent: tk.Widget) -> tk.Frame:
        sb = tk.Frame(parent, bg=SURFACE, width=SIDEBAR_W)
        sb.pack_propagate(False)

        # Top padding
        frame(sb, bg=SURFACE, height=12).pack()

        # Main nav
        nav_items = [
            ("⊞",  "Dashboard", self.PAGE_DASHBOARD),
            ("⚙",  "Spoofer",   self.PAGE_SPOOFER),
            ("≡",  "Settings",  self.PAGE_SETTINGS),
        ]
        for icon, text, page_idx in nav_items:
            item = _NavItem(sb, icon, text,
                            on_click=lambda p=page_idx: self._show_page(p),
                            bg=SURFACE)
            self._nav_items.append(item)

        # Filler
        frame(sb, bg=SURFACE).pack(fill="both", expand=True)

        # Footer separator
        sep(sb, BORDER).pack(fill="x", padx=16, pady=4)

        # Footer items
        footer_items = [
            ("?",  "Support",  self._open_support),
        ]
        for icon, text, cmd in footer_items:
            fi = _NavItem(sb, icon, text, on_click=cmd, bg=SURFACE)
            # Footer items don't participate in page selection
            self._nav_items.append(fi)

        # Version tag
        ver_frame = frame(sb, bg=SURFACE)
        ver_frame.pack(fill="x", padx=14, pady=4)
        tk.Label(ver_frame, text=f"Version {APP_VERSION}", bg=SURFACE, fg=TEXT_DIM,
                 font=F_SMALL, anchor="w").pack(side="left")

        # Connection indicator
        self._conn_badge = _ConnectionBadge(sb)
        self._conn_badge.pack(padx=14, pady=(4, 0), anchor="w")

        # User profile row
        sep(sb, BORDER).pack(fill="x", padx=16, pady=6)
        profile = frame(sb, bg=SURFACE)
        profile.pack(fill="x", padx=14, pady=(0, 14))

        # Avatar circle
        av = tk.Canvas(profile, width=30, height=30, bg=SURFACE,
                       bd=0, highlightthickness=0)
        av.pack(side="left", padx=(0, 8))
        av.create_oval(0, 0, 30, 30, fill=ACCENT_DIM, outline=ACCENT)
        initial = (self._username[0].upper() if self._username else "?")
        av.create_text(15, 15, text=initial, fill=ACCENT,
                       font=tkfont.Font(family="Segoe UI", size=10, weight="bold"))

        user_info = frame(profile, bg=SURFACE)
        user_info.pack(side="left", fill="x", expand=True)
        self._uname_lbl = tk.Label(user_info, text=self._username,
                                   bg=SURFACE, fg=TEXT, font=F_BOLD, anchor="w")
        self._uname_lbl.pack(fill="x")
        self._tier_lbl  = tk.Label(user_info, text=self._lic.tier,
                                   bg=SURFACE, fg=TEXT_MUTED, font=F_SMALL, anchor="w")
        self._tier_lbl.pack(fill="x")

        return sb

    def _build_pages(self) -> None:
        from ui.pages.dashboard import DashboardPage
        from ui.pages.spoofer   import SpooferPage
        from ui.pages.settings  import SettingsPage

        page_classes = [DashboardPage, SpooferPage, SettingsPage]
        for cls in page_classes:
            pg = cls(
                self._content,
                token=self._token,
                username=self._username,
                lic_info=self._lic,
                app=self,
            )
            pg.pack(fill="both", expand=True)
            pg.pack_forget()
            self._pages.append(pg)

    # ── Page switching ────────────────────────────────────────────────────────

    def _show_page(self, idx: int) -> None:
        if idx == self._active_page:
            return
        for i, pg in enumerate(self._pages):
            if i == idx:
                pg.pack(fill="both", expand=True)
                if hasattr(pg, "on_show"):
                    pg.on_show()
            else:
                pg.pack_forget()

        for i, ni in enumerate(self._nav_items):
            # Only the first 3 nav items correspond to pages
            ni.set_active(i < 3 and i == idx)

        self._active_page = idx

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _drag_move(self, event: tk.Event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.geometry(f"+{x}+{y}")

    # ── Window controls ───────────────────────────────────────────────────────

    def _minimize(self) -> None:
        self.overrideredirect(False)
        self.iconify()
        self.bind("<Map>", self._on_restore)

    def _on_restore(self, _event: tk.Event) -> None:
        self.overrideredirect(True)
        self.unbind("<Map>")

    def _toggle_maximize(self) -> None:
        if not hasattr(self, "_maximized"):
            self._maximized = False
        if self._maximized:
            self.geometry(WINDOW_DEFAULT)
            self._maximized = False
        else:
            self.state("zoomed")
            self._maximized = True

    def _on_configure(self, _event: tk.Event) -> None:
        pass   # resize handled natively by Tk geometry manager

    def _on_close(self) -> None:
        if settings.get("confirm_before_close"):
            from ui.widgets import confirm_dialog
            if not confirm_dialog(self, "Close Application",
                                  "Are you sure you want to close Ghost?",
                                  confirm_label="Close", danger=False):
                return
        if settings.get("minimize_to_tray"):
            self._minimize()
        else:
            self.destroy()

    def open_spoofer(self) -> None:
        self._show_page(self.PAGE_SPOOFER)

    def open_settings(self) -> None:
        self._show_page(self.PAGE_SETTINGS)

    def _open_support(self) -> None:
        import webbrowser
        webbrowser.open(SUPPORT_URL)

    # ── Update check ─────────────────────────────────────────────────────────

    def _check_updates_bg(self) -> None:
        if not settings.auto_update_check:
            return
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        release = updater.check_for_update(settings.update_channel)
        if release:
            self.after(0, lambda r=release: self._show_update_dialog(r))

    def _show_update_dialog(self, release: dict) -> None:
        from ui.update_dialog import UpdateDialog
        UpdateDialog(self, release)

    # ── Connection status ─────────────────────────────────────────────────────

    def _refresh_conn_status(self) -> None:
        def _check():
            import auth_manager as am
            result = am.validate_stored_session(self._token)
            ok = result.get("ok", False)
            self.after(0, lambda: (
                self._conn_badge.set_online() if ok else self._conn_badge.set_offline()
            ))
        threading.Thread(target=_check, daemon=True).start()
        self.after(60_000, self._refresh_conn_status)   # re-check every minute

    # ── License update callback ───────────────────────────────────────────────

    def update_license(self, lic: license_service.LicenseInfo) -> None:
        self._lic = lic
        self._tier_lbl.configure(text=lic.tier)
        # Propagate to dashboard page
        if self._pages and hasattr(self._pages[0], "update_license"):
            self._pages[0].update_license(lic)

    def toast(self, message: str, variant: str = "success") -> None:
        Toast(self, message, variant=variant)
