"""デスクトップ漫画ビュー。

描画はComicReaderEngineの状態(ComicViewState)だけに従い、ページ計算を
UI側では行わない。画像のデコード・リサイズ・見開き合成はワーカー
スレッドで実行し、Tkinter更新のみ after() でメインスレッドへ戻す。
"""
from __future__ import annotations

import io
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageOps, ImageTk

from ..models import ComicViewState
from .widgets import C, FONT_TITLE, FONT_UI, FONT_UI_SMALL, HoverButton


class ComicView(tk.Frame):
    def __init__(self, master, services, on_entry_nav=None,
                 on_fullscreen=None, on_state_change=None, **kw):
        super().__init__(master, bg="black", **kw)
        self.services = services
        self.engine = services.comic_engine
        self.on_entry_nav = on_entry_nav          # (delta) -> None
        self.on_fullscreen = on_fullscreen        # () -> None
        self.on_state_change = on_state_change    # (state) -> None
        self.state: ComicViewState | None = None
        self._photo = None
        self._render_version = 0
        self._seek_visible = False
        self._hide_seek_job = None
        self._scale_dragging = False
        self._destroyed = False

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0,
                                cursor="hand2")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._placeholder()

        self.seek_panel = tk.Frame(self, bg=C["surface"], padx=12, pady=8)
        controls = tk.Frame(self.seek_panel, bg=C["surface"])
        controls.pack(side=tk.LEFT, padx=(0, 10))
        HoverButton(controls, text="‹", font=FONT_TITLE, padx=8, pady=3,
                    command=self.prev_single_page).pack(side=tk.LEFT, padx=(0, 4))
        HoverButton(controls, text="›", font=FONT_TITLE, padx=8, pady=3,
                    command=self.next_single_page).pack(side=tk.LEFT)
        self.page_label = tk.Label(self.seek_panel, text="", bg=C["surface"],
                                   fg=C["text"], font=FONT_UI_SMALL, width=14)
        self.page_label.pack(side=tk.RIGHT, padx=(10, 0))
        self.seek_var = tk.IntVar(value=1)
        self.seek = ttk.Scale(
            self.seek_panel, from_=1, to=1, orient="horizontal",
            variable=self.seek_var, command=self._on_seek
        )
        self.seek.pack(side=tk.LEFT, fill=tk.X, expand=True)

        for widget in (self, self.canvas):
            widget.bind("<Configure>", lambda e: self._render_async())
            widget.bind("<Double-1>", self._on_double_click)
            widget.bind("<MouseWheel>", self._on_mousewheel)
            widget.bind("<Button-4>", lambda e: self.prev_page())
            widget.bind("<Button-5>", lambda e: self.next_page())
            for seq, delta in (("<Button-8>", -1), ("<Button-9>", 1)):
                try:
                    widget.bind(seq, lambda e, d=delta: self._entry_nav(d))
                except tk.TclError:
                    pass
            widget.bind("<Motion>", self._on_motion)
            widget.bind("<Leave>", self._schedule_hide_seek)

        self.seek_panel.bind("<Enter>", self._show_seek)
        self.seek_panel.bind("<Leave>", self._schedule_hide_seek)
        self.seek.bind("<ButtonPress-1>",
                       lambda e: self._set_scale_dragging(True))
        self.seek.bind("<ButtonRelease-1>",
                       lambda e: self._set_scale_dragging(False))

    # ---------------- セッション/状態 ----------------

    @property
    def session_id(self) -> str | None:
        return self.state.session_id if self.state else None

    def set_state(self, state: ComicViewState | None) -> None:
        self.state = state
        self._sync_seek()
        self._render_async()
        if state is not None and self.on_state_change:
            self.on_state_change(state)

    def clear(self) -> None:
        self.state = None
        self._photo = None
        self._sync_seek()
        self._placeholder()

    def _engine_do(self, func, *args) -> None:
        if not self.state:
            return
        try:
            state = func(self.state.session_id, *args)
        except Exception as e:
            self._show_error(str(e))
            return
        self.set_state(state)

    # ---------------- ページ操作 ----------------

    def next_page(self):
        self._engine_do(self.engine.next_spread)

    def prev_page(self):
        self._engine_do(self.engine.previous_spread)

    def next_single_page(self):
        self._engine_do(self.engine.next_page)

    def prev_single_page(self):
        self._engine_do(self.engine.previous_page)

    def goto_page(self, index: int):
        self._engine_do(self.engine.goto_page, index)

    def _entry_nav(self, delta):
        if self.on_entry_nav:
            self.on_entry_nav(delta)
        return "break"

    def _on_double_click(self, event=None):
        if self.on_fullscreen and self.state:
            self.on_fullscreen()

    def _on_mousewheel(self, event):
        if event.delta < 0:
            self.next_page()
        else:
            self.prev_page()

    # ---------------- シークバー ----------------

    def _sync_seek(self):
        state = self.state
        if state and state.page_count:
            total = state.page_count
            self.seek.configure(to=total)
            self.seek_var.set(total - state.page_index)
            first = state.visible_pages[0] + 1
            last = state.visible_pages[-1] + 1
            pages = f"{first}" if first == last else f"{first}-{last}"
            self.page_label.config(
                text=f"{pages} / {total} "
                     f"[{state.entry_index + 1}/{state.entry_count}]"
            )
        else:
            self.seek_var.set(1)
            self.page_label.config(text="")

    def _on_seek(self, value):
        state = self.state
        if not state or self._scale_dragging:
            return
        index = state.page_count - int(float(value))
        index = max(0, min(index, state.page_count - 1))
        if index != state.page_index:
            self.goto_page(index)

    def _set_scale_dragging(self, dragging: bool):
        self._scale_dragging = dragging
        if not dragging:
            self._on_seek(self.seek_var.get())

    def _on_motion(self, event):
        if event.y >= max(0, self.winfo_height() - 58):
            self._show_seek()
        else:
            self._schedule_hide_seek()

    def _show_seek(self, event=None):
        if self._hide_seek_job is not None:
            self.after_cancel(self._hide_seek_job)
            self._hide_seek_job = None
        if not self._seek_visible:
            self.seek_panel.place(relx=0, rely=1, relwidth=1, anchor="sw")
            self._seek_visible = True

    def _schedule_hide_seek(self, event=None):
        if self._hide_seek_job is not None:
            self.after_cancel(self._hide_seek_job)
        self._hide_seek_job = self.after(700, self._hide_seek)

    def _hide_seek(self):
        if not self._scale_dragging and self._seek_visible:
            self.seek_panel.place_forget()
            self._seek_visible = False
        self._hide_seek_job = None

    # ---------------- 描画(非同期) ----------------

    def _placeholder(self):
        self.canvas.delete("all")
        self.canvas.create_text(
            max(1, self.canvas.winfo_width()) / 2,
            max(1, self.canvas.winfo_height()) / 2,
            text="📖\n\n漫画を選択してください",
            fill=C["text_dim"], font=FONT_UI, justify="center",
        )

    def _show_error(self, message: str):
        self.canvas.delete("all")
        self.canvas.create_text(
            max(1, self.canvas.winfo_width()) / 2,
            max(1, self.canvas.winfo_height()) / 2,
            text=f"画像を読み込めませんでした\n{message}",
            fill=C["danger"], font=FONT_UI, justify="center",
        )

    def _render_async(self):
        state = self.state
        if state is None:
            self._placeholder()
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        if width < 4 or height < 4:
            return
        self._render_version += 1
        version = self._render_version
        session_id = state.session_id
        pages = list(state.visible_pages)
        direction = state.reading_direction
        self.services.pipeline.executor.submit(
            self._compose_worker, version, session_id, pages,
            direction, width, height,
        )

    def _compose_worker(self, version, session_id, pages, direction,
                        width, height):
        try:
            images = []
            for page_index in pages:
                data, _, _ = self.engine.render_page(session_id, page_index)
                img = Image.open(io.BytesIO(data))
                img = ImageOps.exif_transpose(img) or img
                images.append(img.convert("RGB"))
            if direction == "rtl" and len(images) == 2:
                images.reverse()

            gap = 10 if len(images) == 2 else 0
            slot_w = (width - gap) / max(1, len(images))
            fitted = []
            for img in images:
                ratio = min(slot_w / img.width, height / img.height)
                size = (max(1, int(img.width * ratio)),
                        max(1, int(img.height * ratio)))
                fitted.append(img.resize(size, Image.Resampling.LANCZOS))
            total_w = sum(img.width for img in fitted) + gap
            spread = Image.new("RGB", (width, height), "black")
            x = int((width - total_w) / 2)
            for img in fitted:
                y = int((height - img.height) / 2)
                spread.paste(img, (x, y))
                x += img.width + gap
        except Exception as e:
            message = str(e)
            self._safe_after(lambda: (version == self._render_version
                                      and self._show_error(message)))
            return
        self._safe_after(lambda: self._apply_render(version, spread))

    def _safe_after(self, callback):
        if self._destroyed:
            return
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _apply_render(self, version, spread):
        if self._destroyed or version != self._render_version:
            return
        try:
            self._photo = ImageTk.PhotoImage(spread)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        except tk.TclError:
            pass

    def destroy(self):
        self._destroyed = True
        super().destroy()


class ComicFullscreenWindow(tk.Toplevel):
    """全画面リーダー。同じエンジンセッションを共有する。"""

    def __init__(self, owner: ComicView, services):
        super().__init__(owner)
        self.owner = owner
        self.title("FrameDeck")
        self.configure(bg="black")
        self.geometry("1200x800")
        self.attributes("-fullscreen", True)
        self.view = ComicView(
            self, services,
            on_entry_nav=self._on_entry_nav,
            on_fullscreen=self.destroy,
        )
        self.view.pack(fill=tk.BOTH, expand=True)
        self.view.set_state(owner.state)
        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _on_entry_nav(self, delta):
        if self.owner.on_entry_nav:
            self.owner.on_entry_nav(delta)
            self.view.set_state(self.owner.state)

    def destroy(self):
        # 全画面中の移動結果をメインビューへ反映
        if self.view.state is not None:
            self.owner.set_state(self.view.state)
        super().destroy()
