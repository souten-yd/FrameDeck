"""Portable FrameDeck command line entry point.

The application code is shared by desktop Linux and NAS packages. Platform
launchers should select behavior through FRAMEDECK_* environment variables
instead of patching FrameDeck itself.
"""
from __future__ import annotations

import os
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


def main() -> None:
    cfg = resolve_launch_config()
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
