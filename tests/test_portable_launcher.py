import pytest

from framedeck.__main__ import resolve_launch_config


def test_portable_launcher_defaults(monkeypatch):
    for key in (
        "FRAMEDECK_MODE", "FRAMEDECK_HOST", "FRAMEDECK_PORT",
        "FRAMEDECK_OPEN_BROWSER", "FRAMEDECK_HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = resolve_launch_config()
    assert cfg.mode == "web"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.open_browser is False
    assert cfg.home is None


def test_portable_launcher_reads_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAMEDECK_MODE", "web_desktop")
    monkeypatch.setenv("FRAMEDECK_HOST", "127.0.0.1")
    monkeypatch.setenv("FRAMEDECK_PORT", "9100")
    monkeypatch.setenv("FRAMEDECK_OPEN_BROWSER", "yes")
    monkeypatch.setenv("FRAMEDECK_HOME", str(tmp_path))
    cfg = resolve_launch_config()
    assert cfg.mode == "web_desktop"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9100
    assert cfg.open_browser is True
    assert cfg.home == str(tmp_path)


@pytest.mark.parametrize("value", ["0", "65536", "nope"])
def test_portable_launcher_rejects_invalid_port(monkeypatch, value):
    monkeypatch.setenv("FRAMEDECK_PORT", value)
    with pytest.raises(ValueError):
        resolve_launch_config()
