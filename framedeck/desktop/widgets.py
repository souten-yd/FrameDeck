"""デスクトップUI共通ウィジェット(Tokyo Night風ダークテーマ)。"""
from __future__ import annotations

import tkinter as tk

C = {
    "bg":          "#16161e",
    "panel":       "#1a1b26",
    "surface":     "#24283b",
    "surface_hi":  "#2f3549",
    "border":      "#3b4261",
    "text":        "#c0caf5",
    "text_dim":    "#565f89",
    "accent":      "#7aa2f7",
    "accent_dim":  "#3d59a1",
    "star":        "#e0af68",
    "star_off":    "#414868",
    "danger":      "#f7768e",
    "ok":          "#9ece6a",
}

FONT_UI = ("Noto Sans CJK JP", 10)
FONT_UI_SMALL = ("Noto Sans CJK JP", 9)
FONT_TITLE = ("Noto Sans CJK JP", 11, "bold")
FONT_STAR = ("Noto Sans CJK JP", 13)


class HoverButton(tk.Label):
    """フラットでホバー反応するボタン(tk.Labelベース)。"""

    def __init__(self, master, text, command=None, fg=None, hover_fg=None,
                 bg=None, hover_bg=None, font=FONT_UI, padx=12, pady=5, **kw):
        self._fg = fg or C["text"]
        self._hover_fg = hover_fg or C["accent"]
        self._bg = bg or C["surface"]
        self._hover_bg = hover_bg or C["surface_hi"]
        super().__init__(
            master, text=text, fg=self._fg, bg=self._bg, font=font,
            padx=padx, pady=pady, cursor="hand2", **kw
        )
        self._command = command
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, e=None):
        self.config(bg=self._hover_bg, fg=self._hover_fg)

    def _on_leave(self, e=None):
        self.config(bg=self._bg, fg=self._fg)

    def _on_click(self, e=None):
        if self._command:
            self._command()

    def set_colors(self, fg=None, bg=None, hover_fg=None, hover_bg=None):
        if fg is not None:
            self._fg = fg
        if bg is not None:
            self._bg = bg
        if hover_fg is not None:
            self._hover_fg = hover_fg
        if hover_bg is not None:
            self._hover_bg = hover_bg
        self.config(fg=self._fg, bg=self._bg)


class StarRatingBar(tk.Frame):
    """★をクリックして評価するバー。ホバーでプレビュー点灯。"""

    def __init__(self, master, on_rate, on_clear, **kw):
        super().__init__(master, bg=C["panel"], **kw)
        self.on_rate = on_rate
        self.stars = []
        self.current = 0
        for i in range(1, 6):
            lbl = tk.Label(
                self, text="★", font=FONT_STAR, fg=C["star_off"],
                bg=C["panel"], cursor="hand2", padx=2
            )
            lbl.pack(side=tk.LEFT)
            lbl.bind("<Enter>", lambda e, n=i: self._preview(n))
            lbl.bind("<Leave>", lambda e: self._preview(self.current))
            lbl.bind("<Button-1>", lambda e, n=i: self.on_rate(n))
            self.stars.append(lbl)
        clear = tk.Label(
            self, text="✕", font=FONT_UI_SMALL, fg=C["text_dim"],
            bg=C["panel"], cursor="hand2", padx=8
        )
        clear.pack(side=tk.LEFT)
        clear.bind("<Enter>", lambda e: clear.config(fg=C["danger"]))
        clear.bind("<Leave>", lambda e: clear.config(fg=C["text_dim"]))
        clear.bind("<Button-1>", lambda e: on_clear())

    def set_current(self, rating):
        self.current = rating or 0
        self._preview(self.current)

    def _preview(self, n):
        for idx, lbl in enumerate(self.stars, start=1):
            lbl.config(fg=C["star"] if idx <= n else C["star_off"])
