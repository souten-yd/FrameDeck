"""Portable FrameDeck command line entry point.

The application code is shared by desktop Linux and NAS packages. Platform
launchers should select behavior through FRAMEDECK_* environment variables
instead of patching FrameDeck itself.
"""
from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class LaunchConfig:
    mode: str
    host: str
    port: int
    open_browser: bool
    home: str | None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolve_launch_config() -> LaunchConfig:
    mode = os.environ.get("FRAMEDECK_MODE", "web").strip()
    if mode not in {"web", "web_desktop"}:
        raise ValueError("FRAMEDECK_MODE must be 'web' or 'web_desktop'")

    host = os.environ.get("FRAMEDECK_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("FRAMEDECK_PORT", "9000"))
    except ValueError as exc:
        raise ValueError("FRAMEDECK_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("FRAMEDECK_PORT must be between 1 and 65535")

    home = os.environ.get("FRAMEDECK_HOME") or None
    return LaunchConfig(
        mode=mode,
        host=host,
        port=port,
        open_browser=_env_bool("FRAMEDECK_OPEN_BROWSER", False),
        home=home,
    )


def _ensure_linux_owned_venv_crypto() -> bool:
    """Install *only* a fixed pairing dependency for a managed Linux venv.

    FrameDeck.py already bootstraps the same venv dependencies. This is the
    alternate `python -m framedeck` path used by some service managers and by
    the built-in Ubuntu source updater after restart. Never modify the QNAP
    bundled runtime, system Python or arbitrary third-party Python venvs.
    """
    if os.environ.get("FRAMEDECK_PROFILE", "").lower() == "qnap-lite":
        return True
    if importlib.util.find_spec("cryptography") is not None:
        return True
    app_root = Path(__file__).resolve().parents[1]
    venv = Path(sys.prefix).resolve()
    if (venv.name != "FrameDeck_venv" or
            not (venv / "pyvenv.cfg").is_file() or
            not (app_root / "FrameDeck.py").is_file() or
            venv.parent != app_root):
        return False
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "cryptography>=43,<47"],
            check=True, timeout=300,
        )
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print(
            "[FrameDeck] Linux pairing crypto dependency setup failed: "
            f"{type(exc).__name__}. The media viewer can still start.",
            file=sys.stderr,
        )
        return False
    return importlib.util.find_spec("cryptography") is not None


def main() -> None:
    cfg = resolve_launch_config()
    _ensure_linux_owned_venv_crypto()
    from .bootstrap import run

    run(
        app_mode=cfg.mode,
        host=cfg.host,
        port=cfg.port,
        open_browser=cfg.open_browser,
        base_dir=cfg.home,
    )


if __name__ == "__main__":
    main()
