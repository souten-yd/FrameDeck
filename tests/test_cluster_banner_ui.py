"""The connected-worker overview belongs to Settings, not below the toolbar."""
from pathlib import Path


def test_cluster_banner_absent_but_settings_overview_preserved():
    root = Path(__file__).resolve().parents[1]
    html = (root / "framedeck/web/templates/index.html").read_text("utf-8")
    css = (root / "framedeck/web/static/css/app.css").read_text("utf-8")
    js = (root / "framedeck/web/static/js/app.js").read_text("utf-8")

    # No hidden or dynamically recreated banner can consume menu space.
    assert 'id="cluster-summary"' not in html
    assert ".cluster-summary" not in css
    assert '$("cluster-summary")' not in js

    # Media quality state still updates from the same /api/cluster/status.
    assert "async function refreshClusterOverview()" in js
    assert 'S.clusterStatus = result' in js
    assert "updateWorkerQualityOptions(result.peer?.offload_active === true)" in js
    assert "if (connection && workerOverview(connection))" in js

    # The status is still available when actually opening Settings.
    assert "clusterStatus.textContent = workerOverview(result)" in js
