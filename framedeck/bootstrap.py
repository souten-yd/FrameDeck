"""起動シーケンス。

APP_MODE == "web"        : FastAPIサーバのみ(Tkinterは初期化しない)
APP_MODE == "web_desktop": Webサーバをバックグラウンドスレッドで起動し、
                           起動確認後にTkinter UIをメインスレッドで起動
"""
from __future__ import annotations

import logging
import logging.handlers
import socket
import sys
import threading
import time
import webbrowser

from .config import (
    AppPaths,
    Settings,
    ensure_runtime_directories,
    resolve_app_paths,
)
from .core.services import Services, build_services
from .core.storage import Storage

logger = logging.getLogger("framedeck")


def configure_logging(paths: AppPaths) -> None:
    handler = logging.handlers.RotatingFileHandler(
        paths.log_file, maxBytes=2 * 1024**2, backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    root.addHandler(console)


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe_host, port)) == 0


def _lan_url_hint(port: int) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.168.255.255", 1))
            ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return f"http://{ip}:{port}"
    except OSError:
        pass
    return None


def _print_urls(host: str, port: int) -> None:
    print(f"[FrameDeck] Web UI: http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        lan = _lan_url_hint(port)
        if lan:
            print(f"[FrameDeck] LAN URL: {lan}")


def _open_browser_when_ready(port: int, timeout: float = 15.0) -> None:
    import urllib.request

    def _wait_and_open():
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{port}"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            return
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_wait_and_open, daemon=True).start()


class WebServerThread:
    """uvicornをバックグラウンドスレッドで動かすラッパー。"""

    def __init__(self, services: Services, host: str, port: int):
        import uvicorn
        from .web.app import create_app

        self._config = uvicorn.Config(
            create_app(services), host=host, port=port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(
            target=self._server.run, daemon=True, name="framedeck-web"
        )

    def start(self, wait_ready: float = 15.0) -> None:
        self._thread.start()
        deadline = time.time() + wait_ready
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return
            if not self._thread.is_alive():
                raise RuntimeError("Webサーバの起動に失敗しました。")
            time.sleep(0.1)
        raise RuntimeError("Webサーバの起動がタイムアウトしました。")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def run(app_mode: str = "web", host: str = "0.0.0.0", port: int = 9000,
        open_browser: bool = False, base_dir=None) -> None:
    paths = resolve_app_paths(base_dir)
    ensure_runtime_directories(paths)
    configure_logging(paths)
    settings = Settings(paths)
    database = Storage(paths.database_file)
    services = build_services(settings, database, paths)
    services.startup_maintenance()

    if _port_in_use(host, port):
        print(
            f"[FrameDeck] エラー: ポート {port} は既に使用されています。\n"
            f"  他のFrameDeckが起動中でないか確認するか、FrameDeck.py の "
            f"WEB_PORT を変更してください。",
            file=sys.stderr,
        )
        services.shutdown()
        sys.exit(1)

    if app_mode == "web":
        run_web_server(services, host, port, open_browser)
        return

    if app_mode == "web_desktop":
        server = WebServerThread(services, host, port)
        server.start()
        _print_urls(host, port)
        if open_browser:
            _open_browser_when_ready(port)
        try:
            run_desktop_ui(services)
        finally:
            server.stop()
            services.shutdown()
        return

    raise ValueError(f"Invalid APP_MODE: {app_mode}")


def run_web_server(services: Services, host: str, port: int,
                   open_browser: bool) -> None:
    import uvicorn
    from .web.app import create_app

    _print_urls(host, port)
    if open_browser:
        _open_browser_when_ready(port)
    try:
        # SIGINT/SIGTERMはuvicornが処理し、lifespanのshutdownで
        # services.shutdown()が呼ばれる
        uvicorn.run(
            create_app(services), host=host, port=port,
            log_level="info", access_log=False,
        )
    finally:
        services.shutdown()


def run_desktop_ui(services: Services) -> None:
    from .desktop.app import DesktopApp

    app = DesktopApp(services)
    app.mainloop()
