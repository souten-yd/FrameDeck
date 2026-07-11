"""mpv JSON IPC 双方向コントローラー(デスクトップUI用)。

- プロパティ購読(pause / time-pos / duration / volume / mute / speed /
  track-list / chapter / eof-reached / idle-active / path / media-title)
- マウス戻る/進む・ホイール・クリックはLuaスクリプトから
  `script-message framedeck-nav <cmd>` として送出し、IPCの
  client-message イベントで受け取る(navファイルの150msポーリングは廃止)。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import threading
import time
from pathlib import Path

OBSERVED_PROPERTIES = [
    "pause", "time-pos", "duration", "volume", "mute", "speed",
    "track-list", "chapter", "eof-reached", "idle-active", "path",
    "media-title",
]

MPV_LUA_SCRIPT = r"""
local function nav(cmd)
    mp.commandv("script-message", "framedeck-nav", cmd)
end

mp.add_forced_key_binding("WHEEL_UP", "wheel-seek-forward", function()
    mp.commandv("seek", "10")
end)

mp.add_forced_key_binding("WHEEL_DOWN", "wheel-seek-back", function()
    mp.commandv("seek", "-10")
end)

mp.add_forced_key_binding("MBTN_LEFT", "click-toggle-pause", function()
    mp.commandv("cycle", "pause")
end)

mp.add_forced_key_binding("MBTN_BACK", "mouse-prev-video", function()
    nav("prev")
end)

mp.add_forced_key_binding("MBTN_FORWARD", "mouse-next-video", function()
    nav("next")
end)
"""


class MPVController:
    """mpvプロセスとJSON IPCの管理。イベントはコールバックで通知する。"""

    def __init__(self, runtime_dir: Path,
                 on_property=None, on_nav=None, on_ended=None):
        self._runtime_dir = Path(runtime_dir)
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self.on_property = on_property      # (name, value) -> None
        self.on_nav = on_nav                # ("prev"|"next") -> None
        self.on_ended = on_ended            # () -> None
        self._process: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[int, dict] = {}
        self._pending_cond = threading.Condition(self._lock)
        self.properties: dict[str, object] = {}

        pid = os.getpid()
        self._socket_path = str(self._runtime_dir / f"mpv_{pid}.sock")
        self._lua_path = str(self._runtime_dir / "framedeck_mpv.lua")
        with open(self._lua_path, "w", encoding="utf-8") as f:
            f.write(MPV_LUA_SCRIPT)

    # ---------------- プロセス ----------------

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def load(self, path: str, wid: int | None = None,
             volume: int = 0, start: float = 0.0) -> None:
        """動画を読み込む。未起動ならmpvを起動、起動済みならloadfileする。"""
        if self.is_running() and self._sock is not None:
            self.command("loadfile", path, "replace")
            if start > 0:
                self.command("set_property", "start", str(start))
                self.seek_absolute(start)
            return
        self._spawn(path, wid=wid, volume=volume, start=start)

    def _spawn(self, path: str, wid: int | None, volume: int,
               start: float) -> None:
        self.stop()
        if os.path.exists(self._socket_path):
            try:
                os.remove(self._socket_path)
            except OSError:
                pass
        cmd = [
            "mpv",
            "--force-window=yes", "--keep-open=yes", "--really-quiet",
            f"--volume={volume}",
            f"--script={self._lua_path}",
            f"--input-ipc-server={self._socket_path}",
        ]
        if start > 0:
            cmd.append(f"--start={start:.2f}")
        env = os.environ.copy()
        if wid is not None:
            cmd.append(f"--wid={wid}")
            if platform.system() == "Linux":
                cmd.append("--gpu-context=x11egl")
                # Waylandネイティブでは--wid埋め込みが無視されるため
                # Xwayland(X11)経由で起動させる
                env.pop("WAYLAND_DISPLAY", None)
        cmd.append(path)
        self._process = subprocess.Popen(cmd, env=env)
        self._connect()

    def _connect(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running():
                return
            try:
                if platform.system() == "Windows":
                    raise OSError("Windows named pipe は未実装です")
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._socket_path)
                self._sock = sock
                break
            except OSError:
                time.sleep(0.1)
        if self._sock is None:
            return
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="mpv-ipc-reader"
        )
        self._reader_thread.start()
        for i, name in enumerate(OBSERVED_PROPERTIES, start=1):
            self._send({"command": ["observe_property", i, name]})

    # ---------------- IPC ----------------

    def _send(self, payload: dict) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            pass

    def command(self, *args) -> None:
        self._send({"command": list(args)})

    def get_property(self, name: str, timeout: float = 1.0):
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
        self._send({"command": ["get_property", name],
                    "request_id": request_id})
        deadline = time.time() + timeout
        with self._pending_cond:
            while request_id not in self._pending:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._pending_cond.wait(remaining)
            response = self._pending.pop(request_id)
        return response.get("data")

    def _read_loop(self) -> None:
        buffer = b""
        sock = self._sock
        while sock is not None:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._handle_message(message)

    def _handle_message(self, message: dict) -> None:
        if "request_id" in message:
            with self._pending_cond:
                self._pending[message["request_id"]] = message
                self._pending_cond.notify_all()
            return
        event = message.get("event")
        if event == "property-change":
            name = message.get("name")
            value = message.get("data")
            self.properties[name] = value
            if name == "eof-reached" and value is True and self.on_ended:
                try:
                    self.on_ended()
                except Exception:
                    pass
            if self.on_property:
                try:
                    self.on_property(name, value)
                except Exception:
                    pass
        elif event == "client-message":
            args = message.get("args") or []
            if len(args) >= 2 and args[0] == "framedeck-nav" and self.on_nav:
                try:
                    self.on_nav(args[1])
                except Exception:
                    pass

    # ---------------- 操作API ----------------

    def play(self) -> None:
        self.command("set_property", "pause", False)

    def pause(self) -> None:
        self.command("set_property", "pause", True)

    def toggle_pause(self) -> None:
        self.command("cycle", "pause")

    def seek(self, seconds: float) -> None:
        self.command("seek", seconds)

    def seek_absolute(self, seconds: float) -> None:
        self.command("seek", seconds, "absolute")

    def set_position(self, seconds: float) -> None:
        self.seek_absolute(seconds)

    def set_volume(self, volume: int) -> None:
        self.command("set_property", "volume", max(0, min(100, volume)))

    def set_mute(self, muted: bool) -> None:
        self.command("set_property", "mute", muted)

    def set_speed(self, speed: float) -> None:
        self.command("set_property", "speed", max(0.25, min(3.0, speed)))

    def select_audio_track(self, track_id: int) -> None:
        self.command("set_property", "aid", track_id)

    def select_subtitle_track(self, track_id: int | None) -> None:
        self.command("set_property", "sid",
                     "no" if track_id is None else track_id)

    def set_fullscreen(self, enabled: bool) -> None:
        self.command("set_property", "fullscreen", enabled)

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self.properties = {}
        if os.path.exists(self._socket_path):
            try:
                os.remove(self._socket_path)
            except OSError:
                pass
