(() => {
  "use strict";
  const state = {
    csrf: "", config: null, runtime: null, updates: null, days: [], jobs: [], comparisons: [], events: [],
    timer: null, updateTimer: null,
    fileBrowsers: {
      remote: { request: 0, loadedProfile: "", path: "", date: "", dates: [], index: null, meta: null, currentResult: null, query: "" },
      local: { request: 0, loadedProfile: "", path: "", date: "", dates: [], index: null, meta: null, currentResult: null, query: "" },
    },
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
  const icon = name => {
    const paths = {
      folder: '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"/><path d="M3 9h18"/>',
      file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h6"/>',
      database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
      report: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 17v-3M12 17v-6M16 17v-4"/>',
      control: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9 15 2 2 4-5"/>',
      calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
      chevron: '<path d="m9 18 6-6-6-6"/>',
      home: '<path d="m3 10 9-7 9 7"/><path d="M5 9v11h14V9M9 20v-6h6v6"/>',
    };
    return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || ""}</svg>`;
  };

  function entryVisual(item = {}) {
    if (item.type === "directory") return { iconName: "folder", className: "folder", label: "文件夹" };
    const name = String(item.name || "").toLowerCase();
    const kind = String(item.kind || "").toLowerCase();
    if (kind === "runtime_report" || name.includes("report")) return { iconName: "report", className: "report", label: "运行报告" };
    if ((kind && !["control", "evidence"].includes(kind)) || name.endsWith(".parquet")) return { iconName: "database", className: "file", label: "归档数据" };
    if (name.endsWith(".json") || name.endsWith(".sha256")) return { iconName: "control", className: "control", label: "控制文件" };
    return { iconName: "file", className: "file", label: "文件" };
  }
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
    verified: ["恢复验证通过", "good"], downloading: ["下载中", "warn"], verifying: ["校验中", "warn"],
    ready: ["可下载", "warn"], remote_running: ["远端归档中", "warn"], waiting_manifest: ["等待发布", "warn"],
    remote_failed: ["远端失败", "bad"], error: ["处理失败", "bad"], manifest_changed: ["清单已变化", "bad"],
    cancelled: ["已取消", "warn"], interrupted: ["等待恢复", "warn"],
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

  function activeJob() {
    return state.runtime?.current_job
      || state.jobs.find(item => ["queued", "running", "cancelling"].includes(item.status))
      || null;
  }

  function taskBusy() {
    return !!state.runtime?.running || !!state.runtime?.pending || !!activeJob();
  }

  function switchPage(pageName) {
    if (pageName === "files") pageName = "remote-files";
    const navPage = ["remote-files", "local-files"].includes(pageName) ? "files" : pageName;
    $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === navPage));
    $$(".page").forEach(page => page.classList.toggle("active", page.id === `${pageName}-page`));
    if (pageName === "jobs") renderJobs();
    if (pageName === "remote-files") loadFileDates("remote");
    if (pageName === "local-files") loadFileDates("local");
    if (pageName === "updates") pollUpdateStatus();
  }

  function renderMetrics() {
    const runtime = state.runtime || {};
    const currentJob = activeJob();
    const checking = runtime.running && ["discovering", "scanning"].includes(runtime.progress?.phase);
    const verified = state.days.filter(item => item.status === "verified");
    const bad = state.days.filter(item => ["error", "remote_failed", "manifest_changed"].includes(item.status));
    const pending = state.days.filter(item => !["verified", "error", "remote_failed", "manifest_changed"].includes(item.status));
    $("#metric-runtime").textContent = checking ? "检查中" : runtime.running ? "执行中" : currentJob ? "排队中" : (runtime.auto_download ? "自动运行" : "已暂停");
    $("#metric-runtime-detail").textContent = checking ? "正在发现新归档与异常状态" : runtime.running || currentJob ? (currentJob?.detail || runtime.detail) : `下次检查 ${timeText(runtime.next_scan_at)}`;
    $("#metric-verified").textContent = String(verified.length);
    $("#metric-latest").textContent = verified.length ? `最新 ${verified.map(item => item.archive_date).sort().at(-1)}` : "尚无本地归档";
    $("#metric-pending").textContent = String(pending.length + bad.length);
    $("#metric-errors").textContent = `待同步 ${pending.length} · 失败 ${bad.length}`;
    const disk = runtime.disk || {};
    const ratio = disk.total ? Math.round(Number(disk.used) / Number(disk.total) * 100) : 0;
    $("#metric-disk").textContent = runtime.disk_error ? "不可用" : `${ratio}%`;
    $("#metric-disk-detail").textContent = runtime.disk_error || `可用 ${bytes(disk.free)} / ${bytes(disk.total)}`;
    $("#sidebar-storage-label").textContent = runtime.disk_error ? "不可用" : `${ratio}%`;
    $("#sidebar-storage-detail").textContent = runtime.disk_error || `${bytes(disk.free)} 可用，共 ${bytes(disk.total)}`;
    const storageBar = $("#sidebar-storage-bar");
    storageBar.style.width = `${ratio}%`;
    storageBar.className = ratio >= 92 ? "bad" : ratio >= 80 ? "warn" : "";
    $("#connection-state").textContent = checking ? "状态检查中" : runtime.running ? "任务执行中" : "客户端在线";
    $("#connection-state").className = "status-dot good";
    $("#cancel-task").classList.toggle("hidden", !activeJob());
    $("#cancel-task").textContent = checking ? "停止检查" : "取消任务";
    $("#scan-only").disabled = taskBusy();
    $("#scan-download").disabled = taskBusy();
  }

  const jobStatusMap = {
    queued: ["排队中", "warn"], running: ["执行中", "warn"], cancelling: ["正在取消", "warn"],
    recovering: ["等待恢复", "warn"], completed: ["已完成", "good"], cancelled: ["已取消", "warn"],
    failed: ["失败", "bad"],
  };

  function jobLabel(job) {
    return ({ scan: "检查新归档", scan_download: "自动同步", download: "指定日期下载", verify: "重新校验" }[job?.action] || job?.action || "任务");
  }

  function progressValues(job, progress) {
    const value = progress && Object.keys(progress).length ? progress : job || {};
    const scanning = ["discovering", "scanning"].includes(value.phase);
    const total = Number(value.bytes_total || 0);
    const done = Number(value.bytes_transferred ?? value.bytes_done ?? 0);
    const objects = Number(value.object_count || 0);
    const objectsDone = Number(value.objects_done || 0);
    const scanTotal = Number(value.scan_dates_total || 0);
    const scanDone = Number(value.scan_dates_done || 0);
    const percent = scanning && scanTotal
      ? Math.max(0, Math.min(100, scanDone / scanTotal * 100))
      : total ? Math.max(0, Math.min(100, done / total * 100))
      : objects ? Math.max(0, Math.min(100, objectsDone / objects * 100)) : 0;
    return { value, total, done, objects, objectsDone, percent, scanning, scanTotal, scanDone };
  }

  function renderTaskPanel(prefix, job, progress) {
    const values = progressValues(job, progress);
    const live = values.value;
    const active = job && ["queued", "running", "cancelling"].includes(job.status);
    const [label, tone] = values.scanning
      ? ["检查中", "warn"]
      : job
      ? (job.phase === "recovering" ? jobStatusMap.recovering : jobStatusMap[job.status] || [job.status || "未知", ""])
      : ["空闲", ""];
    if (prefix === "overview-job") {
      $("#overview-job-title").textContent = values.scanning ? "归档状态检查" : job ? `${jobLabel(job)} · ${job.profile_id || "全部采集服务器"}` : "当前没有运行任务";
      $("#overview-job-detail").textContent = job
        ? (job.status === "cancelling" ? "正在停止任务，已完成对象会保留" : live.phase === "discovering" ? `正在读取远端归档日期清单${live.profile_id ? ` · ${live.profile_id}` : ""}` : values.scanning ? `正在检查远端归档日期与本地状态${live.profile_id ? ` · ${live.profile_id}` : ""}` : progress.phase === "downloading" ? "正在下载并校验对象" : job.detail || "等待后台任务")
        : "自动检查会在计划时间运行";
      $("#overview-job-state").textContent = label;
      $("#overview-job-state").className = `state-pill ${tone}`;
      $("#overview-job-object").textContent = values.scanning
        ? (live.phase === "discovering" ? "读取归档日期清单" : live.archive_date ? `检查 ${live.archive_date}` : "准备检查归档日期")
        : live.current_object ? live.current_object.split("/").at(-1) : (job?.archive_date || "等待任务");
      $("#overview-job-percent").textContent = job ? `${values.percent.toFixed(1)}%` : "--";
      $("#overview-job-progress").style.width = `${values.percent}%`;
      $("#overview-job-objects").textContent = values.scanning ? `日期 ${values.scanDone}/${values.scanTotal || "--"}` : `对象 ${values.objectsDone}/${values.objects || "--"}`;
      $("#overview-job-bytes").textContent = values.scanning ? "数据 无需传输" : `数据 ${values.total ? `${bytes(values.done)} / ${bytes(values.total)}` : "--"}`;
      $("#overview-job-speed").textContent = `速度 ${live.speed_bytes_per_second ? `${bytes(live.speed_bytes_per_second)}/秒` : "--"}`;
      $("#overview-job-eta").textContent = `剩余 ${live.eta_seconds != null ? durationText(live.eta_seconds) : "--"}`;
      $("#overview-job-concurrency").textContent = `并发 ${live.active_transfers != null ? `${live.active_transfers}/${live.download_workers || "--"}` : "--"}`;
      $("#overview-job-limit").textContent = `限速 ${live.bandwidth_limit || "不限速"}`;
      return;
    }
    $("#jobs-active-title").textContent = values.scanning ? "归档状态检查" : job ? `${jobLabel(job)} · ${job.profile_id || "全部采集服务器"}` : "当前没有运行任务";
    $("#jobs-active-detail").textContent = job
      ? (job.status === "cancelling" ? "正在停止任务，已完成对象会保留" : live.phase === "discovering" ? `正在读取 ${live.profile_id || "采集服务器"} 的远端日期清单` : values.scanning ? `正在核对 ${live.archive_date || live.profile_id || "归档日期"}` : live.current_object || job.detail || "等待后台任务")
      : "等待自动检查或手动操作";
    $("#jobs-active-state").textContent = label;
    $("#jobs-active-state").className = `state-pill ${tone}`;
    $("#jobs-current-object").textContent = values.scanning ? (live.archive_date || "读取日期清单") : live.current_object || job?.archive_date || "--";
    $("#jobs-progress-percent").textContent = job ? `${values.percent.toFixed(1)}%` : "0%";
    $("#jobs-progress-bar").style.width = `${values.percent}%`;
    $("#jobs-progress-objects").textContent = values.scanning ? `日期 ${values.scanDone}/${values.scanTotal || "--"}` : `对象 ${values.objectsDone}/${values.objects || "--"}`;
    $("#jobs-progress-bytes").textContent = values.scanning ? "数据 无需传输" : `数据 ${values.total ? `${bytes(values.done)} / ${bytes(values.total)}` : "--"}`;
    $("#jobs-progress-speed").textContent = `速度 ${live.speed_bytes_per_second ? `${bytes(live.speed_bytes_per_second)}/秒` : "--"}`;
    $("#jobs-progress-eta").textContent = `剩余 ${live.eta_seconds != null ? durationText(live.eta_seconds) : "--"}`;
    $("#jobs-progress-concurrency").textContent = `并发 ${live.active_transfers != null ? `${live.active_transfers}/${live.download_workers || "--"}` : "--"}`;
    $("#jobs-progress-limit").textContent = `限速 ${live.bandwidth_limit || "不限速"}`;
    $("#jobs-cancel-task").disabled = !active;
  }

  function renderJobs() {
    const active = activeJob();
    const progress = state.runtime?.running ? state.runtime?.progress || {} : {};
    renderTaskPanel("overview-job", active, progress);
    renderTaskPanel("jobs", active, progress);
    renderTransferDock(active, progress);
    $("#jobs-count").textContent = String(state.jobs.length);
    const body = $("#jobs-body");
    if (!state.jobs.length) { body.innerHTML = '<tr><td colspan="6" class="empty-cell">暂无任务</td></tr>'; return; }
    const profiles = Object.fromEntries((state.config?.profiles || []).map(item => [item.profile_id, item.display_name]));
    body.innerHTML = state.jobs.map(job => {
      const [label, tone] = job.phase === "recovering" ? jobStatusMap.recovering : jobStatusMap[job.status] || [job.status || "未知", ""];
      const values = progressValues(job, job.id === active?.id ? progress : {});
      const location = [profiles[job.profile_id] || job.profile_id, job.archive_date].filter(Boolean).join(" · ") || "全部采集服务器";
      const detail = job.error || job.detail || "";
      return `<tr><td><div class="job-cell"><strong>${escapeHtml(jobLabel(job))}</strong><small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small></div></td><td>${escapeHtml(location)}</td><td><span class="state-pill ${tone}">${escapeHtml(label)}</span></td><td>${values.objects ? `${values.objectsDone}/${values.objects}` : "--"}</td><td>${values.total ? `${bytes(values.done)} / ${bytes(values.total)}` : "--"}</td><td>${timeText(job.updated_at)}</td></tr>`;
    }).join("");
  }

  function renderTransferDock(job = activeJob(), progress = state.runtime?.running ? state.runtime?.progress || {} : {}) {
    const values = progressValues(job, progress);
    const live = values.value;
    const active = !!job && ["queued", "running", "cancelling"].includes(job.status);
    const [statusLabel] = values.scanning
      ? ["检查中"]
      : job
      ? (job.phase === "recovering" ? jobStatusMap.recovering : jobStatusMap[job.status] || [job.status || "未知", ""])
      : ["空闲", ""];
    $("#transfer-dock").classList.toggle("busy", active);
    $("#transfer-dock-title").textContent = values.scanning ? "正在检查归档状态" : job ? `${jobLabel(job)} · ${job.profile_id || "全部采集服务器"}` : "传输空闲";
    $("#transfer-dock-detail").textContent = job
      ? (values.scanning ? `${live.profile_id || "全部采集服务器"} · 日期 ${values.scanDone}/${values.scanTotal || "--"}` : live.current_object || job.archive_date || job.detail || "等待后台任务")
      : "等待自动检查或手动下载";
    $("#transfer-dock-state").textContent = statusLabel;
    $("#transfer-dock-percent").textContent = job ? `${values.percent.toFixed(1)}%` : "0%";
    $("#transfer-dock-progress").style.width = `${values.percent}%`;
    $("#transfer-dock-speed").textContent = `速度 ${live.speed_bytes_per_second ? `${bytes(live.speed_bytes_per_second)}/秒` : "--"}`;
    $("#transfer-dock-eta").textContent = `剩余 ${live.eta_seconds != null ? durationText(live.eta_seconds) : "--"}`;
    $("#transfer-cancel").classList.toggle("hidden", !active);
    $("#transfer-cancel").title = values.scanning ? "停止检查" : "取消任务";
    $("#nav-job-state").textContent = active ? (values.scanning ? `状态检查 ${values.percent.toFixed(0)}%` : `${statusLabel} ${values.percent.toFixed(0)}%`) : "当前空闲";
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
      const source = { google_drive: "Google Drive", verified_directory: "已验证目录" }[profile.source_type] || profile.source_type;
      return `<article class="profile-row"><div><h3>${escapeHtml(profile.display_name)}</h3><p>collector=${escapeHtml(profile.collector_id)}</p></div><span class="source-label">${source}</span><div class="profile-stats"><span>已验证 <strong>${verified}</strong></span><span>异常 <strong>${alerts}</strong></span><span>${profile.enabled ? "已启用" : "已停用"}</span></div></article>`;
    }).join("");
  }

  function reportView(report = {}) {
    const status = String(report.status || "");
    const historical = status && report.assessment_classification !== "current";
    const issueCount = Number(report.issue_count || 0);
    if (historical) {
      const engine = String(report.assessment_engine_version || "").replace("smsi-runtime-health-assessment/", "");
      return {
        text: `历史记录${engine ? ` ${engine}` : ""}${issueCount ? ` · ${issueCount} 项` : ""}`,
        tone: "",
        detail: `历史归档报告，原结论 ${status || "未知"}；不计入当前生产状态或归档异常。`,
      };
    }
    if (!status) return { text: "未生成", tone: "", detail: "" };
    const dataStatus = String(report.data_quality_status || "unknown");
    const operationCount = Number(report.operational_issue_count || 0);
    const observationCount = Number(report.observation_count || 0);
    const labels = {
      healthy: ["数据健康", "good"],
      attention: ["数据需关注", "warn"],
      critical: ["数据严重", "bad"],
      unknown: ["数据质量证据不足", "warn"],
    };
    const [label, tone] = labels[dataStatus] || labels.unknown;
    const suffix = dataStatus === "healthy"
      ? operationCount ? ` · ${operationCount} 项运行记录` : observationCount ? ` · ${observationCount} 项已恢复观察` : ""
      : ` · ${Number(report.data_quality_issue_count || 0)} 项`;
    return {
      text: `${label}${suffix}`,
      tone,
      detail: operationCount
        ? `当日数据质量 ${label}；另有 ${operationCount} 项运行过程记录，详见原始归档报告。`
        : "",
    };
  }

  function renderDays() {
    const body = $("#days-body");
    const profiles = Object.fromEntries((state.config?.profiles || []).map(item => [item.profile_id, item.display_name]));
    $("#day-summary").textContent = `${state.days.length} 条本地状态记录`;
    if (!state.days.length) { body.innerHTML = '<tr><td colspan="8" class="empty-cell">暂无记录</td></tr>'; return; }
    body.innerHTML = state.days.map(item => {
      const live = state.runtime?.progress;
      const liveMatches = live && live.profile_id === item.profile_id && live.archive_date === item.archive_date && ["downloading", "verifying"].includes(live.phase);
      const objectsDone = liveMatches ? Number(live.objects_done || 0) : Number(item.objects_done || 0);
      const objectCount = liveMatches ? Number(live.object_count || item.object_count || 0) : Number(item.object_count || 0);
      const bytesDone = liveMatches ? Number(live.bytes_transferred || live.bytes_done || 0) : Number(item.bytes_done || 0);
      const bytesTotal = liveMatches ? Number(live.bytes_total || item.bytes_total || 0) : Number(item.bytes_total || 0);
      const [label, tone] = statusMap[item.status] || [item.status, ""];
      const report = item.report_summary || {};
      const reportState = reportView(report);
      const liveDetail = liveMatches ? [
        live.current_object ? `当前 ${live.current_object.split("/").at(-1)}` : "",
        live.speed_bytes_per_second ? `${bytes(live.speed_bytes_per_second)}/秒` : "",
        live.eta_seconds != null ? `剩余约 ${durationText(live.eta_seconds)}` : "",
      ].filter(Boolean).join(" · ") : "";
      const detailText = liveDetail || item.error || item.detail || "";
      const dataText = objectCount ? `${bytesDone ? bytes(bytesDone) : "0 B"} / ${bytes(bytesTotal)}` : bytes(bytesTotal);
      const statusDetail = liveDetail ? `<small class="row-progress-detail">${escapeHtml(liveDetail)}</small>` : "";
      const action = `${item.status === "verified" ? `<button class="button small secondary verify-day" data-profile="${escapeHtml(item.profile_id)}" data-date="${escapeHtml(item.archive_date)}" ${taskBusy() ? "disabled" : ""}>重新校验</button>` : ""}<button class="button small quiet day-detail-toggle" data-profile="${escapeHtml(item.profile_id)}" data-date="${escapeHtml(item.archive_date)}">文件详情</button>`;
      const reportTitle = [reportState.detail, (report.top_issues || []).map(issue => issue.title || issue.code).join("；")].filter(Boolean).join(" ");
      return `<tr title="${escapeHtml(detailText)}"><td>${escapeHtml(item.archive_date)}</td><td>${escapeHtml(profiles[item.profile_id] || item.profile_id)}</td><td><span class="state-pill ${tone}">${escapeHtml(label)}</span>${statusDetail}</td><td><span class="state-pill ${reportState.tone}" title="${escapeHtml(reportTitle)}">${escapeHtml(reportState.text)}</span></td><td>${objectsDone}/${objectCount}</td><td>${dataText}</td><td>${timeText(item.updated_at)}</td><td class="row-actions">${action}</td></tr><tr class="day-detail-row hidden"><td colspan="8"><div class="day-detail-content"></div></td></tr>`;
    }).join("");
    $$(".verify-day", body).forEach(button => button.addEventListener("click", () => verifyDay(button.dataset.profile, button.dataset.date)));
    $$(".day-detail-toggle", body).forEach(button => button.addEventListener("click", () => toggleDayDetail(button)));
  }

  function renderComparisons() {
    const body = $("#comparisons-body");
    const profiles = Object.fromEntries((state.config?.profiles || []).map(item => [item.profile_id, item.display_name]));
    const dataStatus = {
      healthy: ["一致", "good"], attention: ["差异较大", "warn"],
      critical: ["校验异常", "bad"], unknown: ["证据不足", ""],
    };
    $("#comparison-summary").textContent = state.comparisons.length
      ? `${state.comparisons.length} 个日期对比结果；只使用已完成恢复验证的数据`
      : "仅比较两侧均已完成恢复验证的日期";
    if (!state.comparisons.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty-cell">两台服务器完成同一日期的下载与恢复验证后自动生成</td></tr>';
      return;
    }
    body.innerHTML = state.comparisons.map(item => {
      const left = profiles[item.left_profile_id] || item.left_profile_id;
      const right = profiles[item.right_profile_id] || item.right_profile_id;
      const records = item.record_count || {};
      const difference = Number(item.record_relative_difference || 0);
      const dataIssues = item.data_issues || (item.issues || []).filter(issue =>
        String(issue.code || "").startsWith("collector_identity") ||
        ["business_inventory_mismatch", "record_volume_difference"].includes(issue.code)
      );
      const effectiveDataStatus = item.data_status || (dataIssues.length
        ? dataIssues.map(issue => issue.severity).sort((a, b) => ["critical", "attention", "unknown", "healthy"].indexOf(a) - ["critical", "attention", "unknown", "healthy"].indexOf(b))[0]
        : "healthy");
      const absoluteDifference = Number(item.record_difference ?? Math.abs(Number(records.left || 0) - Number(records.right || 0)));
      const [baseLabel, tone] = dataStatus[effectiveDataStatus] || [effectiveDataStatus || "未知", ""];
      const observationLabels = {
        object_checksum_difference: "校验和",
        object_row_count_difference: "对象行数",
        object_size_difference: "对象大小",
      };
      const observations = item.observed_differences || [];
      const observationText = observations.map(observation =>
        `${observationLabels[observation.code] || observation.code} ${Number(observation.count || 0)} 项`
      ).join("、");
      const label = effectiveDataStatus === "healthy" && (absoluteDifference > 0 || observations.length)
        ? "正常差异" : baseLabel;
      const percent = difference * 100;
      const percentText = percent === 0 ? "0%" : percent < 0.1 ? `${percent.toFixed(3)}%` : percent < 1 ? `${percent.toFixed(2)}%` : `${percent.toFixed(1)}%`;
      const issueText = dataIssues.map(issue => issue.detail || issue.code).filter(Boolean).join("；");
      const restore = item.restore_verification || {};
      const restorePassed = restore.left === "verified" && restore.right === "verified";
      const summaries = item.report_summary || {};
      const reportStatuses = item.report_status || {};
      const leftReport = reportView(summaries.left || {
        status: reportStatuses.left || "",
        assessment_classification: reportStatuses.left ? "historical" : "",
      });
      const rightReport = reportView(summaries.right || {
        status: reportStatuses.right || "",
        assessment_classification: reportStatuses.right ? "historical" : "",
      });
      const sameReport = leftReport.text === rightReport.text;
      const reportText = sameReport ? `两侧${leftReport.text}` : `${leftReport.text} / ${rightReport.text}`;
      const reportTone = leftReport.tone === "bad" || rightReport.tone === "bad"
        ? "bad" : leftReport.tone === "warn" || rightReport.tone === "warn" ? "warn" : leftReport.tone || rightReport.tone;
      const reportIssues = item.report_issues || (item.issues || []).filter(issue =>
        String(issue.code || "").startsWith("report_data_quality:") ||
        String(issue.code || "").startsWith("source_health:") ||
        issue.code === "quality_policy_mismatch"
      );
      const reportIssueText = reportIssues.map(issue => issue.detail || issue.code).filter(Boolean).join("；");
      const comparisonText = `相差 ${absoluteDifference.toLocaleString("zh-CN")} 行 · ${percentText}${observationText ? ` · ${observationText}` : ""}${issueText ? ` · ${issueText}` : ""}`;
      const comparisonTitle = [observationText, issueText].filter(Boolean).join("；");
      return `<tr><td>${escapeHtml(item.archive_date)}</td><td><span class="state-pill ${tone}">${escapeHtml(label)}</span></td><td>${escapeHtml(left)} / ${escapeHtml(right)}</td><td><span class="state-pill ${restorePassed ? "good" : ""}">${restorePassed ? "两侧通过" : "证据不足"}</span></td><td>${Number(records.left || 0).toLocaleString("zh-CN")} / ${Number(records.right || 0).toLocaleString("zh-CN")}</td><td title="${escapeHtml(reportIssueText)}"><span class="state-pill ${reportTone}">${escapeHtml(reportText)}</span></td><td title="${escapeHtml(comparisonTitle)}">${escapeHtml(comparisonText)}</td></tr>`;
    }).join("");
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
      const report = detail.report_summary || {};
      const reportState = reportView(report);
      const sourceCounts = report.source_counts || {};
      const reportLine = report.status
        ? `归档日数据质量：${reportState.text} · 来源健康 ${Number(sourceCounts.healthy || 0)} / 关注 ${Number(sourceCounts.attention || 0)} / 严重 ${Number(sourceCounts.critical || 0)} / 未知 ${Number(sourceCounts.unknown || 0)}`
        : "归档日数据质量：未生成";
      const issueLine = (report.top_issues || []).map(issue => `${issue.title || issue.code}${issue.action ? `；${issue.action}` : ""}`).join(" | ");
      const historicalLine = reportState.detail ? `<span class="detail-note">${escapeHtml(reportState.detail)}</span>` : "";
      const issueLabel = report.assessment_classification === "current" ? "报告记录" : "历史记录";
      content.innerHTML = `<div class="day-detail-summary"><span>远端：<strong class="state-text ${remoteState[1]}">${escapeHtml(remoteState[0])}</strong> · ${Number(detail.remote.object_count || 0)} 个对象 · ${bytes(detail.remote.bytes_total)}</span><span>本地：${Number(detail.local.object_count || 0)}/${Number(detail.remote.object_count || 0)} 个对象 · ${bytes(detail.local.bytes_done)} / ${bytes(detail.remote.bytes_total)}</span><span>${escapeHtml(reportLine)}</span>${historicalLine}${issueLine ? `<span class="detail-note">${issueLabel}：${escapeHtml(issueLine)}</span>` : ""}<span>${escapeHtml(detail.remote.detail || detail.local.detail || "")}</span></div>${detail.objects.length ? `<div class="table-wrap"><table class="object-table"><thead><tr><th>对象</th><th>远端</th><th>本地</th><th>大小</th><th>本地字节</th></tr></thead><tbody>${detail.objects.map(item => { const state = localObjectStatus[item.local_state] || [item.local_state, ""]; return `<tr><td title="${escapeHtml(item.relative_key)}">${escapeHtml(item.name)}</td><td><span class="state-pill good">已列入 manifest</span></td><td><span class="state-pill ${state[1]}">${escapeHtml(state[0])}</span></td><td>${bytes(item.size_bytes)}</td><td>${bytes(item.local_bytes)}</td></tr>`; }).join("")}</tbody></table></div>` : '<p class="empty-cell">远端尚未发布可读取的 manifest。</p>'}`;
      detailRow.dataset.loaded = "true";
    } catch (error) { content.innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`; }
  }

  function renderEvents() {
    const root = $("#event-list");
    if (!state.events.length) { root.innerHTML = '<p class="empty-cell">暂无记录</p>'; return; }
    root.innerHTML = state.events.map(item => `<article class="event-item ${escapeHtml(item.level)}"><header><strong>${escapeHtml(item.event)}</strong><time>${timeText(item.created_at)}</time></header><p>${escapeHtml([item.profile_id, item.archive_date, item.detail].filter(Boolean).join(" · ") || "--")}</p></article>`).join("");
  }

  function renderAll() {
    renderMetrics();
    renderProfiles();
    renderComparisons();
    renderDays();
    renderEvents();
    renderJobs();
    updateRemoteDownloadAction();
  }

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
      status = archiveBusy ? `版本 ${staged} 已准备；重启时会安全暂停当前任务，启动后继续。` : `版本 ${staged} 已准备，可以重启客户端。`;
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
    $("#restart-update").disabled = active || !updates.helper_available;
    const blockedReason = $("#update-blocked-reason");
    if (archiveBusy) {
      const progress = state.runtime?.progress || {};
      const archivePhase = archivePhaseMap[progress.phase] || "归档任务正在运行";
      const objects = progress.object_count ? ` · ${Number(progress.objects_done || 0)}/${Number(progress.object_count)} 个对象` : "";
      blockedReason.textContent = `${archivePhase}${objects}；重启会安全暂停当前任务，已完成对象和临时文件会保留，启动后继续。`;
      blockedReason.className = "workflow-notice warn";
      $("#restart-update").title = "安全暂停当前任务并重启客户端";
    } else if (staged && !active && updates.helper_available) {
      blockedReason.textContent = "更新包已下载并校验，可以重启客户端。";
      blockedReason.className = "workflow-notice good";
      $("#restart-update").title = "切换已校验的更新包并重启客户端";
    } else if (!updates.helper_available) {
      blockedReason.textContent = "更新助手不可用，暂时不能切换版本。";
      blockedReason.className = "workflow-notice bad";
      $("#restart-update").title = "更新助手不可用";
    } else {
      blockedReason.textContent = "客户端当前空闲，可以按需重启当前版本。";
      blockedReason.className = "workflow-notice";
      $("#restart-update").title = "重启当前运行版本";
    }
    $("#restart-hint").textContent = !updates.helper_available
      ? "Ubuntu 更新助手不可用"
      : archiveBusy
        ? "可重启，当前任务随后恢复"
        : staged ? "更新已准备，可以安全切换" : "空闲时可按需重启";

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
      toast("更新已准备，可以重启客户端");
    } catch (error) { toast(error.message, true); }
    finally { stopUpdatePolling(); await pollUpdateStatus(); }
  }

  async function restartUpdate() {
    const button = $("#restart-update");
    const targetRevision = state.updates?.staged_revision || state.updates?.current_revision || "";
    const activatesUpdate = !!state.updates?.staged_revision;
    const archiveBusy = !!state.runtime?.running;
    button.disabled = true;
    $("#update-detail").textContent = archiveBusy
      ? "正在安全暂停当前归档任务；随后重启客户端并自动恢复任务..."
      : activatesUpdate
        ? "正在切换更新并重启客户端，等待新版本上线..."
        : "正在重启客户端，等待服务恢复...";
    $("#update-detail").className = "update-detail";
    try {
      await api("/api/update/restart", { method: "POST", body: "{}" });
    } catch (error) {
      const blocked = /未能在 30 秒内安全暂停|归档任务正在运行|更新助手未安装|更新助手拒绝|无法连接更新助手/.test(error.message);
      if (blocked) {
        toast(error.message, true);
        button.disabled = false;
        await pollUpdateStatus();
        return;
      }
      // 服务重启时连接可能先断开，继续用版本标记确认是否已经切换。
    }
    toast("客户端正在重启，页面会自动刷新");
    waitForClientRevision(targetRevision, 0);
  }

  function waitForClientRevision(targetRevision, attempt) {
    setTimeout(async () => {
      try {
        const response = await fetch("/api/update/status", { credentials: "same-origin", cache: "no-store" });
        if (response.ok) {
          const payload = await response.json();
          if (!targetRevision || payload.updates?.current_revision === targetRevision) {
            window.location.reload();
            return;
          }
        }
      } catch (error) {
        // 服务切换期间短暂断开是预期的，继续等待恢复。
      }
      if (attempt < 20) {
        waitForClientRevision(targetRevision, attempt + 1);
      } else {
        toast("重启结果暂时无法确认，请手动刷新页面", true);
        $("#restart-update").disabled = false;
        await pollUpdateStatus();
      }
    }, 1000);
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
    state.fileBrowsers[scope].date = "";
    state.fileBrowsers[scope].dates = [];
    state.fileBrowsers[scope].index = null;
    state.fileBrowsers[scope].meta = null;
    state.fileBrowsers[scope].currentResult = null;
    state.fileBrowsers[scope].query = "";
    $(`#${scope}-up`).disabled = true;
    $(`#${scope}-search`).value = "";
    $(`#${scope}-search`).disabled = true;
    $(`#${scope}-tree-count`).textContent = "--";
    $(`#${scope}-tree`).innerHTML = `<p class="tree-empty">${escapeHtml(message)}</p>`;
    $(`#${scope}-inspector`).innerHTML = '<p class="inspector-empty">当前没有可显示的对象。</p>';
    $(`#${scope}-location-summary`).textContent = message;
    $(`#${scope}-summary`).textContent = message;
    if (scope === "remote") {
      $("#remote-download").disabled = true;
      $("#remote-gate").textContent = message;
      $("#remote-gate").className = "browser-gate";
    }
    const columns = scope === "remote" ? 6 : 5;
    $(`#${scope}-files-body`).innerHTML = `<tr><td colspan="${columns}" class="empty-cell">${escapeHtml(message)}</td></tr>`;
  }

  async function loadFileDates(scope, force = false) {
    const profileId = $(`#${scope}-profile`).value;
    if (!profileId) return clearFileBrowser(scope, "请先在设置中添加采集服务器");
    const browserState = state.fileBrowsers[scope];
    if (!force && browserState.loadedProfile === profileId && browserState.dates.length) {
      if (
        browserState.date
        && browserState.index
        && browserState.meta?.profile_id === profileId
        && browserState.meta?.archive_date === browserState.date
      ) {
        renderCachedIndex(scope, browserState.path || "");
      } else {
        renderDateList(scope, browserState.dates);
      }
      return;
    }
    const previousDate = force ? browserState.date : "";
    const previousPath = force ? browserState.path : "";
    const request = ++state.fileBrowsers[scope].request;
    const refreshButton = $(`#${scope}-refresh`);
    refreshButton.classList.add("loading");
    refreshButton.disabled = true;
    refreshButton.setAttribute("aria-busy", "true");
    browserState.loadedProfile = "";
    browserState.path = "";
    browserState.index = null;
    browserState.meta = null;
    clearFileBrowser(scope, scope === "remote" ? "正在读取网盘日期..." : "正在读取本地日期...");
    try {
      const query = new URLSearchParams({ profile_id: profileId, scope });
      const payload = await api(`/api/files/dates?${query}`);
      if (request !== state.fileBrowsers[scope].request) return;
      const dates = payload.result.dates || [];
      state.fileBrowsers[scope].dates = dates;
      state.fileBrowsers[scope].loadedProfile = profileId;
      if (!dates.length) return clearFileBrowser(scope, scope === "remote" ? "网盘中没有可浏览的归档日期" : "本地没有正式或暂存归档目录");
      if (previousDate && dates.some(item => item.archive_date === previousDate)) {
        state.fileBrowsers[scope].date = previousDate;
        await loadFileList(scope, previousPath);
        return;
      }
      renderDateList(scope, dates);
    } catch (error) {
      if (request !== state.fileBrowsers[scope].request) return;
      clearFileBrowser(scope, error.message);
    } finally {
      if (request === state.fileBrowsers[scope].request) {
        refreshButton.classList.remove("loading");
        refreshButton.disabled = false;
        refreshButton.removeAttribute("aria-busy");
      }
    }
  }

  async function loadFileList(scope, requestedPath = null) {
    const profileId = $(`#${scope}-profile`).value;
    const archiveDate = state.fileBrowsers[scope].date;
    if (!profileId || !archiveDate) return;
    const path = requestedPath === null ? state.fileBrowsers[scope].path : requestedPath;
    state.fileBrowsers[scope].path = path || "";
    if (
      requestedPath !== null
      && state.fileBrowsers[scope].index
      && state.fileBrowsers[scope].meta?.profile_id === profileId
      && state.fileBrowsers[scope].meta?.archive_date === archiveDate
    ) {
      renderCachedIndex(scope, path || "");
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
      if (payload.result.browse_index) {
        state.fileBrowsers[scope].index = payload.result.browse_index;
        state.fileBrowsers[scope].meta = {
          profile_id: profileId,
          archive_date: archiveDate,
          state: payload.result.state,
          detail: payload.result.detail,
          download_eligible: !!payload.result.download_eligible,
          download_block_reason: payload.result.download_block_reason || "",
          manifest_sha256: payload.result.manifest_sha256 || "",
        };
      }
      renderFileList(scope, payload.result);
    } catch (error) {
      if (request !== state.fileBrowsers[scope].request) return;
      body.innerHTML = `<tr><td colspan="${columns}" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
      $(`#${scope}-summary`).textContent = error.message;
    }
  }

  function renderDateList(scope, dates) {
    const browserState = state.fileBrowsers[scope];
    browserState.date = "";
    browserState.path = "";
    browserState.currentResult = null;
    browserState.query = "";
    $(`#${scope}-search`).value = "";
    $(`#${scope}-search`).disabled = true;
    if (scope === "remote") {
      $("#remote-download").disabled = true;
      $("#remote-gate").textContent = "选择日期后检查下载资格。";
      $("#remote-gate").className = "browser-gate";
    }
    renderFileBreadcrumbs(scope, "", "", "");
    $(`#${scope}-summary`).innerHTML = `<span><strong>归档根目录</strong></span><span>${dates.length} 个日期</span><span>按日期组织</span>`;
    renderDirectoryTree(scope);
    renderInspector(scope);
    renderLocationSummary(scope);
    const body = $(`#${scope}-files-body`);
    const columns = scope === "remote" ? 6 : 5;
    if (!dates.length) {
      body.innerHTML = `<tr><td colspan="${columns}" class="empty-cell">没有可显示的归档日期</td></tr>`;
      return;
    }
    if (scope === "remote") {
      body.innerHTML = dates.map(item => {
        const stateInfo = item.status === "unknown" && item.remote
          ? ["待检查", ""]
          : statusMap[item.status] || [item.remote ? "可读取" : "未知", item.remote ? "good" : ""];
        const localInfo = item.local ? ["本地已存在", "good"] : item.partial ? ["暂存中", "warn"] : ["未下载", ""];
        return `<tr class="directory-row"><td><button class="date-link folder-link" data-date="${escapeHtml(item.archive_date)}"><span class="file-icon folder">${icon("folder")}</span><span class="file-entry-label"><strong>${escapeHtml(item.archive_date)}</strong><small>归档日期</small></span><span class="file-chevron">${icon("chevron")}</span></button></td><td>归档日期</td><td>${bytes(item.bytes_total)}</td><td>${Number(item.row_count || 0).toLocaleString("zh-CN")}</td><td><span class="state-pill ${localInfo[1]}">${localInfo[0]}</span></td><td><span class="state-pill ${stateInfo[1]}">${stateInfo[0]}</span></td></tr>`;
      }).join("");
    } else {
      body.innerHTML = dates.map(item => {
        const location = item.local && item.partial ? "已验证 + 暂存" : item.local ? "已验证目录" : "暂存目录";
        const stateInfo = statusMap[item.status] || [item.status || "未知", ""];
        return `<tr class="directory-row"><td><button class="date-link folder-link" data-date="${escapeHtml(item.archive_date)}"><span class="file-icon folder">${icon("folder")}</span><span class="file-entry-label"><strong>${escapeHtml(item.archive_date)}</strong><small>归档日期</small></span><span class="file-chevron">${icon("chevron")}</span></button></td><td>${location}</td><td>${bytes(item.bytes_total)}</td><td><span class="state-pill ${stateInfo[1]}">${stateInfo[0]}</span></td><td>${timeText(item.updated_at)}</td></tr>`;
      }).join("");
    }
    $$(".date-link", body).forEach(button => button.addEventListener("click", () => selectFileDate(scope, button.dataset.date || "")));
  }

  function renderLocationSummary(scope) {
    const browserState = state.fileBrowsers[scope];
    const profileId = $(`#${scope}-profile`).value;
    const profile = (state.config?.profiles || []).find(item => item.profile_id === profileId);
    const location = browserState.date
      ? [browserState.date, browserState.path].filter(Boolean).join(" / ")
      : `${browserState.dates.length} 个归档日期`;
    $(`#${scope}-location-summary`).textContent = `${profile?.display_name || profileId || "未配置采集服务器"} · ${location}`;
  }

  function renderDirectoryTree(scope) {
    const browserState = state.fileBrowsers[scope];
    const root = $(`#${scope}-tree`);
    if (!browserState.date) {
      $(`#${scope}-tree-count`).textContent = String(browserState.dates.length);
      root.innerHTML = `<button class="tree-item root active" data-tree-root="true" style="--tree-depth:0">${icon("home")}<span>全部归档</span></button>${browserState.dates.map(item => `<button class="tree-item" data-tree-date="${escapeHtml(item.archive_date)}" style="--tree-depth:1">${icon("calendar")}<span>${escapeHtml(item.archive_date)}</span></button>`).join("")}`;
    } else {
      const paths = new Set();
      for (const item of browserState.index || []) {
        const parts = String(item.path || "").split("/").filter(Boolean);
        parts.slice(0, -1).forEach((_part, index) => paths.add(parts.slice(0, index + 1).join("/")));
      }
      const directories = [...paths].sort((left, right) => left.localeCompare(right));
      $(`#${scope}-tree-count`).textContent = String(directories.length + 1);
      root.innerHTML = `<button class="tree-item root" data-tree-root="true" style="--tree-depth:0">${icon("home")}<span>全部归档</span></button><button class="tree-item ${browserState.path ? "" : "active"}" data-tree-path="" style="--tree-depth:1">${icon("calendar")}<span>${escapeHtml(browserState.date)}</span></button>${directories.map(path => { const parts = path.split("/"); const active = browserState.path === path ? "active" : ""; return `<button class="tree-item ${active}" data-tree-path="${escapeHtml(path)}" style="--tree-depth:${Math.min(parts.length + 1, 8)}">${icon("folder")}<span title="${escapeHtml(path)}">${escapeHtml(parts.at(-1))}</span></button>`; }).join("")}`;
    }
    $$("[data-tree-root]", root).forEach(button => button.addEventListener("click", () => renderDateList(scope, browserState.dates)));
    $$("[data-tree-date]", root).forEach(button => button.addEventListener("click", () => selectFileDate(scope, button.dataset.treeDate || "")));
    $$("[data-tree-path]", root).forEach(button => button.addEventListener("click", () => renderCachedIndex(scope, button.dataset.treePath || "")));
  }

  function inspectorFields(fields) {
    return `<dl class="inspector-fields">${fields.filter(item => item[1] !== "" && item[1] != null).map(([label, value, className = ""]) => `<div><dt>${escapeHtml(label)}</dt><dd class="${escapeHtml(className)}">${escapeHtml(value)}</dd></div>`).join("")}</dl>`;
  }

  function renderInspector(scope, item = null) {
    const browserState = state.fileBrowsers[scope];
    const result = browserState.currentResult;
    const root = $(`#${scope}-inspector`);
    if (!browserState.date) {
      root.innerHTML = `<div class="inspector-hero"><span class="file-icon folder">${icon("calendar")}</span><strong>全部归档</strong><small>${browserState.dates.length} 个日期</small></div>${inspectorFields([["位置", scope === "remote" ? "Google Drive" : "本地归档"], ["采集服务器", $(`#${scope}-profile`).selectedOptions[0]?.textContent || "--"]])}`;
      return;
    }
    if (!item) {
      const [statusLabel] = statusMap[result?.state] || [result?.state || "未知", ""];
      const title = browserState.path.split("/").filter(Boolean).at(-1) || browserState.date;
      root.innerHTML = `<div class="inspector-hero"><span class="file-icon folder">${icon(browserState.path ? "folder" : "calendar")}</span><strong>${escapeHtml(title)}</strong><small>${browserState.path ? "文件夹" : "归档日期"}</small></div>${inspectorFields([["路径", browserState.path || "归档根目录"], ["项目", `${Number(result?.entry_count || 0)} 项`], ["数据量", bytes(result?.bytes_total)], ["记录数", Number(result?.row_count || 0).toLocaleString("zh-CN")], ["状态", statusLabel], ["说明", result?.detail || ""]])}`;
      return;
    }
    const visual = entryVisual(item);
    const isDirectory = item.type === "directory";
    const local = localObjectStatus[item.local_state] || [item.local_state || "--", ""];
    const location = item.state === "downloading" ? "下载中" : item.location === "verified" ? "已验证目录" : item.location === "partial" ? "暂存目录" : (item.locations || []).join(" + ");
    const fields = isDirectory
      ? [["路径", item.path], ["内容", `${Number(item.entry_count || 0)} 个对象`], ["位置", (item.locations || []).join(" + ")]]
      : scope === "remote"
        ? [["路径", item.path], ["类型", [item.kind, item.table_name].filter(Boolean).join(" · ") || visual.label], ["大小", bytes(item.size_bytes)], ["记录数", Number(item.row_count || 0).toLocaleString("zh-CN")], ["本地状态", local[0]], ["SHA-256", item.sha256 || "--", "mono"]]
        : [["路径", item.path], ["类型", visual.label], ["位置", location || "本地"], ["大小", bytes(item.size_bytes)], ["网盘清单", item.remote_state === "listed" ? "已列入" : item.remote_state === "control" ? "控制文件" : "仅本地"], ["修改时间", timeText(item.modified_at)]];
    root.innerHTML = `<div class="inspector-hero"><span class="file-icon ${visual.className}">${icon(visual.iconName)}</span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(visual.label)}</small></div>${inspectorFields(fields)}`;
  }

  function navigateUp(scope) {
    const browserState = state.fileBrowsers[scope];
    if (!browserState.date) return;
    if (!browserState.path) {
      renderDateList(scope, browserState.dates);
      return;
    }
    const parent = browserState.path.split("/").slice(0, -1).join("/");
    renderCachedIndex(scope, parent);
  }

  function selectFileDate(scope, archiveDate) {
    state.fileBrowsers[scope].date = archiveDate;
    state.fileBrowsers[scope].path = "";
    loadFileList(scope, "");
  }

  function renderCachedIndex(scope, path) {
    const browserState = state.fileBrowsers[scope];
    const entries = {};
    for (const item of browserState.index || []) {
      const relative = String(item.path || "");
      if (path && !relative.startsWith(`${path}/`)) continue;
      const remainder = path ? relative.slice(path.length + 1) : relative;
      if (!remainder) continue;
      const [first, ...rest] = remainder.split("/");
      const childPath = path ? `${path}/${first}` : first;
      if (rest.length) {
        const entry = entries[childPath] || { type: "directory", path: childPath, name: first, entry_count: 0, locations: [] };
        entry.entry_count += 1;
        for (const location of item.locations || [item.location]) {
          if (location && !entry.locations.includes(location)) entry.locations.push(location);
        }
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
    renderFileList(scope, {
      scope,
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
      download_eligible: browserState.meta.download_eligible,
      download_block_reason: browserState.meta.download_block_reason,
    });
  }

  function updateRemoteDownloadAction(result = state.fileBrowsers.remote.meta) {
    const button = $("#remote-download");
    const gate = $("#remote-gate");
    if (!result || !state.fileBrowsers.remote.date) {
      button.disabled = true;
      return;
    }
    const busy = taskBusy();
    const profileId = $("#remote-profile").value;
    const archiveDate = state.fileBrowsers.remote.date;
    const day = state.days.find(item => item.profile_id === profileId && item.archive_date === archiveDate);
    const localVerified = day?.status === "verified";
    const localBlocked = ["manifest_changed", "remote_failed", "error"].includes(day?.status);
    const eligible = !!result.download_eligible && !busy && !localVerified && !localBlocked;
    let reason = result.download_block_reason
      || (result.state === "ready" ? "远端归档已验证，可以下载" : result.detail || "当前日期还不能下载");
    if (localBlocked) reason = day.error || day.detail || "当前归档状态异常，请先处理。";
    if (localVerified) reason = "本地归档已经完整验证。";
    if (busy) reason = "已有任务正在执行或排队，请等待完成后再下载。";
    gate.textContent = eligible ? "远端归档已验证，可以下载到 Ubuntu 本地归档。" : reason;
    gate.className = `browser-gate ${eligible ? "good" : localBlocked || ["remote_failed", "manifest_changed"].includes(result.state) ? "bad" : "warn"}`;
    button.disabled = !eligible;
  }

  function renderFileList(scope, result) {
    const browserState = state.fileBrowsers[scope];
    browserState.date = result.archive_date || browserState.date;
    browserState.path = result.path || "";
    browserState.currentResult = result;
    browserState.query = "";
    $(`#${scope}-search`).value = "";
    $(`#${scope}-search`).disabled = false;
    renderFileBreadcrumbs(scope, result.path || "", result.parent_path || "", result.archive_date || browserState.date);
    if (scope === "remote") {
      updateRemoteDownloadAction(result);
    }
    renderDirectoryTree(scope);
    renderLocationSummary(scope);
    renderInspector(scope);
    renderFileRows(scope);
  }

  function renderFileRows(scope) {
    const browserState = state.fileBrowsers[scope];
    const result = browserState.currentResult || {};
    const entries = result.entries || [];
    const query = browserState.query.trim().toLocaleLowerCase("zh-CN");
    const visible = entries.map((item, index) => ({ item, index })).filter(({ item }) => {
      if (!query) return true;
      return [item.name, item.path, item.kind, item.table_name, item.location, ...(item.locations || [])]
        .filter(Boolean).join(" ").toLocaleLowerCase("zh-CN").includes(query);
    });
    const [stateLabel, stateTone] = statusMap[result.state] || [result.state || "未知", ""];
    const matchText = query ? `<span><strong>${visible.length}</strong> / ${entries.length} 项匹配</span>` : `<span>${Number(result.entry_count || entries.length)} 项</span>`;
    $(`#${scope}-summary`).innerHTML = `<span><strong>${escapeHtml(result.archive_date || browserState.date)}</strong></span>${matchText}<span>${bytes(result.bytes_total)}</span>${Number(result.row_count || 0) ? `<span>${Number(result.row_count).toLocaleString("zh-CN")} 行</span>` : ""}<span class="state-text ${stateTone}">${escapeHtml(stateLabel)}</span>`;
    const body = $(`#${scope}-files-body`);
    if (!visible.length) {
      const columns = scope === "remote" ? 6 : 5;
      body.innerHTML = `<tr><td colspan="${columns}" class="empty-cell">${query ? "没有匹配的文件或文件夹" : "当前目录没有可显示的文件"}</td></tr>`;
      return;
    }
    if (scope === "remote") {
      body.innerHTML = visible.map(({ item, index }) => {
        if (item.type === "directory") return `<tr class="directory-row" data-entry-index="${index}" tabindex="0"><td><button class="folder-link" data-path="${escapeHtml(item.path)}"><span class="file-icon folder">${icon("folder")}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${Number(item.entry_count || 0)} 个对象</small></span><span class="file-chevron">${icon("chevron")}</span></button></td><td>文件夹</td><td>--</td><td>--</td><td>--</td><td>--</td></tr>`;
        const local = localObjectStatus[item.local_state] || [item.local_state || "未知", ""];
        const type = [item.kind, item.table_name].filter(Boolean).join(" · ") || "归档对象";
        const visual = entryVisual(item);
        return `<tr data-entry-index="${index}" tabindex="0"><td><div class="file-entry"><span class="file-icon ${visual.className}">${icon(visual.iconName)}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.path)}</small></span></div></td><td>${escapeHtml(type)}</td><td>${bytes(item.size_bytes)}</td><td>${Number(item.row_count || 0).toLocaleString("zh-CN")}</td><td><span class="state-pill ${local[1]}">${escapeHtml(local[0])}</span></td><td class="hash" title="${escapeHtml(item.sha256)}">${escapeHtml(String(item.sha256 || "").slice(0, 12))}</td></tr>`;
      }).join("");
    } else {
      body.innerHTML = visible.map(({ item, index }) => {
        if (item.type === "directory") return `<tr class="directory-row" data-entry-index="${index}" tabindex="0"><td><button class="folder-link" data-path="${escapeHtml(item.path)}"><span class="file-icon folder">${icon("folder")}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml((item.locations || [item.location]).filter(Boolean).join(" + "))}</small></span><span class="file-chevron">${icon("chevron")}</span></button></td><td>${escapeHtml((item.locations || [item.location]).filter(Boolean).join(" + "))}</td><td>--</td><td>--</td><td>--</td></tr>`;
        const location = item.state === "downloading" ? ["下载中", "warn"] : item.location === "verified" ? ["已验证目录", "good"] : ["暂存目录", "warn"];
        const remote = item.remote_state === "listed" ? ["已列入", "good"] : item.remote_state === "control" ? ["控制文件", ""] : ["仅本地", "warn"];
        const visual = entryVisual(item);
        return `<tr data-entry-index="${index}" tabindex="0"><td><div class="file-entry"><span class="file-icon ${visual.className}">${icon(visual.iconName)}</span><span class="file-entry-label"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.path)}</small></span></div></td><td><span class="state-pill ${location[1]}">${location[0]}</span></td><td>${bytes(item.size_bytes)}</td><td><span class="state-pill ${remote[1]}">${remote[0]}</span></td><td>${timeText(item.modified_at)}</td></tr>`;
      }).join("");
    }
    $$(".folder-link", body).forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      loadFileList(scope, button.dataset.path || "");
    }));
    $$('[data-entry-index]', body).forEach(row => {
      const select = () => selectBrowserEntry(scope, entries[Number(row.dataset.entryIndex)], row);
      row.addEventListener("click", select);
      row.addEventListener("dblclick", () => {
        const item = entries[Number(row.dataset.entryIndex)];
        if (item?.type === "directory") loadFileList(scope, item.path || "");
      });
      row.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        const item = entries[Number(row.dataset.entryIndex)];
        if (item?.type === "directory") loadFileList(scope, item.path || "");
        else select();
      });
    });
  }

  function selectBrowserEntry(scope, item, row) {
    if (!item) return;
    $$(`#${scope}-files-body tr[data-entry-index]`).forEach(current => current.classList.toggle("selected", current === row));
    renderInspector(scope, item);
  }

  function renderFileBreadcrumbs(scope, path, parentPath, archiveDate) {
    const root = $(`#${scope}-breadcrumbs`);
    const up = $(`#${scope}-up`);
    up.disabled = !archiveDate;
    const parts = path ? path.split("/") : [];
    const crumbs = [`<button class="breadcrumb root-breadcrumb" data-path="">${icon("home")}<span>归档根目录</span></button>`];
    if (archiveDate) crumbs.push(`<span class="breadcrumb-separator">/</span><button class="breadcrumb date-breadcrumb" data-date="${escapeHtml(archiveDate)}">${escapeHtml(archiveDate)}</button>`);
    let current = "";
    parts.forEach(part => {
      current = current ? `${current}/${part}` : part;
      crumbs.push(`<span class="breadcrumb-separator">/</span><button class="breadcrumb" data-path="${escapeHtml(current)}">${escapeHtml(part)}</button>`);
    });
    root.innerHTML = crumbs.join("");
    $$(".root-breadcrumb", root).forEach(button => button.addEventListener("click", () => renderDateList(scope, state.fileBrowsers[scope].dates)));
    $$(".date-breadcrumb", root).forEach(button => button.addEventListener("click", () => loadFileList(scope, "")));
    $$(".breadcrumb[data-path]", root).filter(button => !button.classList.contains("root-breadcrumb")).forEach(button => {
      button.addEventListener("click", () => loadFileList(scope, button.dataset.path || ""));
    });
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
        <label>来源<select data-field="source_type"><option value="google_drive" ${sourceType === "google_drive" ? "selected" : ""}>Google Drive</option><option value="verified_directory" ${sourceType === "verified_directory" ? "selected" : ""}>已验证目录</option></select></label>
        <label class="drive-field">rclone remote<input data-field="drive_remote" value="${escapeHtml(profile.drive_remote || "gdrive:")}"></label>
        <label class="drive-field wide-field">网盘前缀<input data-field="drive_prefix" value="${escapeHtml(profile.drive_prefix || "smsi/v3")}"></label>
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
        verified_source_root: get("verified_source_root").value.trim(), enabled: get("enabled").checked,
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
      for (const scope of ["remote", "local"]) {
        state.fileBrowsers[scope].request += 1;
        state.fileBrowsers[scope].loadedProfile = "";
        state.fileBrowsers[scope].date = "";
        state.fileBrowsers[scope].dates = [];
        state.fileBrowsers[scope].index = null;
        state.fileBrowsers[scope].meta = null;
      }
      toast("设置已保存");
    }
    catch (error) { toast(error.message, true); }
  }

  async function runScan(download) {
    try { await api("/api/actions/scan", { method: "POST", body: JSON.stringify({ download }) }); toast(download ? "增量同步已加入队列" : "新归档检查已加入队列"); await refresh(); }
    catch (error) { toast(error.message, true); }
  }

  async function downloadDate() {
    const profileId = $("#remote-profile").value;
    const archiveDate = state.fileBrowsers.remote.date;
    if (!profileId || !archiveDate) return toast("请先选择归档日期", true);
    try {
      await api("/api/actions/download", { method: "POST", body: JSON.stringify({ profile_id: profileId, archive_date: archiveDate }) });
      toast(`${archiveDate} 已加入下载队列`);
      await refresh();
    } catch (error) { toast(error.message, true); }
  }

  async function verifyDay(profile_id, archive_date) {
    try { await api("/api/actions/verify", { method: "POST", body: JSON.stringify({ profile_id, archive_date }) }); toast(`${archive_date} 已加入重新校验队列`); await refresh(); }
    catch (error) { toast(error.message, true); }
  }

  async function refresh() {
    try {
      const result = await api("/api/status");
      state.runtime = result.runtime; state.updates = result.updates; state.days = result.days; state.jobs = result.jobs || []; state.comparisons = result.comparisons || []; state.events = result.events; renderAll(); renderUpdates();
    } catch (error) {
      $("#connection-state").textContent = "连接中断"; $("#connection-state").className = "status-dot bad";
    }
  }

  async function bootstrap() {
    const result = await api("/api/bootstrap");
    state.csrf = result.csrf; state.config = result.config; state.runtime = result.runtime; state.updates = result.updates; state.days = result.days; state.jobs = result.jobs || []; state.comparisons = result.comparisons || []; state.events = result.events;
    renderAll(); renderUpdates(); populateSettings(); renderFileProfileOptions();
    if (result.initial_password_pending) toast("当前仍在使用初始密码，请在设置中更改");
    state.timer = setInterval(refresh, 5000);
  }

  $$(".nav-item").forEach(button => button.addEventListener("click", () => switchPage(button.dataset.page)));
  $$(".scope-tab").forEach(button => button.addEventListener("click", () => switchPage(button.dataset.targetPage)));
  for (const scope of ["remote", "local"]) {
    $(`#${scope}-profile`).addEventListener("change", () => {
      state.fileBrowsers[scope].loadedProfile = "";
      clearFileBrowser(scope, "正在切换采集服务器...");
      loadFileDates(scope);
    });
    $(`#${scope}-refresh`).addEventListener("click", () => loadFileDates(scope, true));
    $(`#${scope}-up`).addEventListener("click", () => navigateUp(scope));
    $(`#${scope}-search`).addEventListener("input", event => {
      const browserState = state.fileBrowsers[scope];
      if (!browserState.currentResult) return;
      browserState.query = event.currentTarget.value || "";
      renderInspector(scope);
      renderFileRows(scope);
    });
  }
  $("#scan-only").addEventListener("click", () => runScan(false));
  $("#scan-download").addEventListener("click", () => runScan(true));
  const cancelTask = async () => {
    try {
      await api("/api/actions/cancel", { method: "POST", body: "{}" });
      toast("已请求取消任务");
      await refresh();
    } catch (error) { toast(error.message, true); }
  };
  $("#cancel-task").addEventListener("click", cancelTask);
  $("#jobs-cancel-task").addEventListener("click", cancelTask);
  $("#transfer-cancel").addEventListener("click", cancelTask);
  $("#transfer-open").addEventListener("click", () => switchPage("jobs"));
  $("#remote-download").addEventListener("click", downloadDate);
  $("#check-update").addEventListener("click", checkUpdate);
  $("#download-update").addEventListener("click", downloadUpdate);
  $("#restart-update").addEventListener("click", restartUpdate);
  $("#save-settings").addEventListener("click", saveSettings);
  $("#add-profile").addEventListener("click", () => {
    const used = new Set(state.config.profiles.map(item => item.profile_id)); let counter = 1; while (used.has(`collector-${counter}`)) counter += 1;
    state.config.profiles.push({ profile_id: `collector-${counter}`, display_name: `采集服务器 ${counter}`, collector_id: `collector-${counter}`, enabled: true, source_type: "google_drive", drive_remote: "gdrive:", drive_prefix: "smsi/v3", verified_source_root: "" }); renderProfileEditor();
  });
  $("#password-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget;
    try { await api("/api/password", { method: "PUT", body: JSON.stringify({ current_password: form.elements.current_password.value, new_password: form.elements.new_password.value }) }); form.reset(); toast("密码已更新"); }
    catch (error) { toast(error.message, true); }
  });
  $("#logout").addEventListener("click", async () => { try { await api("/logout", { method: "POST", body: "{}" }); location.href = "/login"; } catch (error) { toast(error.message, true); } });
  bootstrap().catch(error => toast(error.message, true));
})();
