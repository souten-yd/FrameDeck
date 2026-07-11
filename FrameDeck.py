#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FrameDeck - ローカルメディアサーバ兼ビューア

起動方法:
    python3 FrameDeck.py

APP_MODE を "web" にするとWebサーバのみ、"web_desktop" にすると
WebサーバとデスクトップUI(Tkinter)を同時起動します。
"""

import os
import sys
import platform
import subprocess
import importlib.util
import venv

# ============================================================
# FrameDeck startup configuration
# ============================================================

APP_MODE = "web"
# "web"        : Webサーバのみ起動
# "web_desktop": WebサーバとデスクトップUIを同時起動

WEB_HOST = "0.0.0.0"
WEB_PORT = 9000

AUTO_OPEN_BROWSER = True
ENABLE_DESKTOP_UI = APP_MODE == "web_desktop"

VALID_APP_MODES = {"web", "web_desktop"}

if APP_MODE not in VALID_APP_MODES:
    raise ValueError(
        f"Invalid APP_MODE: {APP_MODE}. "
        f"Expected one of {sorted(VALID_APP_MODES)}"
    )

# ============================================================
# 仮想環境ブートストラップ
# ============================================================

VENV_PACKAGES = {
    "PIL": "Pillow",
    "rarfile": "rarfile",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "aiofiles": "aiofiles",
    "pydantic": "pydantic",
    "send2trash": "send2trash",
}


def _venv_python_path(venv_dir):
    if platform.system() == "Windows":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _ensure_runtime_environment():
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    script_stem = os.path.splitext(os.path.basename(script_path))[0]
    venv_dir = os.path.join(script_dir, f"{script_stem}_venv")
    venv_python = _venv_python_path(venv_dir)

    if os.path.abspath(sys.executable) != os.path.abspath(venv_python):
        if not os.path.exists(venv_python):
            print(f"[FrameDeck] 仮想環境を作成しています: {venv_dir}")
            venv.EnvBuilder(with_pip=True).create(venv_dir)
        os.execv(venv_python, [venv_python, script_path, *sys.argv[1:]])

    missing = [
        package for module, package in VENV_PACKAGES.items()
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        print(f"[FrameDeck] 依存パッケージをインストールします: {missing}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *missing],
                timeout=600,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            # 無限再起動を避け、手動コマンドを提示して終了する
            print("[FrameDeck] 依存パッケージのインストールに失敗しました。", file=sys.stderr)
            print(f"  エラー: {e}", file=sys.stderr)
            print("  以下を手動で実行してください:", file=sys.stderr)
            print(f"    {sys.executable} -m pip install " + " ".join(f'"{p}"' for p in missing),
                  file=sys.stderr)
            sys.exit(1)

    return venv_dir


_APP_BASE_DIR = _ensure_runtime_environment()


def main():
    from framedeck.bootstrap import run
    run(
        app_mode=APP_MODE,
        host=WEB_HOST,
        port=WEB_PORT,
        open_browser=AUTO_OPEN_BROWSER,
        base_dir=_APP_BASE_DIR,
    )


if __name__ == "__main__":
    main()
