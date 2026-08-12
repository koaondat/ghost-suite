"""
ui/login_window.py — Premium login screen
==========================================
Shown on first launch or when the persisted session is invalid.
Single license-key entry (not username/password) — per the spec.
After successful activation it stores the JWT and calls on_success().
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

import auth_manager
import license_service
from app_config import (
    APP_VERSION, PRODUCT_NAME, PRODUCT_TAGLINE,
    PURCHASE_URL, SUPPORT_URL, DISCORD_URL, API_BASE_URL,
)
from ui.theme import (
    BG, SURFACE, SURFACE2, SURFACE3, BORDER, BORDER2,
    TEXT, TEXT2, TEXT_MUTED, TEXT_DIM, WHITE,
    ACCENT, ACCENT_HOV, ACCENT_DIM,
    DANGER, SUCCESS,
    TOPBAR_H, PAD,
    F_BODY, F_BOLD, F_SMALL, F_H2, F_TITLE, F_BIG, F_XLARGE,
)
from ui.widgets import frame, label, sep, Button


class LoginWindow(tk.Toplevel):
    """
    Minimal, beautiful login window.
    Calls `on_success(token, username, license_info)` on success.
    Calls `on_cancel()` if the window is closed without auth.
    """

    W, H = 460, 520

    def __init__(self,
                 master: tk.Tk,
                 on_success: Callable[[str, str, "license_service.LicenseInfo"], None],
                 on_cancel: Callable[[], None]):
        super().__init__(master)
        self._on_success = on_success
        self._on_cancel  = on_cancel
        self._loading    = False

        self.title(PRODUCT_NAME)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self._center(master)
        self._build()

    # ── layout ────────────────────────────────────────────────────────────────

    def _center(self, master: tk.Tk) -> None:
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x  = (sw - self.W) // 2
        y  = (sh - self.H) // 2
        self.geometry(f"{self.W}x{self.H}+{x}+{y}")

    def _build(self) -> None:
        # ── Top accent stripe ─────────────────────────────────────────────
        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")

        outer = frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=0, pady=0)

        # ── Logo area ────────────────────────────────────────────────────
        logo_frame = frame(outer, bg=BG)
        logo_frame.pack(pady=(42, 0))

        # Diamond logo mark
        canvas = tk.Canvas(logo_frame, width=52, height=52, bg=BG,
                            bd=0, highlightthickness=0)
        canvas.pack()
        # Outer diamond (accent)
        pts = [26, 4, 48, 26, 26, 48, 4, 26]
        canvas.create_polygon(pts, fill=ACCENT, outline="")
        # Inner diamond (dark)
        inner_pts = [26, 13, 39, 26, 26, 39, 13, 26]
        canvas.create_polygon(inner_pts, fill=BG, outline="")
        # Center dot
        canvas.create_oval(22, 22, 30, 30, fill=ACCENT, outline="")

        tk.Label(outer, text=PRODUCT_NAME, bg=BG, fg=WHITE,
                 font=tkfont.Font(family="Segoe UI", size=20, weight="bold")
                 ).pack(pady=(10, 3))
        tk.Label(outer, text="Welcome", bg=BG, fg=TEXT_MUTED,
                 font=tkfont.Font(family="Segoe UI", size=11)
                 ).pack()
        tk.Label(outer, text="Enter your license key to continue.", bg=BG, fg=TEXT_DIM,
                 font=F_SMALL
                 ).pack(pady=(4, 0))

        # ── Divider ──────────────────────────────────────────────────────
        sep(outer, BORDER).pack(fill="x", padx=40, pady=20)

        # ── Key entry ────────────────────────────────────────────────────
        form = frame(outer, bg=BG)
        form.pack(fill="x", padx=40)

        tk.Label(form, text="License Key", bg=BG, fg=TEXT_MUTED,
                 font=F_H2, anchor="w").pack(fill="x", pady=(0, 6))

        # Entry wrapper with focus ring
        self._entry_frame = tk.Frame(form, bg=SURFACE3,
                                     highlightthickness=1,
                                     highlightbackground=BORDER2)
        self._entry_frame.pack(fill="x")

        self._key_var = tk.StringVar()
        self._entry = tk.Entry(
            self._entry_frame,
            textvariable=self._key_var,
            bg=SURFACE3, fg=TEXT,
            insertbackground=ACCENT,
            relief="flat", bd=0,
            font=tkfont.Font(family="Consolas", size=11),
            selectbackground=ACCENT_DIM, selectforeground=TEXT,
        )
        self._entry.pack(fill="x", ipady=11, padx=14)
        self._entry.bind("<FocusIn>",  lambda _e: self._entry_frame.configure(
            highlightbackground=ACCENT))
        self._entry.bind("<FocusOut>", lambda _e: self._entry_frame.configure(
            highlightbackground=BORDER2))
        self._entry.bind("<Return>", lambda _e: self._activate())
        self._entry.focus_set()

        # ── Error label ──────────────────────────────────────────────────
        self._err_var = tk.StringVar()
        tk.Label(form, textvariable=self._err_var, bg=BG, fg=DANGER,
                 font=F_SMALL, anchor="w", wraplength=360,
                 justify="left").pack(fill="x", pady=(6, 0))

        # ── Activate button ──────────────────────────────────────────────
        btn_frame = frame(outer, bg=BG)
        btn_frame.pack(fill="x", padx=40, pady=(16, 0))

        self._btn = tk.Button(
            btn_frame, text="ACTIVATE",
            bg=ACCENT, fg=WHITE,
            activebackground=ACCENT_HOV, activeforeground=WHITE,
            relief="flat", bd=0,
            font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
            cursor="hand2", pady=12,
            command=self._activate,
        )
        self._btn.pack(fill="x")
        self._btn.bind("<Enter>", lambda _e: self._btn.configure(bg=ACCENT_HOV))
        self._btn.bind("<Leave>", lambda _e: self._btn.configure(bg=ACCENT))

        # ── Footer links ─────────────────────────────────────────────────
        footer = frame(outer, bg=BG)
        footer.pack(pady=(24, 0))

        def _link(parent: tk.Widget, text: str, color: str = TEXT_DIM,
                  command: Callable | None = None) -> tk.Label:
            lbl = tk.Label(parent, text=text, bg=BG, fg=color,
                           font=F_SMALL, cursor="hand2")
            if command:
                lbl.bind("<Button-1>", lambda _e: command())
                lbl.bind("<Enter>",    lambda _e: lbl.configure(fg=TEXT_MUTED))
                lbl.bind("<Leave>",    lambda _e: lbl.configure(fg=color))
            return lbl

        _link(footer, "Need a license?",  TEXT_DIM).pack(side="left")
        _link(footer, " Purchase",  ACCENT, self._open_purchase).pack(side="left")
        tk.Label(footer, text="   ", bg=BG).pack(side="left")
        _link(footer, "Having trouble?",  TEXT_DIM).pack(side="left")
        _link(footer, " Support", TEXT_MUTED, self._open_support).pack(side="left")

        # ── Dev-mode notice (production URL is baked in; always set) ─────
        from app_config import _PRODUCTION_API_URL
        if API_BASE_URL and API_BASE_URL != _PRODUCTION_API_URL:
            tk.Label(outer, text=f"Dev mode: {API_BASE_URL}",
                     bg=BG, fg=TEXT_DIM, font=F_SMALL).pack(pady=(12, 0))

        # ── Version ───────────────────────────────────────────────────────
        tk.Label(outer, text=f"v{APP_VERSION}", bg=BG, fg=TEXT_DIM,
                 font=F_SMALL).pack(pady=(16, 0))

    # ── Activation logic ──────────────────────────────────────────────────────

    def _activate(self) -> None:
        if self._loading:
            return
        key = self._key_var.get().strip().upper()
        if not key:
            self._err("Please enter your license key.")
            return
        if len(key) < 12:
            self._err("That doesn't look like a valid license key.")
            return

        self._loading = True
        self._err("")
        self._btn.configure(state="disabled", text="Activating…")
        threading.Thread(target=self._do_activate, args=(key,), daemon=True).start()

    def _do_activate(self, key: str) -> None:
        """
        Key-only activation flow:
          POST /api/license/activate { license_key }
          → on ok: save key to session, build LicenseInfo, open dashboard
          → on error: display customer-friendly message
        """
        result  = auth_manager.activate_license(key)
        ok      = result.get("ok", False)
        err_msg = result.get("error", "Activation failed. Please try again.")

        if ok:
            lic = license_service.LicenseInfo(result)
            auth_manager.save_key_session(key)
            # Pass the key as the "token" so the dashboard poller can re-validate
            self.after(0, lambda: self._on_auth_success(key, "", lic))
        else:
            # Map HTTP status codes to friendly messages
            status_code = result.get("status", 0)
            if status_code == 0:
                err_msg = ("Unable to reach the activation server. "
                           "Check your internet connection and try again.")
            elif status_code == 404:
                err_msg = result.get("error", "License key not found.")
            elif status_code == 402:
                err_msg = result.get("error", "This license has expired.")
            elif status_code == 403:
                err_msg = result.get("error", "This license has been revoked.")
            elif status_code == 429:
                err_msg = "Too many attempts. Please wait a moment and try again."
            elif status_code >= 500:
                err_msg = "Server error. Please try again in a few moments."
            self.after(0, lambda m=err_msg: self._err(m))
            self.after(0, self._reset_btn)

    def _on_auth_success(self, token: str, username: str,
                          lic: "license_service.LicenseInfo") -> None:
        self.destroy()
        self._on_success(token, username, lic)

    def _reset_btn(self) -> None:
        self._loading = False
        self._btn.configure(state="normal", text="ACTIVATE")

    def _err(self, msg: str) -> None:
        self._err_var.set(msg)

    def _cancel(self) -> None:
        self._on_cancel()

    def _open_purchase(self) -> None:
        import webbrowser
        webbrowser.open(PURCHASE_URL)

    def _open_support(self) -> None:
        import webbrowser
        webbrowser.open(SUPPORT_URL)


class _CredentialOverlay(tk.Toplevel):
    """Mini dialog for username + password when needed."""

    def __init__(self, parent: tk.Toplevel, key: str,
                 on_submit: Callable[[str, str, str], None]):
        super().__init__(parent)
        self._key       = key
        self._on_submit = on_submit
        self.title("Sign In")
        self.configure(bg=SURFACE)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        body = frame(self, bg=SURFACE)
        body.pack(fill="both", padx=24, pady=20)

        tk.Label(body, text="Account Login", bg=SURFACE, fg=TEXT,
                 font=tkfont.Font(family="Segoe UI", size=12, weight="bold")
                 ).pack(anchor="w")
        tk.Label(body, text="Your license is linked to an account.\nEnter your credentials to continue.",
                 bg=SURFACE, fg=TEXT_MUTED, font=F_SMALL, justify="left"
                 ).pack(anchor="w", pady=(4, 14))

        def _field(placeholder: str, show: str = "") -> tk.Entry:
            f = tk.Frame(body, bg=SURFACE3, highlightthickness=1,
                         highlightbackground=BORDER2)
            f.pack(fill="x", pady=(0, 8))
            e = tk.Entry(f, bg=SURFACE3, fg=TEXT, insertbackground=ACCENT,
                         relief="flat", bd=0, font=F_BODY, show=show,
                         selectbackground=ACCENT_DIM, selectforeground=TEXT)
            e.insert(0, placeholder)
            e._ph = placeholder  # type: ignore
            def _fi(_ev): 
                if e.get() == e._ph:   # type: ignore
                    e.delete(0, "end"); e.configure(fg=TEXT)
                f.configure(highlightbackground=ACCENT)
            def _fo(_ev):
                if not e.get():
                    e.insert(0, e._ph); e.configure(fg=TEXT_MUTED)  # type: ignore
                f.configure(highlightbackground=BORDER2)
            e.configure(fg=TEXT_MUTED)
            e.bind("<FocusIn>",  _fi)
            e.bind("<FocusOut>", _fo)
            e.pack(fill="x", ipady=9, padx=10)
            return e

        self._u = _field("Username")
        self._p = _field("Password", show="\u2022")

        sep(body, BORDER).pack(fill="x", pady=(8, 12))

        btn_row = frame(body, bg=SURFACE)
        btn_row.pack(fill="x")
        sb = tk.Button(btn_row, text="Sign In",
                       bg=ACCENT, fg=WHITE,
                       activebackground=ACCENT_HOV, activeforeground=WHITE,
                       relief="flat", bd=0, font=F_BOLD, cursor="hand2",
                       padx=16, pady=8, command=self._submit)
        sb.pack(side="right")
        tk.Button(btn_row, text="Cancel",
                  bg=SURFACE3, fg=TEXT_MUTED,
                  activebackground=BORDER, activeforeground=TEXT,
                  relief="flat", bd=0, font=F_BODY, cursor="hand2",
                  padx=16, pady=8, command=self.destroy
                  ).pack(side="right", padx=(0, 8))

        self.update_idletasks()
        w, h = 360, self.winfo_reqheight() + 10
        px = parent.winfo_x() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")

    def _val(self, e: tk.Entry) -> str:
        v = e.get()
        return "" if v == getattr(e, "_ph", None) else v

    def _submit(self) -> None:
        u = self._val(self._u)
        p = self._val(self._p)
        if not u or not p:
            return
        self.destroy()
        self._on_submit(u, p, self._key)
