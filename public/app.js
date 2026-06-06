/* ═══════════════════════════════════════════════
   工作通 — 前端控制台
   支持 Playwright 自动投递引擎 + 扩展模式
   ═══════════════════════════════════════════════ */

const api = async (path, options = {}) => {
  const resp = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `HTTP ${resp.status}`);
  }
  return resp.json();
};

/* ── Settings fields ──────────────────────────── */

const fields = [
  "api_base_url", "model", "daily_chat_limit",
  "cooldown_min_ms", "cooldown_max_ms", "min_score_to_chat",
  "salary_expectation", "target_city", "target_job_keyword", "target_cities", "target_roles",
  "preferred_keywords", "blocked_keywords",
  "auto_send_initial", "auto_reply",
  "stop_on_risk_prompt", "allow_contact_info_in_messages",
];

function splitList(value) {
  return String(value || "").split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
}
function joinList(value) {
  return Array.isArray(value) ? value.join("，") : value || "";
}

/* ── UI helpers ───────────────────────────────── */

function $_(id) { return document.querySelector("#" + id); }

function setHealth(text, danger) {
  const el = $_("health");
  if (el) {
    el.textContent = text;
    el.classList.toggle("danger", !!danger);
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function escapeHtml(v) {
  return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
}
function escapeAttr(v) { return escapeHtml(v).replaceAll("`","&#096;"); }

function reasonText(job) {
  const status = String(job.status || job.decision || "");
  if (!["skipped", "skip", "error"].includes(status)) return "";
  const looksLikeOpening = (value) => /^(您好|你好|Hi|Hello)|我对.+感兴趣|期待沟通|方便的话/i.test(String(value || "").trim());
  const parts = [];
  for (const item of job.reasons || []) {
    if (item && String(item) !== String(job.initial_message || "") && !looksLikeOpening(item)) parts.push(String(item));
  }
  for (const item of job.risks || []) {
    if (item) parts.push("风险：" + String(item));
  }
  return parts.join("；");
}

/* ══════ Playwright Automation ══════════════════ */

let pwPollTimer = null;
let pwIsRunning = false;

function getActiveMode() {
  const checked = document.querySelector('input[name="pw-mode"]:checked');
  return checked ? checked.value : "expected";
}

function getSearchKeyword() {
  return $_("pw-search-keyword").value.trim();
}

function updateSearchVisibility() {
  const mode = getActiveMode();
  $_("pw-search-keyword").style.display = mode === "search" ? "inline-block" : "none";
}

async function pwLaunchBrowser() {
  const btn = $_("pw-launch");
  btn.disabled = true;
  btn.textContent = "启动中...";
  try {
    const result = await api("/api/setup/launch-browser", { method: "POST" });
    setHealth("浏览器已启动，请在打开的 Chrome 窗口中登录 BOSS 直聘。");
    $_("pw-browser-status").textContent = "运行中";
    $_("pw-message").textContent = "浏览器已就绪 \\u2713  请在 BOSS 页面登录后点击 '开始自动投递'";
    $_("pw-message").classList.remove("danger");
    return true;
  } catch (err) {
    setHealth("启动失败: " + err.message, true);
    $_("pw-browser-status").textContent = "启动失败";
    $_("pw-message").textContent = "错误: " + err.message;
    $_("pw-message").classList.add("danger");
    return false;
  } finally {
    btn.disabled = false;
    btn.textContent = "🔧 启动浏览器";
  }
}

async function pwStartAutomation() {
  if (pwIsRunning) return;
  const mode = getActiveMode();
  const searchKeyword = getSearchKeyword();
  if (mode === "search" && !searchKeyword) {
    alert("搜索模式下请先输入搜索关键词");
    return;
  }
  try {
    const result = await api("/api/automation/playwright/start", {
      method: "POST",
      body: JSON.stringify({ mode, search_keyword: searchKeyword }),
    });
    pwIsRunning = true;
    $_("pw-task-status").textContent = "运行中";
    $_("pw-progress").style.display = "block";
    $_("pw-start").disabled = true;
    $_("pw-message").textContent = result.message;
    $_("pw-message").classList.remove("danger");
    currentBatchId = "";
    lastJobsVersion = "";
    $_("jobs").innerHTML = "";
    // Start polling for progress
    pwPollTimer = setInterval(pwPollProgress, 1500);
  } catch (err) {
    alert("启动失败: " + err.message);
    setHealth(err.message, true);
  }
}

async function pwStopAutomation() {
  try {
    await api("/api/automation/playwright/stop", { method: "POST" });
    pwIsRunning = false;
    $_("pw-task-status").textContent = "已停止";
    $_("pw-start").disabled = false;
    $_("pw-message").textContent = "已手动停止";
    if (pwPollTimer) { clearInterval(pwPollTimer); pwPollTimer = null; }
  } catch (err) {
    alert("停止失败: " + err.message);
  }
}

async function pwPollProgress() {
  try {
    const data = await api("/api/automation/playwright/status");
    const stats = data.stats || {};

    // Switch to current batch_id when automation starts a new run
    if (data.batch_id && data.batch_id !== currentBatchId) {
      currentBatchId = data.batch_id;
      lastJobsVersion = "";
      await loadJobs();
    }

    $_("pw-task-status").textContent = data.running ? "运行中" : (data.status || "空闲");
    $_("pw-stat-sent").textContent = stats.sent || 0;
    $_("pw-stat-skipped").textContent = stats.skipped || 0;
    $_("pw-stat-errors").textContent = stats.errors || 0;
    const total = stats.total || 0;
    const done = (stats.sent || 0) + (stats.skipped || 0) + (stats.errors || 0);
    $_("pw-stat-total").textContent = done + "/" + total;
    $_("pw-status-text").textContent = data.message || "";
    $_("pw-message").textContent = data.message || "就绪";

    // Progress bar
    if (total > 0) {
      const pct = Math.min(100, Math.round(done / total * 100));
      $_("pw-progress-fill").style.width = pct + "%";
      $_("pw-progress-fill").textContent = pct + "%";
    }

    await checkJobsVersion();

    // Detect completion
    if (!data.running && pwIsRunning) {
      pwIsRunning = false;
      $_("pw-start").disabled = false;
      $_("pw-task-status").textContent = data.status === "completed" ? "✅ 完成" : "已停止";
      $_("pw-progress-fill").style.width = "100%";
      if (pwPollTimer) { clearInterval(pwPollTimer); pwPollTimer = null; }
      // Refresh jobs & events
      await loadJobs();
      await loadEvents();
    }
  } catch {
    // Silent — browser might be restarting
  }
}

async function pwCloseBrowser() {
  const btn = $_("pw-close-browser");
  btn.disabled = true;
  btn.textContent = "关闭中...";
  try {
    await api("/api/setup/stop-browser", { method: "POST" });
    $_("pw-browser-status").textContent = "未启动";
    $_("pw-task-status").textContent = "空闲";
    $_("pw-message").textContent = "浏览器已关闭";
    setHealth("浏览器已关闭");
  } catch (err) {
    alert("关闭失败: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "❌ 关闭浏览器";
  }
}

/* ══════ Settings ═══════════════════════════════ */

async function loadHealth() {
  try {
    const health = await api("/api/health");
    const parts = [];
    parts.push(health.deepseek_configured ? "DeepSeek ✓" : "DeepSeek ✗");
    parts.push(health.model || "");
    parts.push(health.browser_running ? "浏览器: 运行中" : "浏览器: 未启动");
    setHealth(parts.join("  |  "));
    $_("pw-browser-status").textContent = health.browser_running ? "运行中" : "未启动";
  } catch (err) {
    setHealth("服务异常: " + err.message, true);
  }
}

async function loadSettings() {
  const settings = await api("/api/settings");
  for (const key of fields) {
    const node = $_(key);
    if (!node) continue;
    if (node.type === "checkbox") {
      node.checked = Boolean(settings[key]);
    } else if (Array.isArray(settings[key])) {
      node.value = joinList(settings[key]);
    } else {
      node.value = settings[key] ?? "";
    }
  }
}

async function saveSettings() {
  const payload = {};
  for (const key of fields) {
    const node = $_(key);
    if (!node) continue;
    if (node.type === "checkbox") {
      payload[key] = node.checked;
    } else if (["target_cities","target_roles","preferred_keywords","blocked_keywords"].includes(key)) {
      payload[key] = splitList(node.value);
    } else if (["daily_chat_limit","cooldown_min_ms","cooldown_max_ms","min_score_to_chat"].includes(key)) {
      payload[key] = Number(node.value || 0);
    } else {
      payload[key] = node.value;
    }
  }
  await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload) });
  await loadAll();
}

