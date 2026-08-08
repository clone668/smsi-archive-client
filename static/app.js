(() => {
  "use strict";
  const state = {
    csrf: "", config: null, runtime: null, updates: null, days: [], events: [],
    timer: null, updateTimer: null,
    fileBrowsers: {
      remote: { request: 0, path: "", index: null, meta: null },
      local: { request: 0, path: "" },
    },
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
  const icon = name => {
    const paths = {
      folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"/><path d="M3 9h18"/>',
      file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h6"/>',
      chevron: '<path d="m9 18 6-6-6-6"/>',
      home: '<path d="m3 10 9-7 9 7"/><path d="M5 9v11h14V9M9 20v-6h6v6"/>',
    };
    return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || ""}</svg>`;
  };
  const bytes = value => {
    let n = Number(value || 0);
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  };
  const timeText = value => {
    if (!value) return "--";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString("zh-CN", { hour12: false });
  };
  const statusMap = {
    verified: ["已验证", "good"], downloading: ["下载中", "warn"], verifying: ["校验中", "warn"],
    ready: ["可下载", "warn"], remote_running: ["远端归档中", "warn"], waiting_manifest: ["等待发布", "warn"],
    remote_failed: ["远端失败", "bad"], error: ["处理失败", "bad"], manifest_changed: ["清单已变化", "bad"],
    cancelled: ["已取消", "warn"],
    unknown: ["未知", ""],
  };
  const updatePhaseMap = {
    idle: "等待操作", checking: "正在检查版本", downloading: "正在下载更新包",
    verifying: "正在校验更新包", ready: "更新已准备", failed: "更新失败",
  };
  const archivePhaseMap = {
    downloading: "正在下载归档", verifying: "正在校验归档", cancelled: "归档已取消",
  };
  let toastTimer;

  function toast(message, error = false) {
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.className = "toast"; }, 3500);
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.csrf;
    const response = await fetch(path, { credentials: "same-origin", ...options, headers });
    let payload;
    try { payload = await response.json(); } catch { payload = { error: `请求失败 (${response.status})` }; }
    if (response.status === 401) { location.href = "/login"; throw new Error("登录已失效"); }
    if (!response.ok || payload.ok === false) throw new Error(payload.error || `请求失败 (${response.status})`);
    return payload;
  }

  function renderMetrics() {
    const runtime = state.runtime || {};
    const verified = state.days.filter(item => item.status === "verified");
    const bad = state.days.filter(item => ["error", "remote_failed", "manifest_changed"].includes(item.status));
    const pending = state.days.filter(item => !["verified", "error", "remote_failed", "manifest_changed"].includes(item.status));
    $("#metric-runtime").textContent = runtime.running ? "执行中" : (runtime.auto_download ? "自动运行" : "已暂停");
    $("#metric-runtime-detail").textContent = runtime.running ? runtime.detail : `下次检查 ${timeText(runtime.next_scan_at)}`;
    $("#metric-verified").textContent = String(verified.length);
    $("#metric-latest").textContent = verified.length ? `最新 ${verified.map(item => item.archive_date).sort().at(-1)}` : "尚无本地归档";
    $("#metric-pending").textContent = String(pending.length);
    $("#metric-errors").textContent = bad.length ? `${bad.length} 项需要处理` : "没有失败项";
    const disk = runtime.disk || {};
    const ratio = disk.total ? Math.round(Number(disk.used) / Number(disk.total) * 100) : 0;
    $("#metric-disk").textContent = runtime.disk_error ? "不可用" : `${ratio}%`;
    $("#metric-disk-detail").textContent = runtime.disk_error || `可用 ${bytes(disk.free)} / ${bytes(disk.total)}`;
    $("#connection-state").textContent = runtime.running ? "任务执行中" : "客户端在线";
    $("#connection-state").className = "status-dot good";
    $("#cancel-task").classList.toggle("hidden", !runtime.running);
    $("#scan-only").disabled = !!runtime.running || !!runtime.pending;
    $("#scan-download").disabled = !!runtime.running || !!runtime.pending;
  }

  function renderProfiles() {
    const profiles = state.config?.profiles || [];
    $("#profile-count").textContent = String(profiles.length);
    const root = $("#profile-list");
    if (!profiles.length) { root.className = "profile-list empty-state"; root.textContent = "尚未添加采集服务器"; return; }
    root.className = "profile-list";
    root.innerHTML = profiles.map(profile => {
      const records = state.days.filter(item => item.profile_id === profile.profile_id);
      const verified = records.filter(item => item.status === "verified").length;
      const alerts = records.filter(item => ["error", "remote_failed", "manifest_changed"].includes(item.status)).length;
      const source = { google_drive: "Google Drive", ubuntu_sftp: "Ubuntu 内网", verified_directory: "已验证目录" }[profile.source_type] || profile.source_type;
      return `<article class="profile-row"><div><h3>${escapeHtml(profile.display_name)}</h3><p>collector=${escapeHtml(profile.collector_id)}</p></div><span class="source-label">${source}</span><div class="profile-stats"><span>已验证 <strong>${verified}</strong></span><span>异常 <strong>${alerts}</strong></span><span>${profile.enabled ? "已启用" : "已停用"}</span></div></article>`;
    }).join("");
  }

  function renderDays() {
    const body = $("#days-body");
    const profiles = Object.fromEntries((state.config?.profiles || []).map(item => [item.profile_id, item.display_name]));
    $("#day-summary").textContent = `${state.days.length} 条本地状态记录`;
    if (!state.days.length) { body.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无记录</td></tr>'; return; }
    body.innerHTML = state.days.map(item => {
      const live = state.runtime?.progress;
      const liveMatches = live && live.profile_id === item.profile_id && live.archive_date === item.archive_date && ["downloading", "verifying"].includes(live.phase);
      const objectsDone = liveMatches ? Number(live.objects_done || 0) : Number(item.objects_done || 0);
      const objectCount = liveMatches ? Number(live.object_count || item.object_count || 0) : Number(item.object_count || 0);
      const bytesDone = liveMatches ? Number(live.bytes_transferred || live.bytes_done || 0) : Number(item.bytes_done || 0);
      const bytesTotal = liveMatches ? Number(live.bytes_total || item.bytes_total || 0) : Number(item.bytes_total || 0);
      const [label, tone] = statusMap[item.status] || [item.status, ""];
      const liveDetail = liveMatches ? [
        live.current_object ? `当前 ${live.current_object.split("/").at(-1)}` : "",
        live.speed_bytes_per_second ? `${bytes(live.speed_bytes_per_second)}/秒` : "",
        live.eta_seconds != null ? `剩余约 ${durationText(live.eta_seconds)}` : "",
      ].filter(Boolean).join(" · ") : "";
      const detailText = liveDetail || item.error || item.detail || "";
      const dataText = objectCount ? `${bytesDone ? bytes(bytesDone) : "0 B"} / ${bytes(bytesTotal)}` : bytes(bytesTotal);
      const statusDetail = liveDetail ? `<small class="row-progress-detail">${escapeHtml(liveDetail)}</small>` : "";
      const action = `${item.status === "verified" ? `<button class="button small secondary verify-day" data-profile="${escapeHtml(item.profile_id)}" data-date="${escapeHtml(item.archive_date)}">重新校验</button>` : ""}<button class="button small quiet day-detail-toggle" data-profile="${escapeHtml(item.profile_id)}" data-date="${escapeHtml(item.archive_date)}">文件详情</button>`;
      return `<tr title="${escapeHtml(detailText)}"><td>${escapeHtml(item.archive_date)}</td><td>${escapeHtml(profiles[item.profile_id] || item.profile_id)}</td><td><span class="state-pill ${tone}">${escapeHtml(label)}</span>${statusDetail}</td><td>${objectsDone}/${objectCount}</td><td>${dataText}</td><td>${timeText(item.updated_at)}</td><td class="row-actions">${action}</td></tr><tr class="day-detail-row hidden"><td colspan="7"><div class="day-detail-content"></div></td></tr>`;
    }).join("");
    $$(".verify-day", body).forEach(button => button.addEventListener("click", () => verifyDay(button.dataset.profile, button.dataset.date)));
    $$(".day-detail-toggle", body).forEach(button => button.addEventListener("click", () => toggleDayDetail(button)));
  }

  function durationText(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    if (value < 60) return `${value} 秒`;
    const minutes = Math.floor(value / 60);
    const rest = value % 60;
    return minutes < 60 ? `${minutes} 分 ${rest} 秒` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
  }

  const localObjectStatus = {
    present: ["本地存在", "good"], downloading: ["下载中", "warn"], staged: ["已暂存", "warn"],
    missing: ["待下载", ""], mismatch: ["大小异常", "bad"],
  };

  async function toggleDayDetail(button) {
    const detailRow = button.closest("tr").nextElementSibling;
    const content = $(".day-detail-content", detailRow);
    if (!detailRow.classList.contains("hidden")) { detailRow.classList.add("hidden"); button.textContent = "文件详情"; return; }
    detailRow.classList.remove("hidden"); button.textContent = "收起详情";
    if (detailRow.dataset.loaded === "true") return;
    content.innerHTML = '<p class="empty-cell">正在读取远端 manifest 和本地文件状态...</p>';
    try {
      const query = `?profile_id=${encodeURIComponent(button.dataset.profile)}&archive_date=${encodeURIComponent(button.dataset.date)}`;
      const result = await api(`/api/day-detail${query}`);
      const detail = result.detail;
      const remoteState = statusMap[detail.remote.state] || [detail.remote.state, ""];
      content.innerHTML = `<div class="day-detail-summary"><span>远端：<strong class="state-text ${remoteState[1]}">${escapeHtml(remoteState[0])}</strong> · ${Number(detail.remote.object_count || 0)} 个对象 · ${bytes(detail.remote.bytes_total)}</span><span>本地：${Number(detail.local.object_count || 0)}/${Number(detail.remote.object_count || 0)} 个对象 · ${bytes(detail.local.bytes_done)} / ${bytes(detail.remote.bytes_total)}</span><span>${escapeHtml(detail.remote.detail || detail.local.detail || "")}</span></div>${detail.objects.length ? `<div class="table-wrap"><table class="object-table"><thead><tr><th>对象</th><th>远端</th><th>本地</th><th>大小</th><th>本地字节</th></tr></thead><tbody>${detail.objects.map(item => { const state = localObjectStatus[item.local_state] || [item.local_state, ""]; return `<tr><td title="${escapeHtml(item.relative_key)}">${escapeHtml(item.name)}</td><td><span class="state-pill good">已列入 manifest</span></td><td><span class="state-pill ${state[1]}">${escapeHtml(state[0])}</span></td><td>${bytes(item.size_bytes)}</td><td>${bytes(item.local_bytes)}</td></tr>`; }).join("")}</tbody></table></div>` : '<p class="empty-cell">远端尚未发布可读取的 manifest。</p>'}`;
      detailRow.dataset.loaded = "true";
    } catch (error) { content.innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`; }
  }

  function renderEvents() {
    const root = $("#event-list");
    if (!state.events.length) { root.innerHTML = '<p class="empty-cell">暂无记录</p>'; return; }
    root.innerHTML = state.events.map(item => `<article class="event-item ${escapeHtml(item.level)}"><header><strong>${escapeHtml(item.event)}</strong><time>${timeText(item.created_at)}</time></header><p>${escapeHtml([item.profile_id, item.archive_date, item.detail].filter(Boolean).join(" · ") || "--")}</p></article>`).join("");
  }

  function renderAll() { renderMetrics(); renderProfiles(); renderDays(); renderEvents(); }

  function renderUpdates() {
    const updates = state.updates || {};
    const latest = updates.latest || {};
    const operation = updates.operation || {};
    const current = String(updates.current_revision || "未知").slice(0, 12);
    const remote = String(latest.revision || "").slice(0, 12);
    const staged = String(updates.staged_revision || "").slice(0, 12);
    const phase = String(operation.phase || "idle");
    const active = !!operation.active;
    const archiveBusy = !!state.runtime?.running;
    let label = "未检查";
    let tone = "";
    let status = `当前版本 ${current}，尚未检查远端版本。`;
    if (active) {
      label = updatePhaseMap[phase] || "更新进行中";
      tone = "warn";
      status = String(operation.detail || label);
    } else if (phase === "failed") {
      label = "更新失败";
      tone = "bad";
      status = String(operation.error || operation.detail || "更新操作失败");
    } else if (staged) {
      label = "待重启";
      tone = "good";
      status = archiveBusy ? `版本 ${staged} 已准备，归档任务完成后可重启。` : `版本 ${staged} 已准备，可以重启客户端。`;
    } else if (remote && updates.update_available) {
      label = "有新版本";
      tone = "warn";
      status = `发现版本 ${remote}，当前运行 ${current}。`;
    } else if (remote) {
      label = "已是最新";
      tone = "good";
      status = `当前已运行最新版本 ${remote}。`;
    }

    $("#update-current").textContent = current;
    $("#update-latest").textContent = remote || "--";
    $("#update-release-note").textContent = latest.message || "尚未读取版本说明";
    $("#update-state-text").textContent = label;
    $("#update-status").textContent = status;
    $("#update-state").textContent = label;
    $("#update-state").className = `state-pill ${tone}`.trim();
    $("#nav-update-state").textContent = label;
    $("#check-update").disabled = active;
    $("#download-update").disabled = active || !updates.update_available || !!staged;
    $("#restart-update").disabled = active || !staged || archiveBusy || !updates.helper_available;
    const blockedReason = $("#update-blocked-reason");
    if (staged && archiveBusy) {
      const progress = state.runtime?.progress || {};
      const archivePhase = archivePhaseMap[progress.phase] || "归档任务正在运行";
      const objects = progress.object_count ? ` · ${Number(progress.objects_done || 0)}/${Number(progress.object_count)} 个对象` : "";
      blockedReason.textContent = `${archivePhase}${objects}；归档完成后才能重启客户端，避免中断校验。`;
      blockedReason.className = "workflow-notice warn";
      $("#restart-update").title = "归档任务运行中，完成后才允许重启";
    } else if (staged && !active && updates.helper_available) {
      blockedReason.textContent = "更新包已下载并校验，可以重启客户端。";
      blockedReason.className = "workflow-notice good";
      $("#restart-update").title = "切换已校验的更新包并重启客户端";
    } else if (staged && !updates.helper_available) {
      blockedReason.textContent = "更新助手不可用，暂时不能切换版本。";
      blockedReason.className = "workflow-notice bad";
      $("#restart-update").title = "更新助手不可用";
    } else {
      blockedReason.textContent = "";
      blockedReason.className = "workflow-notice hidden";
      $("#restart-update").title = "";
    }
    $("#restart-hint").textContent = !updates.helper_available
      ? "Ubuntu 更新助手不可用"
      : archiveBusy && staged
        ? "归档任务结束后可重启"
        : staged ? "更新已准备，可以安全切换" : "更新准备完成后启用";

    const percent = Number.isFinite(Number(operation.percent)) ? Math.max(0, Math.min(100, Number(operation.percent))) : null;
    const track = $("#update-progress-track");
    const indeterminate = active && percent === null;
    track.classList.toggle("indeterminate", indeterminate);
    $("#update-progress-bar").style.width = `${percent ?? (staged ? 100 : 0)}%`;
    track.setAttribute("aria-valuenow", percent == null ? "0" : String(percent));
    track.setAttribute("aria-valuetext", indeterminate ? label : `${percent ?? (staged ? 100 : 0)}%`);
    $("#update-phase").textContent = updatePhaseMap[phase] || label;
    $("#update-percent").textContent = percent == null ? (active ? "处理中" : staged ? "100%" : "--") : `${percent.toFixed(percent % 1 ? 1 : 0)}%`;
    const done = Number(operation.bytes_done || 0);
    const total = Number(operation.bytes_total || 0);
    $("#update-bytes").textContent = total ? `${bytes(done)} / ${bytes(total)}` : done ? `已处理 ${bytes(done)}` : "数据量 --";
    $("#update-speed").textContent = Number(operation.speed_bytes_per_second || 0) > 0 ? `${bytes(operation.speed_bytes_per_second)}/秒` : "速度 --";
    $("#update-eta").textContent = operation.eta_seconds != null ? `剩余约 ${durationText(operation.eta_seconds)}` : "剩余时间 --";
    $("#update-detail").textContent = operation.error || operation.detail || (staged ? "更新包已完成下载和校验，等待手动重启。" : "尚未开始更新操作。");
    $("#update-detail").className = `update-detail${phase === "failed" ? " error" : ""}`;
  }

  async function pollUpdateStatus() {
    try {
      const result = await api("/api/update/status");
      state.updates = result.updates;
      state.runtime = { ...(state.runtime || {}), running: !!result.archive_running };
      renderUpdates();
    } catch (error) {
      $("#update-detail").textContent = error.message;
      $("#update-detail").className = "update-detail error";
    }
  }

  function startUpdatePolling() {
    clearInterval(state.updateTimer);
    state.updateTimer = setInterval(pollUpdateStatus, 750);
  }

  function stopUpdatePolling() {
    clearInterval(state.updateTimer);
    state.updateTimer = null;
  }

  async function checkUpdate() {
    state.updates.operation = { active: true, phase: "checking", detail: "正在检查 GitHub 最新版本" };
    renderUpdates();
    startUpdatePolling();
    try {
      const result = await api("/api/update/check");
      state.updates = result.updates;
      renderUpdates();
      toast(result.updates.update_available ? "发现新版本" : "当前已是最新版本");
    } catch (error) { toast(error.message, true); }
    finally { stopUpdatePolling(); await pollUpdateStatus(); }
  }

  async function downloadUpdate() {
    const revision = state.updates?.latest?.revision;
    if (!revision) return toast("请先检查更新", true);
    state.updates.operation = { active: true, phase: "checking", detail: "正在确认目标版本" };
    renderUpdates();
    startUpdatePolling();
    try {
      const result = await api("/api/update/download", { method: "POST", body: JSON.stringify({ revision }) });
      state.updates = result.updates;
      renderUpdates();
      toast("更新已准备，归档任务空闲后可重启客户端");
    } catch (error) { toast(error.message, true); }
    finally { stopUpdatePolling(); await pollUpdateStatus(); }
  }

  async function restartUpdate() {
    const button = $("#restart-update");
    button.disabled = true;
    try {
      await api("/api/update/restart", { method: "POST", body: "{}" });
      toast("更新已切换，客户端正在重启");
    } catch (error) { toast(error.message, true); button.disabled = false; }
  }

  function renderFileProfileOptions() {
    const profiles = state.config?.profiles || [];
    for (const scope of ["remote", "local"]) {
      const select = $(`#${scope}-profile`);
      const previous = select.value;
      select.innerHTML = profiles.length
        ? profiles.map(item => `<option value="${escapeHtml(item.profile_id)}">${escapeHtml(item.display_name)}</option>`).join("")
        : '<option value="">尚未配置采集服务器</option>';
      select.disabled = !profiles.length;
      if (profiles.some(item => item.profile_id === previous)) select.value = previous;
    }
  }

  function clearFileBrowser(scope, message) {
    $(`#${scope}-breadcrumbs`).innerHTML = "";
    const dateSelect = $(`#${scope}-date`);
    dateSelect.innerHTML = '<option value="">暂无日期</option>';
    dateSelect.disabled = true;
    $(`#${scope}-summary`).textContent = message;
    const columns = scope === "remote" ? 6 : 5;
    $(`#${scope}-files-body`).innerHTML = `<tr><td colspan="${columns}" class="empty-cell">${escapeHtml(message)}</td></tr>`;
  }

  async function loadFileDates(scope) {
    const profileId = $(`#${scope}-profile`).value;
    if (!profileId) return clearFileBrowser(scope, "请先在设置中添加采集服务器");
    const request = ++state.fileBrowsers[scope].request;
    state.fileBrowsers[scope].path = "";
    if (scope === "remote") {
      state.fileBrowsers.remote.index = null;
      state.fileBrowsers.remote.meta = null;
    }
    clearFileBrowser(scope, scope === "remote" ? "正在读取网盘日期..." : "正在读取本地日期...");
    try {
      const query = new URLSearchParams({ profile_id: profileId, scope });
      const payload = await api(`/api/files/dates?${query}`);
      if (request !== state.fileBrowsers[scope].request) return;
      const dates = payload.result.dates || [];
      const select = $(`#${scope}-date`);
      select.innerHTML = dates.length
        ? dates.map(item => `<option value="${escapeHtml(item.archive_date)}">${escapeHtml(item.archive_date)}</option>`).join("")
        : '<option value="">暂无日期</option>';
      select.disabled = !dates.length;
      if (!dates.length) return clearFileBrowser(scope, scope === "remote" ? "网盘中没有可浏览的归档日期" : "本地没有正式或暂存归档目录");
      await loadFileList(scope);
    } catch (error) {
      if (request !== state.fileBrowsers[scope].request) return;
      clearFileBrowser(scope, error.message);
    }
  }

  async function loadFileList(scope, requestedPath = null) {
    const profileId = $(`#${scope}-profile`).value;
    const archiveDate = $(`#${scope}-date`).value;
    if (!profileId || !archiveDate) return;
    const path = requestedPath === null ? state.fileBrowsers[scope].path : requestedPath;
    state.fileBrowsers[scope].path = path || "";
    if (
      scope === "remote"
      && requestedPath !== null
      && state.fileBrowsers.remote.index
      && state.fileBrowsers.remote.meta?.profile_id === profileId
      && state.fileBrowsers.remote.meta?.archive_date === archiveDate
    ) {
      renderRemoteIndex(path || "");
      return;
    }
    const request = ++state.fileBrowsers[scope].request;
    const columns = scope === "remote" ? 6 : 5;
    const body = $(`#${scope}-files-body`);
    body.innerHTML = `<tr><td colspan="${columns}" class="empty-cell">正在读取文件列表...</td></tr>`;
    $(`#${scope}-summary`).textContent = `正在读取 ${archiveDate}...`;
    try {
      const query = new URLSearchParams({ profile_id: profileId, archive_date: archiveDate, scope });
      if (path) query.set("path", path);
      const payload = await api(`/api/files/list?${query}`);
      if (request !== state.fileBrowsers[scope].request) return;
      if (scope === "remote" && payload.result.browse_index) {
        state.fileBrowsers.remote.index = payload.result.browse_index;
        state.fileBrowsers.remote.meta = {
          profile_id: profileId,
          archive_date: archiveDate,
          state: payload.result.state,
          detail: payload.result.detail,
        };
      }
      renderFileList(scope, payload.result);
    } catch (error) {
      if (request !== state.fileBrowsers[scope].request) return;
      body.innerHTML = `<tr><td colspan="${columns}" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
      $(`#${scope}-summary`).textContent = error.message;
    }
  }

  function renderRemoteIndex(path) {
    const browserState = state.fileBrowsers.remote;
    const entries = {};
    for (const item of browserState.index || []) {
      const relative = String(item.path || "");
      if (path && !relative.startsWith(`${path}/`)) continue;
      const remainder = path ? relative.slice(path.length + 1) : relative;
      if (!remainder) continue;
      const [first, ...rest] = remainder.split("/");
      const childPath = path ? `${path}/${first}` : first;
      if (rest.length) {
        const entry = entries[childPath] || { type: "directory", path: childPath, name: first, entry_count: 0 };
        entry.entry_count += 1;
        entries[childPath] = entry;
      } else {
        entries[childPath] = { ...item, path: childPath, name: first };
      }
    }
    const ordered = Object.values(entries).sort((left, right) => {
      const directoryOrder = Number(left.type !== "directory") - Number(right.type !== "directory");
      return directoryOrder || String(left.name).localeCompare(String(right.name));
    });
    const files = ordered.filter(item => item.type === "file");
    renderFileList("remote", {
      scope: "remote",
      profile_id: browserState.meta.profile_id,
      archive_date: browserState.meta.archive_date,
      path,
      parent_path: path.split("/").slice(0, -1).join("/"),
      state: browserState.meta.state,
      detail: browserState.meta.detail,
      entry_count: ordered.length,
      object_count: files.length,
      bytes_total: files.reduce((total, item) => total + Number(item.size_bytes || 0), 0),
      row_count: files.reduce((total, item) => total + Number(item.row_count || 0), 0),
      entries: ordered,
    });
  }

  function renderFileList(scope, result) {
    const entries = result.entries || [];
    state.fileBrowsers[scope].path = result.path || "";
    const [stateLabel, stateTone] = statusMap[result.state] || [result.state || "未知", ""];
    renderFileBreadcrumbs(scope, result.path || "", result.parent_path || "");
    $(`#${scope}-summary`).innerHTML = `<span><strong>${escapeHtml(result.archive_date)}</strong></span><span>${Number(result.entry_count || 0)} 项</span><span>${bytes(result.bytes_total)}</span>${Number(result.row_count || 0) ? `<span>${Number(result.row_count).toLocaleString("zh-CN")} 行</span>` : ""}<span class="state-text ${stateTone}">${escapeHtml(stateLabel)}</span><span>${escapeHtml(result.detail || "")}</span>`;
    const body = $(`#${scope}-files-body`);
    if (!entries.length) {
      const columns = scope === "remote" ? 6 : 5;
      body.innerHTML = `<tr><td colspan="${columns}" class="empty-cell">当前目录没有可显示的文件</td></tr>`;
      return;
    }
    if (scope === "remote") {
      body.innerHTML = entries.map(item => {
        if (item.type === "directory") return `<tr class="directory-row"><td><button class="folder-link" data-path="${escapeHtml(item.path)}"><span class="file-icon folder">${icon("folder")}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${Number(item.entry_count || 0)} 个对象</small></span><span class="file-chevron">${icon("chevron")}</span></button></td><td>文件夹</td><td>--</td><td>--</td><td>--</td><td>--</td></tr>`;
        const local = localObjectStatus[item.local_state] || [item.local_state || "未知", ""];
        const type = [item.kind, item.table_name].filter(Boolean).join(" · ") || "归档对象";
        return `<tr><td><div class="file-entry"><span class="file-icon file">${icon("file")}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.path)}</small></span></div></td><td>${escapeHtml(type)}</td><td>${bytes(item.size_bytes)}</td><td>${Number(item.row_count || 0).toLocaleString("zh-CN")}</td><td><span class="state-pill ${local[1]}">${escapeHtml(local[0])}</span></td><td class="hash" title="${escapeHtml(item.sha256)}">${escapeHtml(String(item.sha256 || "").slice(0, 12))}</td></tr>`;
      }).join("");
    } else {
      body.innerHTML = entries.map(item => {
        if (item.type === "directory") return `<tr class="directory-row"><td><button class="folder-link" data-path="${escapeHtml(item.path)}"><span class="file-icon folder">${icon("folder")}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml((item.locations || [item.location]).join(" + "))}</small></span><span class="file-chevron">${icon("chevron")}</span></button></td><td>${escapeHtml((item.locations || [item.location]).join(" + "))}</td><td>--</td><td>--</td><td>--</td></tr>`;
        const location = item.state === "downloading" ? ["下载中", "warn"] : item.location === "verified" ? ["已验证目录", "good"] : ["暂存目录", "warn"];
        const remote = item.remote_state === "listed" ? ["已列入", "good"] : item.remote_state === "control" ? ["控制文件", ""] : ["仅本地", "warn"];
        return `<tr><td><div class="file-entry"><span class="file-icon file">${icon("file")}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.path)}</small></span></div></td><td><span class="state-pill ${location[1]}">${location[0]}</span></td><td>${bytes(item.size_bytes)}</td><td><span class="state-pill ${remote[1]}">${remote[0]}</span></td><td>${timeText(item.modified_at)}</td></tr>`;
      }).join("");
    }
    $$(".folder-link", body).forEach(button => button.addEventListener("click", () => loadFileList(scope, button.dataset.path || "")));
  }

  function renderFileBreadcrumbs(scope, path, parentPath) {
    const root = $(`#${scope}-breadcrumbs`);
    const parts = path ? path.split("/") : [];
    const crumbs = [`<button class="breadcrumb root-breadcrumb" data-path="">${icon("home")}<span>归档根目录</span></button>`];
    let current = "";
    parts.forEach(part => {
      current = current ? `${current}/${part}` : part;
      crumbs.push(`<span class="breadcrumb-separator">/</span><button class="breadcrumb" data-path="${escapeHtml(current)}">${escapeHtml(part)}</button>`);
    });
    root.innerHTML = crumbs.join("");
    $$(".breadcrumb", root).forEach(button => button.addEventListener("click", () => loadFileList(scope, button.dataset.path || "")));
    if (path && parentPath === path) root.dataset.invalid = "true";
  }

  function profileTemplate(profile, index) {
    const sourceType = profile.source_type || "google_drive";
    return `<article class="profile-edit" data-index="${index}">
      <div class="profile-edit-head"><strong>${escapeHtml(profile.display_name || "新采集服务器")}</strong><button type="button" class="button small danger remove-profile">删除</button></div>
      <div class="profile-fields">
        <label>配置 ID<input data-field="profile_id" value="${escapeHtml(profile.profile_id)}" required></label>
        <label>显示名称<input data-field="display_name" value="${escapeHtml(profile.display_name)}" required></label>
        <label>Collector ID<input data-field="collector_id" value="${escapeHtml(profile.collector_id)}" required></label>
        <label>来源<select data-field="source_type"><option value="google_drive" ${sourceType === "google_drive" ? "selected" : ""}>Google Drive</option><option value="ubuntu_sftp" ${sourceType === "ubuntu_sftp" ? "selected" : ""}>Ubuntu 内网（SFTP）</option><option value="verified_directory" ${sourceType === "verified_directory" ? "selected" : ""}>已验证目录</option></select></label>
        <label class="drive-field">rclone remote<input data-field="drive_remote" value="${escapeHtml(profile.drive_remote || "gdrive:")}"></label>
        <label class="drive-field wide-field">网盘前缀<input data-field="drive_prefix" value="${escapeHtml(profile.drive_prefix || "smsi/v3")}"></label>
        <label class="sftp-field">Ubuntu 主机<input data-field="sftp_host" value="${escapeHtml(profile.sftp_host || "")}" placeholder="192.168.2.240"></label>
        <label class="sftp-field">SSH 端口<input data-field="sftp_port" type="number" min="1" max="65535" value="${Number(profile.sftp_port || 22)}"></label>
        <label class="sftp-field">只读用户<input data-field="sftp_user" value="${escapeHtml(profile.sftp_user || "smsi-archive-reader")}"></label>
        <label class="sftp-field">SFTP 根目录<input data-field="sftp_root" value="${escapeHtml(profile.sftp_root || "/archive")}"></label>
        <label class="sftp-field wide-field">SSH 私钥文件<input data-field="sftp_key_file" value="${escapeHtml(profile.sftp_key_file || "")}"></label>
        <label class="sftp-field wide-field">known_hosts 文件<input data-field="sftp_known_hosts_file" value="${escapeHtml(profile.sftp_known_hosts_file || "")}"></label>
        <label class="directory-field wide-field">来源根目录<input data-field="verified_source_root" value="${escapeHtml(profile.verified_source_root || "")}"></label>
        <label class="switch-row"><input data-field="enabled" type="checkbox" ${profile.enabled !== false ? "checked" : ""}><span class="switch"></span><span>启用</span></label>
      </div>
    </article>`;
  }

  function renderProfileEditor() {
    const root = $("#profile-editor");
    const profiles = state.config?.profiles || [];
    root.innerHTML = profiles.length ? profiles.map(profileTemplate).join("") : '<p class="empty-state">尚未添加配置</p>';
    $$(".profile-edit", root).forEach(row => {
      $(".remove-profile", row).addEventListener("click", () => {
        state.config.profiles.splice(Number(row.dataset.index), 1); renderProfileEditor();
      });
      const select = $('[data-field="source_type"]', row);
      const updateFields = () => {
        $$(".drive-field", row).forEach(item => item.classList.toggle("hidden", select.value !== "google_drive"));
        $$(".sftp-field", row).forEach(item => item.classList.toggle("hidden", select.value !== "ubuntu_sftp"));
        $$(".directory-field", row).forEach(item => item.classList.toggle("hidden", select.value !== "verified_directory"));
      };
      select.addEventListener("change", updateFields);
      updateFields();
    });
  }

  function populateSettings() {
    const form = $("#settings-form");
    const config = state.config;
    for (const field of ["local_root", "poll_minutes", "history_days", "download_workers", "bandwidth_limit", "rclone_binary", "web_host", "web_port"]) {
      const input = form.elements[field]; if (input) input.value = config[field] ?? "";
    }
    form.elements.minimum_free_gib.value = Math.round(Number(config.minimum_free_bytes) / 1073741824);
    form.elements.auto_download.checked = !!config.auto_download;
    renderProfileEditor();
  }

  function collectProfiles() {
    return $$(".profile-edit", $("#profile-editor")).map(row => {
      const get = name => $(`[data-field="${name}"]`, row);
      return {
        profile_id: get("profile_id").value.trim(), display_name: get("display_name").value.trim(), collector_id: get("collector_id").value.trim(),
        source_type: get("source_type").value, drive_remote: get("drive_remote").value.trim(), drive_prefix: get("drive_prefix").value.trim(),
        verified_source_root: get("verified_source_root").value.trim(), sftp_host: get("sftp_host").value.trim(),
        sftp_port: Number(get("sftp_port").value), sftp_user: get("sftp_user").value.trim(),
        sftp_key_file: get("sftp_key_file").value.trim(), sftp_known_hosts_file: get("sftp_known_hosts_file").value.trim(),
        sftp_root: get("sftp_root").value.trim(), enabled: get("enabled").checked,
      };
    });
  }

  async function saveSettings() {
    const form = $("#settings-form");
    if (!form.reportValidity()) return;
    const payload = {
      local_root: form.elements.local_root.value.trim(), rclone_binary: form.elements.rclone_binary.value.trim(),
      poll_minutes: Number(form.elements.poll_minutes.value), history_days: Number(form.elements.history_days.value),
      download_workers: Number(form.elements.download_workers.value), bandwidth_limit: form.elements.bandwidth_limit.value.trim(),
      minimum_free_bytes: Number(form.elements.minimum_free_gib.value) * 1073741824, auto_download: form.elements.auto_download.checked,
      web_host: form.elements.web_host.value.trim(), web_port: Number(form.elements.web_port.value), profiles: collectProfiles(),
    };
    try {
      const result = await api("/api/config", { method: "PUT", body: JSON.stringify(payload) });
      state.config = result.config;
      populateSettings();
      renderFileProfileOptions();
      state.fileBrowsers.remote.request += 1;
      state.fileBrowsers.local.request += 1;
      toast("设置已保存");
    }
    catch (error) { toast(error.message, true); }
  }

  async function runScan(download) {
    try { await api("/api/actions/scan", { method: "POST", body: JSON.stringify({ download }) }); toast(download ? "同步任务已加入队列" : "检查任务已加入队列"); await refresh(); }
    catch (error) { toast(error.message, true); }
  }

  async function verifyDay(profile_id, archive_date) {
    try { await api("/api/actions/verify", { method: "POST", body: JSON.stringify({ profile_id, archive_date }) }); toast(`${archive_date} 已加入重新校验队列`); await refresh(); }
    catch (error) { toast(error.message, true); }
  }

  async function refresh() {
    try {
      const result = await api("/api/status");
      state.runtime = result.runtime; state.updates = result.updates; state.days = result.days; state.events = result.events; renderAll(); renderUpdates();
    } catch (error) {
      $("#connection-state").textContent = "连接中断"; $("#connection-state").className = "status-dot bad";
    }
  }

  async function bootstrap() {
    const result = await api("/api/bootstrap");
    state.csrf = result.csrf; state.config = result.config; state.runtime = result.runtime; state.updates = result.updates; state.days = result.days; state.events = result.events;
    renderAll(); renderUpdates(); populateSettings(); renderFileProfileOptions();
    if (result.initial_password_pending) toast("当前仍在使用初始密码，请在设置中更改");
    state.timer = setInterval(refresh, 5000);
  }

  $$(".nav-item").forEach(button => button.addEventListener("click", () => {
    $$(".nav-item").forEach(item => item.classList.toggle("active", item === button));
    $$(".page").forEach(page => page.classList.toggle("active", page.id === `${button.dataset.page}-page`));
    if (button.dataset.page === "remote-files") loadFileDates("remote");
    if (button.dataset.page === "local-files") loadFileDates("local");
    if (button.dataset.page === "updates") pollUpdateStatus();
  }));
  for (const scope of ["remote", "local"]) {
    $(`#${scope}-profile`).addEventListener("change", () => loadFileDates(scope));
    $(`#${scope}-date`).addEventListener("change", () => {
      state.fileBrowsers[scope].path = "";
      loadFileList(scope, "");
    });
    $(`#${scope}-refresh`).addEventListener("click", () => loadFileDates(scope));
  }
  $("#scan-only").addEventListener("click", () => runScan(false));
  $("#scan-download").addEventListener("click", () => runScan(true));
  $("#cancel-task").addEventListener("click", async () => { try { await api("/api/actions/cancel", { method: "POST", body: "{}" }); toast("已请求取消任务"); } catch (error) { toast(error.message, true); } });
  $("#check-update").addEventListener("click", checkUpdate);
  $("#download-update").addEventListener("click", downloadUpdate);
  $("#restart-update").addEventListener("click", restartUpdate);
  $("#save-settings").addEventListener("click", saveSettings);
  $("#add-profile").addEventListener("click", () => {
    const used = new Set(state.config.profiles.map(item => item.profile_id)); let counter = 1; while (used.has(`collector-${counter}`)) counter += 1;
    state.config.profiles.push({ profile_id: `collector-${counter}`, display_name: `采集服务器 ${counter}`, collector_id: `collector-${counter}`, enabled: true, source_type: "google_drive", drive_remote: "gdrive:", drive_prefix: "smsi/v3", verified_source_root: "", sftp_host: "", sftp_port: 22, sftp_user: "smsi-archive-reader", sftp_key_file: "", sftp_known_hosts_file: "", sftp_root: "/archive" }); renderProfileEditor();
  });
  $("#password-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    try { await api("/api/password", { method: "PUT", body: JSON.stringify({ current_password: form.elements.current_password.value, new_password: form.elements.new_password.value }) }); form.reset(); toast("密码已更新"); }
    catch (error) { toast(error.message, true); }
  });
  $("#logout").addEventListener("click", async () => { try { await api("/logout", { method: "POST", body: "{}" }); location.href = "/login"; } catch (error) { toast(error.message, true); } });
  bootstrap().catch(error => toast(error.message, true));
})();
