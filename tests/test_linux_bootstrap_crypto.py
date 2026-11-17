"""Linux bootstrap regression: optional keyless pairing crypto is installed by launcher."""
from __future__ import annotations

import ast
from pathlib import Path


def test_linux_launcher_declares_crypto_for_automatic_venv_install():
    """v2.4.23 QPKG included crypto but Linux FrameDeck.py omitted it.

    The launcher checks importability on every invocation, so source-only
    updates must keep X25519/HKDF as a declared runtime dependency.
    """
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "FrameDeck.py").read_text("utf-8"))
    pairs = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "VENV_PACKAGES"
                for t in node.targets)
    )
    declared = ast.literal_eval(pairs.value)
    assert declared["cryptography"] == "cryptography>=43,<47"

    qpkg_requirements = (
        root / "packaging" / "qnap" / "requirements-qnap.txt"
    ).read_text("utf-8")
    assert "cryptography>=43,<47" in qpkg_requirements


def test_linux_launcher_installation_is_explicitly_in_venv_bootstrap():
    """No new unauthenticated API or arbitrary server-command install path."""
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "FrameDeck.py").read_text("utf-8")
    assert "if os.path.abspath(sys.executable) != os.path.abspath(venv_python)" in launcher
    assert 'if importlib.util.find_spec(module) is None' in launcher
    assert '[sys.executable, "-m", "pip", "install", *missing]' in launcher
    assert "cryptography" in launcher


def test_module_launcher_repairs_own_missing_venv_dependency(monkeypatch, tmp_path):
    """Built-in updater can restart via -m framedeck, not only FrameDeck.py."""
    from types import SimpleNamespace
    from framedeck import __main__ as module_runner

    root = tmp_path / "FrameDeck"
    pkg = root / "framedeck"
    pkg.mkdir(parents=True)
    (root / "FrameDeck.py").write_text("# launcher\n", "utf-8")
    venv = root / "FrameDeck_venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", "utf-8")
    monkeypatch.setattr(module_runner, "__file__", str(pkg / "__main__.py"))
    monkeypatch.setattr(module_runner.sys, "prefix", str(venv))
    monkeypatch.delenv("FRAMEDECK_PROFILE", raising=False)
    installed = {"ready": False}
    monkeypatch.setattr(
        module_runner.importlib.util, "find_spec",
        lambda name: SimpleNamespace() if installed["ready"] else None,
    )
    calls = []
    def fake_run(argv, **options):
        calls.append((argv, options))
        installed["ready"] = True
    monkeypatch.setattr(module_runner.subprocess, "run", fake_run)
    assert module_runner._ensure_linux_owned_venv_crypto() is True
    assert len(calls) == 1
    assert calls[0][0] == [
        module_runner.sys.executable, "-m", "pip", "install",
        "cryptography>=43,<47",
    ]
    assert calls[0][1]["check"] is True
    assert calls[0][1]["timeout"] <= 300


def test_module_launcher_does_not_install_on_qnap_or_unmanaged_python(
        monkeypatch, tmp_path):
    from framedeck import __main__ as module_runner
    monkeypatch.setattr(module_runner.importlib.util, "find_spec", lambda _: None)
    calls = []
    monkeypatch.setattr(
        module_runner.subprocess, "run",
        lambda *a, **k: calls.append((a, k)),
    )
    monkeypatch.setenv("FRAMEDECK_PROFILE", "qnap-lite")
    assert module_runner._ensure_linux_owned_venv_crypto() is True
    assert calls == []

    monkeypatch.delenv("FRAMEDECK_PROFILE", raising=False)
    monkeypatch.setattr(module_runner.sys, "prefix", str(tmp_path))
    assert module_runner._ensure_linux_owned_venv_crypto() is False
    assert calls == []