/* ══════ Resumes ═══════════════════════════════ */

async function uploadResume(e) {
  e.preventDefault();
  const input = $_("resume-file");
  if (!input.files?.[0]) return;
  const form = new FormData();
  form.append("file", input.files[0]);
  const resp = await fetch("/api/resumes/upload", { method: "POST", body: form });
  if (!resp.ok) throw new Error(await resp.text());
  input.value = "";
  await loadAll();
}

async function analyzeTextResume() {
  const text = $_("resume-text").value;
  if (!text.trim()) return;
  await api("/api/resumes/text", {
    method: "POST",
    body: JSON.stringify({ text, filename: "pasted-resume.txt" }),
  });
  $_("resume-text").value = "";
  await loadAll();
}

async function activateResume(id) {
  await api(`/api/resumes/${id}/activate`, { method: "POST" });
  await loadAll();
  setHealth("当前简历已切换");
}

async function loadResumes() {
  const resumes = await api("/api/resumes");
  const root = $_("resumes");
  root.innerHTML = "";
  for (const r of resumes) {
    const item = document.createElement("div");
    item.className = "item";
    const title = r.analysis?.name || r.filename || "未命名";
    const skills = (r.analysis?.core_skills || []).slice(0, 8).join("，");
    item.innerHTML = `
      <strong>${escapeHtml(title)} ${r.is_active ? "⭐当前" : ""}</strong>
      <small>${escapeHtml(r.analysis?.summary || "")}</small>
      <small>${escapeHtml(skills)}</small>
      <div><button ${r.is_active ? "disabled" : ""}>${r.is_active ? "已是当前" : "设为当前"}</button></div>
    `;
    item.querySelector("button").addEventListener("click", () => activateResume(r.id));
    root.appendChild(item);
  }
}

