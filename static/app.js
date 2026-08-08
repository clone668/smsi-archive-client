(() => {
  "use strict";
  const state = { csrf: "", config: null, runtime: null, days: [], events: [], timer: null };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
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
    unknown: ["未知", ""],
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
      const [label, tone] = statusMap[item.status] || [item.status, ""];
      const action = item.status === "verified" ? `<button class="button small secondary verify-day" data-profile="${escapeHtml(item.profile_id)}" data-date="${escapeHtml(item.archive_date)}">重新校验</button>` : "";
      return `<tr title="${escapeHtml(item.error || item.detail || "")}"><td>${escapeHtml(item.archive_date)}</td><td>${escapeHtml(profiles[item.profile_id] || item.profile_id)}</td><td><span class="state-pill ${tone}">${escapeHtml(label)}</span></td><td>${Number(item.objects_done || 0)}/${Number(item.object_count || 0)}</td><td>${bytes(item.bytes_done || item.bytes_total)}</td><td>${timeText(item.updated_at)}</td><td>${action}</td></tr>`;
    }).join("");
    $$(".verify-day", body).forEach(button => button.addEventListener("click", () => verifyDay(button.dataset.profile, button.dataset.date)));
  }

  function renderEvents() {
    const root = $("#event-list");
    if (!state.events.length) { root.innerHTML = '<p class="empty-cell">暂无记录</p>'; return; }
    root.innerHTML = state.events.map(item => `<article class="event-item ${escapeHtml(item.level)}"><header><strong>${escapeHtml(item.event)}</strong><time>${timeText(item.created_at)}</time></header><p>${escapeHtml([item.profile_id, item.archive_date, item.detail].filter(Boolean).join(" · ") || "--")}</p></article>`).join("");
  }

  function renderAll() { renderMetrics(); renderProfiles(); renderDays(); renderEvents(); }

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
    try { const result = await api("/api/config", { method: "PUT", body: JSON.stringify(payload) }); state.config = result.config; populateSettings(); toast("设置已保存"); }
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
      state.runtime = result.runtime; state.days = result.days; state.events = result.events; renderAll();
    } catch (error) {
      $("#connection-state").textContent = "连接中断"; $("#connection-state").className = "status-dot bad";
    }
  }

  async function bootstrap() {
    const result = await api("/api/bootstrap");
    state.csrf = result.csrf; state.config = result.config; state.runtime = result.runtime; state.days = result.days; state.events = result.events;
    renderAll(); populateSettings();
    if (result.initial_password_pending) toast("当前仍在使用初始密码，请在设置中更改");
    state.timer = setInterval(refresh, 5000);
  }

  $$(".tab").forEach(button => button.addEventListener("click", () => {
    $$(".tab").forEach(item => item.classList.toggle("active", item === button));
    $$(".page").forEach(page => page.classList.toggle("active", page.id === `${button.dataset.page}-page`));
  }));
  $("#scan-only").addEventListener("click", () => runScan(false));
  $("#scan-download").addEventListener("click", () => runScan(true));
  $("#cancel-task").addEventListener("click", async () => { try { await api("/api/actions/cancel", { method: "POST", body: "{}" }); toast("已请求取消任务"); } catch (error) { toast(error.message, true); } });
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
