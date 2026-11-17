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
