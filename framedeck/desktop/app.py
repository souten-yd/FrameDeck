"""デスクトップUI(Tkinter)。

ビジネスロジックは持たず、共通サービス(LibraryService /
ComicReaderEngine / MPVController / VideoPlaybackService)を呼び出す。
Web UIと読書履歴・再生位置・設定を共有する。
"""
from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..core.services import Services
from ..models import MediaItem
from ..video.mpv_controller import MPVController
from .comic_view import ComicFullscreenWindow, ComicView
from .widgets import (
    C, FONT_TITLE, FONT_UI, FONT_UI_SMALL, HoverButton, StarRatingBar,
)

APP_NAME = "FrameDeck"
MODE_VIDEO = "video"
MODE_COMIC = "comic"


class DesktopApp(tk.Tk):
    def __init__(self, services: Services):
        super().__init__()
        self.services = services
        self.title(APP_NAME)
        self.geometry("1200x700")
        self.minsize(900, 500)
        self.configure(bg=C["bg"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        default_comic = services.settings.get("default_folder_comic", "")
        self.folder = tk.StringVar(value=default_comic)
        self.media_mode = tk.StringVar(value=MODE_COMIC)
        self.filter_mode = tk.StringVar(value="すべて")
        self.sort_mode = tk.StringVar(value="日付順")
        self.volume_value = tk.IntVar(
            value=int(services.settings.get("default_volume", 0)))
        self.last_volume = 50
        self.folder_history = [default_comic]
        self.folder_history_index = 0

        self.items: list[MediaItem] = []
        self._visible_items: list[MediaItem] = []
        self.current_item: MediaItem | None = None
        self.playing_item: MediaItem | None = None
        self.reading_item: MediaItem | None = None
        self.volume_popup = None
        self._hide_job = None
        self._video_position = 0.0
        self._video_duration = 0.0
        self._last_progress_save = 0.0
        self._fullscreen_window = None

        self.mpv = MPVController(
            services.paths.mpv_runtime,
            on_property=self._on_mpv_property,
            on_nav=self._on_mpv_nav,
        )

        self._setup_style()
        self._build_top_bar()
        self._build_main_area()
        self._build_status_bar()
        self._update_mode_ui()
        self._update_folder_nav_buttons()

        for seq, delta in (("<Button-8>", -1), ("<Button-9>", 1)):
            try:
                self.bind_all(seq,
                              lambda e, d=delta: self._on_global_mouse_nav(e, d))
            except tk.TclError:
                pass

        if os.path.isdir(self.folder.get()):
            self.refresh_list()

    # ---------------- スタイル ----------------

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=C["panel"], foreground=C["text"],
                        fieldbackground=C["surface"], bordercolor=C["border"],
                        font=FONT_UI)
        style.configure("TCombobox",
                        fieldbackground=C["surface"], background=C["surface"],
                        foreground=C["text"], arrowcolor=C["accent"],
                        bordercolor=C["border"], lightcolor=C["surface"],
                        darkcolor=C["surface"], padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C["surface"])],
                  foreground=[("readonly", C["text"])],
                  selectbackground=[("readonly", C["surface"])],
                  selectforeground=[("readonly", C["text"])])
        self.option_add("*TCombobox*Listbox.background", C["surface"])
        self.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["accent_dim"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["text"])
        self.option_add("*TCombobox*Listbox.font", FONT_UI_SMALL)
        style.configure("Dark.Treeview",
                        background=C["panel"], fieldbackground=C["panel"],
                        foreground=C["text"], rowheight=32,
                        bordercolor=C["panel"], lightcolor=C["panel"],
                        darkcolor=C["panel"], font=FONT_UI)
        style.configure("Dark.Treeview.Heading",
                        background=C["surface"], foreground=C["text_dim"],
                        font=FONT_UI_SMALL, relief="flat", padding=(8, 6))
        style.map("Dark.Treeview.Heading",
                  background=[("active", C["surface_hi"])])
        style.map("Dark.Treeview",
                  background=[("selected", C["accent_dim"])],
                  foreground=[("selected", "#ffffff")])
        style.layout("Dark.Treeview",
                     [("Dark.Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Dark.Vertical.TScrollbar",
                        background=C["surface"], troughcolor=C["panel"],
                        bordercolor=C["panel"], arrowcolor=C["text_dim"],
                        lightcolor=C["surface"], darkcolor=C["surface"])
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", C["surface_hi"])])
        style.configure("Dark.Vertical.TScale",
                        background=C["surface"], troughcolor=C["bg"],
                        bordercolor=C["surface"], lightcolor=C["accent"],
                        darkcolor=C["accent"])

    # ---------------- UI構築 ----------------

    def _build_top_bar(self):
        bar = tk.Frame(self, bg=C["panel"], padx=12, pady=10)
        bar.pack(side=tk.TOP, fill=tk.X)
        controls = tk.Frame(bar, bg=C["panel"])
        controls.pack(side=tk.TOP, fill=tk.X)

        self.comic_mode_button = HoverButton(
            controls, text="📖", font=("", 14), padx=7,
            command=lambda: self._set_media_mode(MODE_COMIC))
        self.comic_mode_button.pack(side=tk.LEFT, padx=(0, 4))
        self.video_mode_button = HoverButton(
            controls, text="🎬", font=("", 14), padx=7,
            command=lambda: self._set_media_mode(MODE_VIDEO))
        self.video_mode_button.pack(side=tk.LEFT, padx=(0, 8))

        self.folder_back_button = HoverButton(
            controls, text="‹", font=FONT_TITLE, padx=8,
            command=lambda: self._move_folder_history(-1))
        self.folder_back_button.pack(side=tk.LEFT, padx=(0, 4))
        self.folder_forward_button = HoverButton(
            controls, text="›", font=FONT_TITLE, padx=8,
            command=lambda: self._move_folder_history(1))
        self.folder_forward_button.pack(side=tk.LEFT, padx=(0, 8))

        HoverButton(controls, text="📂",
                    command=self.choose_folder).pack(side=tk.LEFT)

        tk.Label(controls, text="評価", bg=C["panel"], fg=C["text_dim"],
                 font=FONT_UI_SMALL).pack(side=tk.LEFT, padx=(10, 6))
        self.star_bar = StarRatingBar(
            controls,
            on_rate=lambda r: self._apply_rating(r),
            on_clear=lambda: self._apply_rating(None))
        self.star_bar.pack(side=tk.LEFT, padx=(0, 12))

        HoverButton(controls, text="⟳",
                    command=self.refresh_list).pack(side=tk.LEFT, padx=(6, 12))

        tk.Label(controls, text="表示", bg=C["panel"], fg=C["text_dim"],
                 font=FONT_UI_SMALL).pack(side=tk.LEFT)
        filter_box = ttk.Combobox(
            controls, textvariable=self.filter_mode, state="readonly",
            width=8, values=["すべて", "評価あり", "評価なし"],
            font=FONT_UI_SMALL)
        filter_box.pack(side=tk.LEFT, padx=(4, 14))
        filter_box.bind("<<ComboboxSelected>>",
                        lambda e: self.refresh_list(rescan=False))

        tk.Label(controls, text="並び替え", bg=C["panel"], fg=C["text_dim"],
                 font=FONT_UI_SMALL).pack(side=tk.LEFT)
        sort_box = ttk.Combobox(
            controls, textvariable=self.sort_mode, state="readonly",
            width=12,
            values=["日付順", "名前順", "評価が高い順", "評価が低い順"],
            font=FONT_UI_SMALL)
        sort_box.pack(side=tk.LEFT, padx=(4, 0))
        sort_box.bind("<<ComboboxSelected>>",
                      lambda e: self.refresh_list(rescan=False))

        HoverButton(controls, text="🗑", padx=6, hover_fg=C["danger"],
                    command=self._on_trash).pack(side=tk.LEFT, padx=(12, 0))

        self.volume_button = HoverButton(controls, text="🔊", padx=3,
                                         command=self._toggle_mute)
        self.volume_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.volume_button.bind("<Enter>", self._show_volume_popup, add="+")
        self.volume_button.bind("<Leave>", self._schedule_hide_volume_popup,
                                add="+")
        self.volume_label = tk.Label(controls, text="0%", width=4,
                                     bg=C["panel"], fg=C["text_dim"],
                                     font=FONT_UI_SMALL)
        self.volume_label.pack(side=tk.RIGHT)

    def _build_main_area(self):
        main = tk.Frame(self, bg=C["bg"])
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        left = tk.Frame(main, width=380, bg=C["panel"])
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=(0, 4))
        left.pack_propagate(False)

        self.tree = ttk.Treeview(
            left, columns=("rating",), show="tree headings",
            selectmode="extended", style="Dark.Treeview")
        self.tree.heading("#0", text="ファイル名")
        self.tree.heading("rating", text="評価")
        self.tree.column("#0", width=250)
        self.tree.column("rating", width=100, anchor="center")
        self.tree.tag_configure("odd", background=C["panel"])
        self.tree.tag_configure("even", background="#1e2030")
        self.tree.tag_configure("rated", foreground=C["star"])

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview,
                            style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", lambda e: self._play_selected())
        self.tree.bind("<ButtonRelease-1>", self._on_single_click_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = tk.Frame(main, bg=C["bg"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                   padx=(4, 8), pady=(0, 4))

        self.video_frame = tk.Frame(right, bg="black",
                                    highlightbackground=C["border"],
                                    highlightthickness=1)
        self.video_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.video_placeholder = tk.Label(
            self.video_frame, text="▶\n\n動画を選択して再生してください",
            bg="black", fg=C["text_dim"], font=FONT_UI, justify="center")
        self.video_placeholder.place(relx=0.5, rely=0.5, anchor="center")

        self.comic_view = ComicView(
            right, self.services,
            on_entry_nav=self._navigate_comic_entry,
            on_fullscreen=self._open_comic_fullscreen,
        )

    def _build_status_bar(self):
        status = tk.Frame(self, bg=C["panel"], padx=12, pady=5)
        status.pack(side=tk.BOTTOM, fill=tk.X)
        self.selected_label = tk.Label(
            status, text="ファイルを選択してください", bg=C["panel"],
            fg=C["text_dim"], font=FONT_UI_SMALL, anchor="w")
        self.selected_label.pack(side=tk.LEFT)
        self.count_label = tk.Label(status, text="", bg=C["panel"],
                                    fg=C["text_dim"], font=FONT_UI_SMALL)
        self.count_label.pack(side=tk.RIGHT)

    # ---------------- モード / フォルダ履歴 ----------------

    def _set_media_mode(self, mode):
        if self.media_mode.get() == mode:
            return
        self.media_mode.set(mode)
        self._stop_video()
        self._close_comic()
        self.current_item = None
        settings = self.services.settings
        default_video = settings.get("default_folder_video", "")
        default_comic = settings.get("default_folder_comic", "")
        default_folder = default_video if mode == MODE_VIDEO else default_comic
        if (not self.folder.get()
                or self.folder.get() in (default_video, default_comic)):
            if default_folder:
                self.folder.set(default_folder)
                self._push_folder_history(default_folder)
        self._update_mode_ui()
        self.refresh_list()

    def _update_mode_ui(self):
        is_video = self.media_mode.get() == MODE_VIDEO
        self.video_mode_button.set_colors(
            bg=C["accent_dim"] if is_video else C["surface"],
            fg="#ffffff" if is_video else C["text"])
        self.comic_mode_button.set_colors(
            bg=C["accent_dim"] if not is_video else C["surface"],
            fg="#ffffff" if not is_video else C["text"])
        if is_video:
            self.comic_view.pack_forget()
            self.video_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.volume_label.pack(side=tk.RIGHT)
            self.volume_button.pack(side=tk.RIGHT, padx=(8, 0))
        else:
            self._stop_video()
            self.video_frame.pack_forget()
            self.comic_view.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self._hide_volume_popup()
            self.volume_button.pack_forget()
            self.volume_label.pack_forget()

    def _push_folder_history(self, path):
        if (self.folder_history
                and self.folder_history[self.folder_history_index] == path):
            return
        self.folder_history = \
            self.folder_history[:self.folder_history_index + 1]
        self.folder_history.append(path)
        self.folder_history_index = len(self.folder_history) - 1
        self._update_folder_nav_buttons()

    def _move_folder_history(self, delta):
        new_index = self.folder_history_index + delta
        if new_index < 0 or new_index >= len(self.folder_history):
            return
        self.folder_history_index = new_index
        self.folder.set(self.folder_history[self.folder_history_index])
        self._update_folder_nav_buttons()
        self.refresh_list()

    def _update_folder_nav_buttons(self):
        back_ok = self.folder_history_index > 0
        forward_ok = \
            self.folder_history_index < len(self.folder_history) - 1
        self.folder_back_button.set_colors(
            fg=C["text"] if back_ok else C["text_dim"])
        self.folder_forward_button.set_colors(
            fg=C["text"] if forward_ok else C["text_dim"])

    # ---------------- 音量 ----------------

    def _show_volume_popup(self, event=None):
        if self._hide_job is not None:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        if self.volume_popup is not None:
            return
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=C["border"])
        x = self.volume_button.winfo_rootx()
        y = self.volume_button.winfo_rooty() - 160
        popup.geometry(f"52x152+{x}+{max(y, 0)}")
        frame = tk.Frame(popup, bg=C["surface"], padx=6, pady=8)
        frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        scale = ttk.Scale(
            frame, from_=100, to=0, orient="vertical",
            variable=self.volume_value, command=self._on_volume_change,
            style="Dark.Vertical.TScale")
        scale.pack(fill=tk.BOTH, expand=True)
        for widget in (popup, frame, scale):
            widget.bind("<Enter>", self._cancel_hide_volume_popup)
            widget.bind("<Leave>", self._schedule_hide_volume_popup)
        self.volume_popup = popup

    def _cancel_hide_volume_popup(self, event=None):
        if self._hide_job is not None:
            self.after_cancel(self._hide_job)
            self._hide_job = None

    def _schedule_hide_volume_popup(self, event=None):
        self._hide_job = self.after(250, self._hide_volume_popup)

    def _hide_volume_popup(self):
        if self.volume_popup is not None:
            self.volume_popup.destroy()
            self.volume_popup = None
        self._hide_job = None

    def _set_volume(self, value):
        value = max(0, min(100, int(float(value))))
        self.volume_value.set(value)
        self.volume_label.config(text=f"{value}%")
        self.volume_button.config(text="🔇" if value == 0 else "🔊")
        if value > 0:
            self.last_volume = value
        self.mpv.set_volume(value)

    def _on_volume_change(self, value):
        self._set_volume(value)

    def _toggle_mute(self, event=None):
        if self.volume_value.get() > 0:
            self._set_volume(0)
        else:
            self._set_volume(self.last_volume if self.last_volume > 0 else 50)

    # ---------------- 一覧 ----------------

    def choose_folder(self):
        path = filedialog.askdirectory(title="フォルダを選択",
                                       initialdir=self.folder.get())
        if path:
            self.folder.set(path)
            self._push_folder_history(path)
            self.refresh_list()

    def refresh_list(self, rescan: bool = True):
        folder = self.folder.get()
        if not folder or not os.path.isdir(folder):
            return
        if rescan:
            try:
                self.items = [
                    item for item in self.services.library.list_folder(
                        folder, mode=self.media_mode.get(),
                        enforce_roots=False)
                    if item.media_type != "folder"
                    or self.media_mode.get() == MODE_COMIC
                ]
            except OSError as e:
                messagebox.showerror("エラー",
                                     f"フォルダを読み込めませんでした:\n{e}")
                return

        mode = self.filter_mode.get()
        if mode == "評価あり":
            visible = [it for it in self.items if it.rating]
        elif mode == "評価なし":
            visible = [it for it in self.items if not it.rating]
        else:
            visible = list(self.items)

        sort_mode = self.sort_mode.get()
        if sort_mode == "日付順":
            visible.sort(key=lambda it: (-it.modified_at,
                                         it.display_name.lower()))
        elif sort_mode == "評価が高い順":
            visible.sort(key=lambda it: (-(it.rating or 0),
                                         it.display_name.lower()))
        elif sort_mode == "評価が低い順":
            visible.sort(key=lambda it: ((it.rating or 0),
                                         it.display_name.lower()))
        else:
            visible.sort(key=lambda it: it.display_name.lower())

        self.tree.delete(*self.tree.get_children())
        self._visible_items = visible
        for idx, item in enumerate(visible):
            tags = ["even" if idx % 2 == 0 else "odd"]
            if item.rating:
                tags.append("rated")
            prefix = "📁 " if item.media_type == "folder" else ""
            self.tree.insert("", "end", iid=str(idx),
                             text=prefix + item.display_name,
                             values=(item.stars,), tags=tuple(tags))

        rated = sum(1 for it in self.items if it.rating)
        kind = "動画" if self.media_mode.get() == MODE_VIDEO else "漫画"
        self.count_label.config(
            text=f"{kind}: {len(visible)} 件表示 / 全 {len(self.items)} 件 "
                 f"(評価済 {rated})")

    def _get_selected_items(self) -> list[MediaItem]:
        return [self._visible_items[int(iid)]
                for iid in self.tree.selection()]

    def _on_select(self, event=None):
        items = self._get_selected_items()
        if not items:
            self.selected_label.config(text="ファイルを選択してください")
            self.current_item = None
            self.star_bar.set_current(0)
            return
        if len(items) == 1:
            item = items[0]
            self.current_item = item
            rating = str(item.rating) if item.rating else "-"
            self.selected_label.config(
                text=f"{item.display_name}  (現在の評価: {rating})")
            self.star_bar.set_current(item.rating)
        else:
            self.current_item = None
            self.selected_label.config(text=f"{len(items)} 件選択中")
            self.star_bar.set_current(0)

    def _on_single_click_open(self, event=None):
        if event is None or event.num != 1:
            return
        if event.state & 0x0005:   # Ctrl/Shiftは複数選択用
            return
        row_id = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        if not row_id or region not in ("tree", "cell"):
            return
        self.after_idle(self._play_selected)

    def _play_selected(self):
        items = self._get_selected_items()
        if not items:
            label = "動画" if self.media_mode.get() == MODE_VIDEO else "漫画"
            messagebox.showinfo("情報", f"開く{label}を選択してください")
            return
        item = items[0]
        if item.media_type == "folder" and self.media_mode.get() == MODE_COMIC:
            # フォルダ項目は漫画候補として開く(画像フォルダ/アーカイブ検出)
            self._open_comic(item)
            return
        if self.media_mode.get() == MODE_COMIC:
            self._open_comic(item)
        else:
            self._play_video(item)

    # ---------------- 漫画 ----------------

    def _open_comic(self, item: MediaItem, entry_id: str | None = None,
                    start: str = "saved"):
        self._stop_video()
        try:
            if entry_id is None:
                entries = self.services.comic_engine.entries_for_item(item.path)
                if not entries:
                    messagebox.showerror(
                        "エラー",
                        "開ける画像または圧縮漫画が見つかりませんでした。")
                    return
                if len(entries) > 1:
                    self._show_entry_dialog(item, entries)
                    return
                entry_id = entries[0].id
            root_folder = os.path.dirname(item.path.rstrip(os.sep))
            state = self.services.comic_engine.create_session(
                root_folder, entry_id,
                restore_progress=(start == "saved"),
                item_path=item.path)
            self.reading_item = item
            self.comic_view.set_state(state)
            self.services.library.mark_opened(item)
        except Exception as e:
            messagebox.showerror("エラー", f"漫画を開けませんでした:\n{e}")

    def _show_entry_dialog(self, item: MediaItem, entries):
        dlg = tk.Toplevel(self)
        dlg.title("開く漫画を選択")
        dlg.configure(bg=C["border"])
        dlg.transient(self)
        dlg.geometry("620x420")
        frame = tk.Frame(dlg, bg=C["panel"], padx=14, pady=12)
        frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        tk.Label(frame, text="開く漫画ファイルを選択してください",
                 bg=C["panel"], fg=C["text"],
                 font=FONT_TITLE).pack(anchor="w", pady=(0, 8))
        list_frame = tk.Frame(frame, bg=C["panel"])
        list_frame.pack(fill=tk.BOTH, expand=True)
        listbox = tk.Listbox(
            list_frame, bg=C["surface"], fg=C["text"],
            selectbackground=C["accent_dim"], selectforeground="#ffffff",
            activestyle="none", font=FONT_UI_SMALL, relief="flat",
            highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical",
                                  command=listbox.yview,
                                  style="Dark.Vertical.TScrollbar")
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for entry in entries:
            prefix = "📁 " if entry.source_type == "image_folder" else "📦 "
            listbox.insert(tk.END, prefix + entry.label)
        if entries:
            listbox.selection_set(0)

        btns = tk.Frame(frame, bg=C["panel"])
        btns.pack(fill=tk.X, pady=(10, 0))

        def open_selected(event=None):
            sel = listbox.curselection()
            if not sel:
                return
            entry = entries[sel[0]]
            dlg.destroy()
            self._open_comic(item, entry_id=entry.id)

        HoverButton(btns, text="開く", command=open_selected,
                    fg=C["ok"], hover_fg=C["ok"]).pack(side=tk.RIGHT,
                                                       padx=(8, 0))
        HoverButton(btns, text="キャンセル",
                    command=dlg.destroy).pack(side=tk.RIGHT)
        listbox.bind("<Double-1>", open_selected)
        dlg.grab_set()

    def _navigate_comic_entry(self, delta):
        """マウス戻る/進む: ReadingSequenceに従って前後の漫画へ。"""
        session_id = self.comic_view.session_id
        if session_id is None:
            # 未読書なら選択項目を開く
            self._play_selected()
            return
        engine = self.services.comic_engine
        try:
            state = (engine.next_entry(session_id) if delta > 0
                     else engine.previous_entry(session_id))
        except Exception as e:
            messagebox.showerror("エラー", str(e))
            return
        if state.at_sequence_end:
            self.selected_label.config(text="最後の漫画です")
        elif state.at_sequence_start:
            self.selected_label.config(text="最初の漫画です")
        self.comic_view.set_state(state)
        self._sync_tree_to_state(state)

    def _sync_tree_to_state(self, state):
        for idx, item in enumerate(self._visible_items):
            if item.id == state.root_item_id:
                iid = str(idx)
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self.tree.see(iid)
                self.reading_item = item
                break

    def _open_comic_fullscreen(self):
        if self.comic_view.state is None:
            return
        if self._fullscreen_window is not None:
            try:
                self._fullscreen_window.destroy()
            except tk.TclError:
                pass
        self._fullscreen_window = ComicFullscreenWindow(
            self.comic_view, self.services)

    def _close_comic(self):
        if self.comic_view.session_id:
            self.services.comic_engine.close_session(
                self.comic_view.session_id)
        self.comic_view.clear()
        self.reading_item = None

    # ---------------- 動画 ----------------

    def _play_video(self, item: MediaItem):
        self._save_video_progress(force=True)
        self.playing_item = item
        self.video_placeholder.place_forget()
        self.update_idletasks()
        resume = 0.0
        if self.services.settings.get("resume_playback", True):
            resume = self.services.video_playback.resume_position(item.id)
        try:
            self.mpv.load(item.path, wid=self.video_frame.winfo_id(),
                          volume=self.volume_value.get(), start=resume)
        except FileNotFoundError:
            messagebox.showerror(
                "エラー",
                "mpv が見つかりません。以下でインストールしてください:\n"
                "sudo apt install mpv")
            self.video_placeholder.place(relx=0.5, rely=0.5, anchor="center")
            self.playing_item = None
            return
        self.services.library.mark_opened(item)
        self._video_position = resume
        self._video_duration = 0.0

    def _on_mpv_property(self, name, value):
        if name == "time-pos" and isinstance(value, (int, float)):
            self._video_position = float(value)
            now = time.time()
            if now - self._last_progress_save > 5.0:
                self._last_progress_save = now
                self._save_video_progress()
        elif name == "duration" and isinstance(value, (int, float)):
            self._video_duration = float(value)

    def _save_video_progress(self, force=False):
        item = self.playing_item
        if item is None:
            return
        if not force and self._video_duration <= 0:
            return
        try:
            self.services.video_playback.save_progress(
                item.id, self._video_position,
                self._video_duration or 0.0)
        except Exception:
            pass

    def _on_mpv_nav(self, command):
        # mpvスレッドからの通知をTkメインスレッドへ
        delta = -1 if command == "prev" else 1
        self.after(0, lambda: self._play_offset(delta))

    def _on_global_mouse_nav(self, event, delta):
        if self.media_mode.get() == MODE_COMIC:
            if self.comic_view.winfo_ismapped():
                self._navigate_comic_entry(delta)
                return "break"
        self._play_offset(delta)
        return "break"

    def _play_offset(self, delta):
        if not self._visible_items:
            return
        base = None
        active = (self.playing_item
                  if self.media_mode.get() == MODE_VIDEO
                  else self.reading_item)
        if active is not None:
            for idx, item in enumerate(self._visible_items):
                if item.id == active.id:
                    base = idx
                    break
        if base is None:
            sel = self.tree.selection()
            if sel:
                base = int(sel[0])
        if base is None:
            new_idx = 0
        else:
            new_idx = base + delta
            if new_idx < 0 or new_idx >= len(self._visible_items):
                self.selected_label.config(
                    text="一覧の端に到達しました")
                return
        iid = str(new_idx)
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self._play_selected()

    def _stop_video(self):
        self._save_video_progress(force=True)
        self.mpv.stop()
        self.playing_item = None
        self.video_placeholder.place(relx=0.5, rely=0.5, anchor="center")

    # ---------------- 評価 / 削除 ----------------

    def _apply_rating(self, rating):
        items = self._get_selected_items()
        if not items:
            messagebox.showinfo("情報",
                                "評価を設定するファイルを選択してください")
            return
        errors = []
        ids = [item.id for item in items]
        for item in items:
            try:
                self.services.rating.set_rating(item.path, rating)
            except Exception as e:
                errors.append(f"{item.display_name}: {e}")
        if errors:
            messagebox.showerror("一部の評価設定に失敗しました",
                                 "\n".join(errors))
        self.refresh_list()
        self._reselect_ids(ids)

    def _reselect_ids(self, ids):
        to_select = [str(idx) for idx, item in enumerate(self._visible_items)
                     if item.id in ids]
        if to_select:
            self.tree.selection_set(to_select)
            self.tree.focus(to_select[0])
            self.tree.see(to_select[0])

    def _on_trash(self):
        selected = self._get_selected_items()
        dlg = tk.Toplevel(self)
        dlg.title("削除")
        dlg.configure(bg=C["border"])
        dlg.transient(self)
        dlg.resizable(False, False)
        frame = tk.Frame(dlg, bg=C["panel"], padx=18, pady=14)
        frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        tk.Label(frame, text="削除方法を選択してください", bg=C["panel"],
                 fg=C["text"], font=FONT_TITLE).pack(anchor="w",
                                                     pady=(0, 10))

        def choose(action):
            dlg.destroy()
            action()

        if selected:
            HoverButton(
                frame, text=f"選択中の {len(selected)} 件を削除",
                hover_fg=C["danger"],
                command=lambda: choose(
                    lambda: self._delete_items(selected)),
            ).pack(fill=tk.X, pady=2)
        HoverButton(
            frame, text="★2以下(★1・★2)をまとめて削除",
            hover_fg=C["danger"],
            command=lambda: choose(self._delete_low_rated),
        ).pack(fill=tk.X, pady=2)
        HoverButton(frame, text="キャンセル",
                    command=dlg.destroy).pack(fill=tk.X, pady=(8, 0))
        dlg.update_idletasks()
        dlg.geometry(f"+{self.winfo_rootx() + 60}+{self.winfo_rooty() + 110}")
        dlg.grab_set()

    def _delete_low_rated(self):
        targets = [it for it in self.items if it.rating in (1, 2)]
        if not targets:
            messagebox.showinfo("情報", "★1・★2の項目はありません")
            return
        self._reselect_ids([t.id for t in targets])
        self.update_idletasks()
        self._delete_items(targets)

    def _delete_items(self, items):
        if not items:
            return
        to_trash = bool(self.services.settings.get("delete_to_trash", True))
        method = "ゴミ箱へ移動" if to_trash else "ディスクから完全に削除"
        names = "\n".join(f"  {it.stars}  {it.display_name}"
                          for it in items[:10])
        more = f"\n  …他 {len(items) - 10} 件" if len(items) > 10 else ""
        if not messagebox.askyesno(
            "削除の確認",
            f"以下の {len(items)} 件を{method}します。"
            f"よろしいですか?\n\n{names}{more}",
            icon="warning",
        ):
            return
        errors = []
        for item in items:
            try:
                if self.playing_item and self.playing_item.id == item.id:
                    self._stop_video()
                if self.reading_item and self.reading_item.id == item.id:
                    self._close_comic()
                self.services.library.delete_item(item.id,
                                                  use_trash=to_trash)
                self.items = [i for i in self.items if i.id != item.id]
            except Exception as e:
                errors.append(f"{item.display_name}: {e}")
        if errors:
            messagebox.showerror("一部の削除に失敗しました",
                                 "\n".join(errors))
        self.refresh_list(rescan=False)

    # ---------------- 終了 ----------------

    def _on_close(self):
        self._save_video_progress(force=True)
        self.mpv.stop()
        if self.comic_view.session_id:
            self.services.comic_engine.close_session(
                self.comic_view.session_id)
        self.destroy()
