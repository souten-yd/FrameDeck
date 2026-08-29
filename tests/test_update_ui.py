from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_update_ui_assets_are_loaded_after_main_assets():
    html = (ROOT / "framedeck/web/templates/index.html").read_text(encoding="utf-8")
    assert '/static/css/updater.css' in html
    assert '/static/js/updater.js' in html
    assert html.index('/static/css/app.css') < html.index('/static/css/updater.css')
    assert html.index('/static/js/app.js') < html.index('/static/js/updater.js')


def test_update_ui_uses_explicit_user_actions():
    js = (ROOT / "framedeck/web/static/js/updater.js").read_text(encoding="utf-8")
    assert '/api/update/status' in js
    assert '/api/update/check' in js
    assert '/api/update/apply' in js
    assert 'window.confirm' in js
    assert '更新を確認' in js
    # Opening Settings reads only local status; GitHub release lookup is behind the button.
    assert 'checkForUpdates(panel)' in js
