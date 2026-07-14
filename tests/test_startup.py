"""起動時の外部アプリ連携に関する回帰テスト。"""

import ast
import inspect
from pathlib import Path

from framedeck.bootstrap import run


def test_browser_auto_open_is_disabled_by_default():
    """サービス起動したFrameDeckがブラウザを子プロセスにしない。"""
    assert inspect.signature(run).parameters["open_browser"].default is False


def test_launcher_does_not_request_browser_auto_open():
    launcher = Path(__file__).parents[1] / "FrameDeck.py"
    tree = ast.parse(launcher.read_text(encoding="utf-8"))

    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "AUTO_OPEN_BROWSER"
            for target in node.targets
        )
    )
    assert isinstance(assignment.value, ast.Constant)
    assert assignment.value.value is False
