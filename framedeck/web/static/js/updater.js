/* FrameDeck updater UI - loaded after app.js so it can extend the existing settings modal. */
"use strict";

(() => {
  const settingsButton = document.getElementById("btn-settings");
  if (!settingsButton) return;

  const originalOpenSettings = settingsButton.onclick;
  let pollTimer = null;
  let reconnectAttempts = 0;

  function bytesLabel(value) {
    const bytes = Number(value) || 0;
    if (!bytes) return "";
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Accept": "application/json", ...(options.headers || {}) },
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) {
      const detail = payload?.detail || payload?.message || `HTTP ${response.status}`;
      throw new Error(detail);
    }
    return payload || {};
  }

  function setMessage(panel, text, kind = "normal") {
    const node = panel.querySelector("[data-update-message]");
    if (!node) return;
    node.textContent = text || "";
    node.dataset.kind = kind;
  }

  function setBusy(panel, busy) {
    panel.querySelectorAll("button").forEach((button) => {
      button.disabled = Boolean(busy);
    });
  }

  function fillLocalStatus(panel, status) {
    const current = panel.querySelector("[data-current-version]");
    const platform = panel.querySelector("[data-platform]");
    if (current) current.textContent = `v${status.current_version || "-"}`;
    if (platform) platform.textContent = status.platform?.label || "判定中";
  }

  function renderCheckResult(panel, result) {
    fillLocalStatus(panel, result);
    const latest = panel.querySelector("[data-latest-version]");
    const asset = panel.querySelector("[data-update-asset]");
    const apply = panel.querySelector("[data-update-apply]");
    const releaseLink = panel.querySelector("[data-release-link]");

    if (latest) latest.textContent = result.latest_version ? `v${result.latest_version}` : "-";
    if (asset) {
      const size = bytesLabel(result.asset?.size);
      asset.textContent = result.asset?.name
        ? `${result.asset.name}${size ? ` (${size})` : ""}`
        : "-";
    }
    if (releaseLink) {
      const url = result.release?.url;
      releaseLink.href = url || "#";
      releaseLink.classList.toggle("hidden", !url);
    }

    if (apply) {
      apply.classList.toggle("hidden", !result.update_available || !result.can_update);
      apply.textContent = result.latest_version
        ? `v${result.latest_version} へ更新`
        : "更新する";
      apply.dataset.targetVersion = result.latest_version || "";
    }

    if (!result.update_available) {
      setMessage(panel, "最新バージョンです。", "ok");
    } else if (!result.can_update) {
      setMessage(panel, result.reason || "更新はありますが、この環境では自動適用できません。", "warn");
    } else {
      setMessage(panel, "更新があります。内容を確認して更新を実行できます。", "ok");
    }
  }

  function renderJob(panel, job) {
    fillLocalStatus(panel, job);
    const progress = panel.querySelector("[data-update-progress]");
    const bar = panel.querySelector("[data-update-progress-bar]");
    const apply = panel.querySelector("[data-update-apply]");
    const check = panel.querySelector("[data-update-check]");
    const active = new Set(["queued", "downloading", "verified", "installing", "restarting"]);
    const isActive = active.has(job.status);

    if (progress) progress.classList.toggle("hidden", !isActive);
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`;
    if (check) check.disabled = isActive;
    if (apply) apply.disabled = isActive;

    if (job.message) {
      setMessage(panel, job.message, job.status === "failed" ? "error" : "normal");
    }

    if (job.status === "failed") {
      setBusy(panel, false);
      return false;
    }
    if (job.status === "completed") {
      setMessage(panel, "更新が完了しました。画面を再読み込みします。", "ok");
      window.setTimeout(() => location.reload(), 600);
      return false;
    }
    // A normal release check also records target_version. Only treat a matching
    // version as a completed restart when an update was actually restarting;
    // otherwise opening Settings would reload the whole page after 600 ms.
    if (job.status === "restarting" && job.target_version &&
        job.current_version === job.target_version) {
      setMessage(panel, `v${job.current_version} へ更新されました。`, "ok");
      window.setTimeout(() => location.reload(), 600);
      return false;
    }
    return isActive;
  }

  async function pollJob(panel) {
    if (!document.body.contains(panel)) {
      stopPolling();
      return;
    }
    try {
      const job = await request("/api/update/status");
      reconnectAttempts = 0;
      const active = renderJob(panel, job);
      if (!active) stopPolling();
    } catch (_) {
      reconnectAttempts += 1;
      setMessage(panel, "FrameDeckの再起動を待っています…", "normal");
      if (reconnectAttempts >= 60) {
        stopPolling();
        setMessage(panel, "再接続できませんでした。ページを再読み込みしてください。", "warn");
      }
    }
  }

  function stopPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = null;
  }

  function startPolling(panel) {
    stopPolling();
    reconnectAttempts = 0;
    pollJob(panel);
    pollTimer = window.setInterval(() => pollJob(panel), 2500);
  }

  async function checkForUpdates(panel) {
    setBusy(panel, true);
    setMessage(panel, "GitHub Releasesを確認しています…");
    try {
      const result = await request("/api/update/check");
      renderCheckResult(panel, result);
    } catch (error) {
      setMessage(panel, error.message || "更新確認に失敗しました。", "error");
    } finally {
      setBusy(panel, false);
    }
  }

  async function applyUpdate(panel, button) {
    const target = button.dataset.targetVersion || "新しいバージョン";
    const ok = window.confirm(
      `FrameDeckを ${target.startsWith("v") ? target : `v${target}`} へ更新します。\n` +
      "更新中は一時的に接続が切れ、完了後にFrameDeckが再起動します。続行しますか？"
    );
    if (!ok) return;

    setBusy(panel, true);
    setMessage(panel, "更新を開始しています…");
    try {
      const job = await request("/api/update/apply", { method: "POST" });
      renderJob(panel, job);
      startPolling(panel);
    } catch (error) {
      setBusy(panel, false);
      setMessage(panel, error.message || "更新を開始できませんでした。", "error");
    }
  }

  function buildPanel() {
    const section = document.createElement("section");
    section.className = "update-settings-card";
    section.dataset.updatePanel = "true";
    section.innerHTML = `
      <div class="update-settings-head">
        <div>
          <h4>FrameDeck <span class="update-current-version" data-current-version>v-</span></h4>
          <p>GitHub Releases の安定版を確認し、この端末に合う更新を適用します。</p>
        </div>
        <button type="button" class="chip-btn" data-update-check>更新を確認</button>
      </div>
      <dl class="update-settings-meta">
        <div><dt>プラットフォーム</dt><dd data-platform>判定中</dd></div>
        <div><dt>最新</dt><dd data-latest-version>-</dd></div>
        <div><dt>更新ファイル</dt><dd data-update-asset>-</dd></div>
      </dl>
      <div class="update-progress hidden" data-update-progress aria-label="更新進捗">
        <span data-update-progress-bar></span>
      </div>
      <p class="update-settings-message" data-update-message aria-live="polite">更新状態を確認しています…</p>
      <div class="update-settings-actions">
        <a class="update-release-link hidden" data-release-link href="#" target="_blank" rel="noopener noreferrer">Releaseを見る</a>
        <button type="button" class="chip-btn hidden" data-update-apply>更新する</button>
      </div>
    `;
    section.querySelector("[data-update-check]")?.addEventListener("click", () => checkForUpdates(section));
    section.querySelector("[data-update-apply]")?.addEventListener("click", (event) => {
      applyUpdate(section, event.currentTarget);
    });
    return section;
  }

  async function mountUpdatePanel() {
    const title = document.getElementById("modal-title");
    const body = document.getElementById("modal-body");
    if (!body || title?.textContent !== "設定") return;
    const container = body.firstElementChild || body;
    if (container.querySelector?.("[data-update-panel]")) return;

    const panel = buildPanel();
    // バージョンと更新状態は、長い設定項目をスクロールしなくても
    // 設定画面を開いた直後に確認できる位置へ置く。
    container.prepend(panel);
    try {
      const job = await request("/api/update/status");
      renderJob(panel, job);
      if (["queued", "downloading", "verified", "installing", "restarting"].includes(job.status)) {
        startPolling(panel);
      } else {
        setMessage(panel, "「更新を確認」でGitHub Releasesを確認します。");
      }
    } catch (error) {
      setMessage(panel, error.message || "更新状態を取得できませんでした。", "error");
    }
  }

  settingsButton.onclick = async function framedeckSettingsWithUpdater(event) {
    stopPolling();
    if (typeof originalOpenSettings === "function") {
      await originalOpenSettings.call(this, event);
    }
    await mountUpdatePanel();
  };
})();