/* ══════ Jobs & Events ═════════════════════════ */

let currentBatchId = "";
let lastJobsVersion = "";
let jobsLoading = false;

async function loadJobs() {
  let url = "/api/jobs?limit=500";
  if (currentBatchId) url += "&batch_id=" + encodeURIComponent(currentBatchId);
  const jobs = await api(url);
  const tbody = $_("jobs");
  tbody.innerHTML = "";
  for (const j of jobs) {
    const tr = document.createElement("tr");
    const skipReason = reasonText(j);
    tr.innerHTML = `
      <td>${j.score}</td>
      <td>${escapeHtml(j.status || j.decision || "")}</td>
      <td><a href="${escapeAttr(j.url)}" target="_blank">${escapeHtml(j.title || "岗位")}</a></td>
      <td>${escapeHtml(j.company || "")}</td>
      <td>${escapeHtml(skipReason.slice(0, 160))}</td>
      <td>${escapeHtml((j.initial_message || "").slice(0, 80))}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function checkJobsVersion() {
  if (jobsLoading) return;
  let url = "/api/jobs/version";
  if (currentBatchId) url += "?batch_id=" + encodeURIComponent(currentBatchId);
  const version = await api(url);
  const token = `${version.batch_id || ""}|${version.count || 0}|${version.latest_updated_at || ""}`;
  if (token === lastJobsVersion) return;
  lastJobsVersion = token;
  jobsLoading = true;
  try {
    await loadJobs();
  } finally {
    jobsLoading = false;
  }
}

async function loadEvents() {
  const events = await api("/api/events?limit=80");
  const root = $_("events");
  root.innerHTML = "";
  for (const e of events) {
    const item = document.createElement("div");
    item.className = "event";
    item.innerHTML = `
      <strong>${escapeHtml(e.type)}</strong>
      <div class="muted">${escapeHtml(e.created_at || "")}</div>
      <div>${escapeHtml(JSON.stringify(e.payload || {}))}</div>
    `;
    root.appendChild(item);
  }
}

/* ══════ Load all ══════════════════════════════ */

async function loadAll() {
  await loadHealth();
  await loadSettings();
  await Promise.all([loadResumes(), loadJobs(), loadEvents()]);
  await pwPollProgress();
}

function alertError(err) { alert(err.message || String(err)); }

/* ══════ Event bindings ════════════════════════ */

$_("refresh").addEventListener("click", () => loadAll().catch(alertError));
$_("save-settings").addEventListener("click", () => saveSettings().catch(alertError));
$_("upload-form").addEventListener("submit", e => uploadResume(e).catch(alertError));
$_("analyze-text").addEventListener("click", () => analyzeTextResume().catch(alertError));

// Playwright buttons
$_("pw-launch").addEventListener("click", () => pwLaunchBrowser().catch(alertError));
$_("pw-start").addEventListener("click", () => pwStartAutomation().catch(alertError));
$_("pw-stop").addEventListener("click", () => pwStopAutomation().catch(alertError));
$_("pw-close-browser").addEventListener("click", () => pwCloseBrowser().catch(alertError));

/* ══════ Init ══════════════════════════════════ */

// Mode selector: toggle search input visibility
document.querySelectorAll('input[name="pw-mode"]').forEach(radio => {
  radio.addEventListener("change", updateSearchVisibility);
});
updateSearchVisibility();

loadAll().catch(alertError);
setInterval(() => loadHealth().catch(() => {}), 5000);
setInterval(() => checkJobsVersion().catch(() => {}), 1500);
