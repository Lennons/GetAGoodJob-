/* ═══════════════════════════════════════════════
   工作通 — Frontend
   ═══════════════════════════════════════════════ */
const api = async (path, opts = {}) => {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
};
const $ = id => document.getElementById(id);
const $$ = (sel, root) => (root || document).querySelectorAll(sel);

const FIELDS = ["api_key","model","daily_chat_limit","cooldown_min_ms","cooldown_max_ms","reply_poll_seconds","min_score_to_chat","target_job_keyword","target_cities","filter_city","blocked_keywords","auto_send_initial","stop_on_risk_prompt","deep_delivery","allow_contact_info_in_messages"];
function splitList(v) { return String(v||"").split(/[,，\n]/).map(s=>s.trim()).filter(Boolean); }
function joinList(v) { return Array.isArray(v)?v.join("，"):v||""; }

let timer = null, running = false, batchId = "", lastVer = "", loading = false, currentJobData = [], jobSearch = "", searchTimer = null;

// ══════ Page Routing ════════════════════════
function navigateTo(page) {
  $$(".page").forEach(p => p.classList.remove("active"));
  $$(".nav-item").forEach(x => x.classList.remove("active"));
  const target = $(`page-${page}`);
  if (target) target.classList.add("active");
  const navBtn = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (navBtn) navBtn.classList.add("active");
  if (page === "settings") loadResumes();
  if (page === "datacenter") loadDatacenter();
  if (page === "workflow") loadDatacenter();
}
$$(".nav-item").forEach(b => {
  b.addEventListener("click", () => { const page = b.dataset.page; if (page) navigateTo(page); });
});

// ══════ Job Detail Drawer ═══════════════════
function openJobDrawer(job) {
  const statusTag = s => ["sent","chat_started"].includes(s) ? "tag-sent" : ["skipped","skip"].includes(s) ? "tag-skip" : s === "error" ? "tag-err" : s === "evaluated" ? "tag-eval" : "";
  const s = job.status || job.decision || "";
  const ts = job.created_at ? job.created_at.slice(0,16).replace("T"," ") : "−";
  $("job-detail-title").textContent = job.title || "岗位详情";
  $("job-detail-body").innerHTML = `
    <div class="job-detail-score">${job.score} <span style="font-size:14px;color:var(--text-secondary)">分</span></div>
    <div class="job-detail-meta">
      <span class="tag ${statusTag(s)}">${s||"−"}</span>
      <span style="font-size:12px;color:var(--text-secondary)">${job.company||"−"}</span>
      <span style="font-size:12px;color:var(--text-secondary)">${job.city||""} · ${job.salary||""}</span>
      <span style="font-size:12px;color:var(--text-muted)">采集于 ${ts}</span>
    </div>
    ${job.url ? `<a href="${job.url}" target="_blank" style="font-size:12px;color:var(--primary)">在 BOSS 直聘查看 →</a>` : ""}
    ${(job.reasons||[]).filter(r=>r).length ? `<div class="job-detail-section"><h4>跳过原因</h4>${job.reasons.filter(r=>r).map(r => `<div class="job-detail-reason">${cleanReason(r)}</div>`).join("")}</div>` : ""}
    ${(job.risks||[]).filter(r=>r).length ? `<div class="job-detail-section"><h4>风险提示</h4>${job.risks.filter(r=>r).map(r => `<div class="job-detail-risk">${cleanReason(r)}</div>`).join("")}</div>` : ""}
    ${job.initial_message ? `<div class="job-detail-section"><h4>AI 开场白</h4><div class="job-detail-msg">${job.initial_message}</div></div>` : ""}
    ${job.description ? `<div class="job-detail-section"><h4>职位描述</h4><div class="job-detail-desc">${job.description}</div></div>` : ""}
  `;
  $("job-drawer").classList.add("open");
  $("job-drawer-overlay").classList.add("open");
}
function closeJobDrawer() { $("job-drawer").classList.remove("open"); $("job-drawer-overlay").classList.remove("open"); }
$("job-drawer-close")?.addEventListener("click", closeJobDrawer);
$("job-drawer-overlay")?.addEventListener("click", closeJobDrawer);

// ══════ Mode ════════════════════════════════
function getMode() { const c = document.querySelector('input[name="pw-mode"]:checked'); return c?c.value:"expected"; }
function getSearch() { return $("pw-search-keyword").value.trim(); }
function updateModeUI() {
  const m = getMode();
  $$(".mode-card").forEach(c => c.classList.toggle("selected", c.querySelector("input").value === m));
  $("search-box").style.display = m === "search" ? "flex" : "none";
}
$$(".mode-card").forEach(c => c.addEventListener("click", () => { c.querySelector("input").checked = true; updateModeUI(); }));

// ══════ Settings ════════════════════════════
async function loadSettings() {
  const s = await api("/api/settings");
  for (const k of FIELDS) { const el = $(k); if (!el) continue;
    if (el.type === "checkbox") el.checked = !!s[k];
    else if (Array.isArray(s[k])) el.value = joinList(s[k]);
    else el.value = s[k] ?? "";
  }
}

async function saveSettings() {
  const p = {};
  for (const k of FIELDS) { const el = $(k); if (!el) continue;
    if (el.type === "checkbox") p[k] = el.checked;
    else if (["target_cities","blocked_keywords"].includes(k)) p[k] = splitList(el.value);
    else if (["daily_chat_limit","cooldown_min_ms","cooldown_max_ms","reply_poll_seconds","min_score_to_chat"].includes(k)) p[k] = Number(el.value || 0);
    else p[k] = el.value;
  }
  await api("/api/settings", { method: "PATCH", body: JSON.stringify(p) });
  await loadSettings();
}

// ══════ Health ═════════════════════════════
async function checkHealth() {
  try {
    const h = await api("/api/health");
    const ok = h.browser_running;
    $("cdp-status").textContent = ok ? "9223 已连接" : "未连接";
    $("cdp-status").style.color = ok ? "#34D399" : "#94A3B8";
    setBrowserBtn(ok);
  } catch(e) { $("cdp-status").textContent = "离线"; $("cdp-status").style.color = "#EF4444"; }
}

// ══════ Toggle Buttons ═════════════════════════
let browserOn = false, autoOn = false, replyOn = false;

function setBrowserBtn(state, busy) {
  browserOn = state;
  const b = $("pw-browser");
  if (busy) { b.disabled = true; b.textContent = busy; return; }
  b.disabled = false; b.textContent = state ? "关闭浏览器" : "启动浏览器";
  b.className = "btn " + (state ? "btn-danger" : "btn-primary");
}
function setAutoBtn(state, busy) {
  autoOn = state;
  const b = $("pw-auto");
  if (busy) { b.disabled = true; b.textContent = busy; return; }
  b.disabled = false; b.textContent = state ? "停止投递" : "开始投递";
  b.className = "btn " + (state ? "btn-danger" : "btn-success");
}
function setReplyBtn(state, busy) {
  replyOn = state;
  const b = $("pw-reply");
  if (busy) { b.disabled = true; b.textContent = busy; return; }
  b.disabled = false; b.textContent = state ? "关闭自动回复" : "开启自动回复";
  b.className = "btn " + (state ? "btn-ghost" : "btn-accent");
}

async function toggleBrowser() {
  if (browserOn) {
    setBrowserBtn(false, "关闭中…");
    try { await api("/api/setup/stop-browser", { method: "POST" }); setBrowserBtn(false); } catch(e) { setBrowserBtn(true); $("progress-last").textContent = "关闭失败：" + e.message; }
    await checkHealth();
  } else {
    setBrowserBtn(false, "启动中…");
    try { await api("/api/setup/launch-browser", { method: "POST" }); setBrowserBtn(true); $("progress-last").textContent = "浏览器已启动"; } catch(e) { setBrowserBtn(false); $("progress-last").textContent = "启动失败：" + e.message; }
    await checkHealth();
  }
}
async function toggleAuto() {
  if (autoOn) {
    setAutoBtn(false, "停止中…");
    try { await api("/api/automation/playwright/stop", { method: "POST" }); setAutoBtn(false); running = false; if (timer) { clearInterval(timer); timer = null; } $("kpi-task").textContent = "就绪"; $("kpi-task").className = "kpi-value green"; $("progress-subtitle").textContent = "已停止"; startPolling(); } catch(e) { setAutoBtn(true); $("progress-last").textContent = "停止失败：" + e.message; }
  } else {
    setAutoBtn(false, "启动中…");
    const mode = getMode(), keyword = getSearch();
    try { await api("/api/automation/playwright/start", { method: "POST", body: JSON.stringify({ mode, search_keyword: keyword }) }); setAutoBtn(true); running = true; $("kpi-task").textContent = "投递中"; $("kpi-task").className = "kpi-value green"; startPolling(); } catch(e) { setAutoBtn(false); $("progress-last").textContent = "启动失败：" + e.message; }
  }
}
async function toggleReply() {
  if (replyOn) {
    setReplyBtn(false, "关闭中…");
    try { const r = await api("/api/reply-monitor/stop", { method: "POST" }); setReplyBtn(false); $("progress-last").textContent = "自动回复已关闭"; } catch(e) { setReplyBtn(true); $("progress-last").textContent = "关闭失败：" + e.message; }
  } else {
    setReplyBtn(false, "启动中…");
    try { const r = await api("/api/reply-monitor/start", { method: "POST" }); setReplyBtn(true); $("progress-last").textContent = "自动回复已开启"; } catch(e) { setReplyBtn(false); $("progress-last").textContent = "启动失败：" + e.message; }
  }
}

function startPolling() { if (timer) clearInterval(timer); timer = setInterval(pollAutomation, 1500); pollAutomation(); }
async function pollAutomation() {
  try {
    const d = await api("/api/automation/poll", { method: "POST", body: JSON.stringify({status:"online",running}) });
    if (d.batch_id && d.batch_id !== batchId) { batchId = d.batch_id; lastVer = ""; await loadJobs(); }

    if (d.running !== autoOn) setAutoBtn(d.running);
    if (d.running) { $("kpi-task").textContent = "投递中"; $("kpi-task").className = "kpi-value green"; }
    if (d.running && !running) { running = true; startPolling(); }
    if (!d.running && running) { running = false; $("kpi-task").textContent = "就绪"; $("kpi-task").className = "kpi-value green"; $("progress-subtitle").textContent = d.status === "completed" ? "完成" : "已停止"; await loadJobs(); }
    setBrowserBtn(d.browser_running);
    $("progress-subtitle").textContent = d.message || "运行中…";
    $("progress-last").textContent = d.last_action || "";
    if (d.progress_pct != null) { $("pw-progress-fill").style.width = d.progress_pct + "%"; $("progress-pct").textContent = d.progress_pct + "%"; }
    if (d.eta) $("progress-eta").textContent = d.eta;
    $("kpi-browser").textContent = d.browser_running ? "已连接" : "未启动";
    $("kpi-browser").className = "kpi-value " + (d.browser_running ? "green" : "blue");
    $("kpi-sent").textContent = d.sent || 0;
    $("kpi-skipped").textContent = d.skipped || 0;
    $("kpi-errors").textContent = d.errors || 0;
    $("kpi-progress").textContent = (d.current||0) + "/" + (d.total||0);
    const pc = $("progress-counter"); if (pc) pc.textContent = (d.current||0) + " / " + (d.total||0);
    try { const q = await api("/api/automation/quota"); $("kpi-quota").textContent = `${q.used||0} / ${q.limit||0}`; } catch(e) {}
    try { const rp = await api("/api/reply-monitor/status"); setReplyBtn(rp.running); } catch(e) {}
    try { const rl = await api("/api/reply-logs?limit=20"); $("kpi-replies").textContent = rl.total || 0; renderReplyLogs(rl.logs || []); } catch(e) {}
    await checkVer();
  } catch(e) {}
}

// ══════ Helpers ═══════════════════════════════
function cleanReason(r) { return String(r||"").replace(/:\s*['"]?\w+_not_found['"]?/g, "").replace(/:\s*'NoneType'.*/g, "").replace(/:\s*name\s+'re'.*/g, "").replace(/:\s*job_card_not_found/g, "").replace(/：\s*job_card_not_found/g, ""); }
// ══════ Jobs ═══════════════════════════════
let jobPage = 1, jobPageSize = 10, jobTotal = 0, jobStatusFilter = "";

function updateJobPagination() {
  const tp = Math.max(1, Math.ceil(jobTotal / jobPageSize));
  $("job-page-info").textContent = `第 ${jobPage} / ${tp} 页（共 ${jobTotal} 条）`;
  $("job-prev").disabled = jobPage <= 1;
  $("job-next").disabled = jobPage >= tp;
}

async function loadJobs() {
  const offset = (jobPage - 1) * jobPageSize;
  let url = `/api/jobs?limit=${jobPageSize}&offset=${offset}`;
  if (jobStatusFilter) url += "&status=" + encodeURIComponent(jobStatusFilter);
  if (jobSearch) url += "&search=" + encodeURIComponent(jobSearch);
  const data = await api(url);
  const jobs = data.jobs; jobTotal = data.total; currentJobData = jobs;
  const statusTag = s => ["sent","chat_started"].includes(s) ? "tag-sent" : ["skipped","skip"].includes(s) ? "tag-skip" : s === "error" ? "tag-err" : s === "evaluated" ? "tag-eval" : "";
  $("jobs").innerHTML = jobs.map((j, i) => {
    const seq = j.seq ?? (data.total - offset - i);
    const s = j.status||j.decision||"";
    // Reorder reasons: move "分数 X 低于..." boilerplate to the end, AI reasons first
    const rawReasons = (j.reasons||[]).map(cleanReason).filter(r=>r);
    const base = rawReasons.filter(r => !/^分数\s*\d+\s*低于/.test(r));
    const scoreLine = rawReasons.filter(r => /^分数\s*\d+\s*低于/.test(r));
    const orderedReasons = [...base, ...scoreLine];
    const reasons = orderedReasons.join("；");
    const risks = (j.risks||[]).map(r=>"⚠"+r).join("；"), note = [reasons,risks].filter(Boolean).join(" ").slice(0,200);
    const ts = j.created_at ? j.created_at.slice(0,16).replace("T"," ") : "−";
    return `<tr data-job-seq="${j.seq}"><td style="color:var(--text-secondary);font-size:12px;text-align:center">${seq}</td><td class="score">${j.score}</td><td><span class="tag ${statusTag(s)}">${s||"−"}</span></td><td><a href="${j.url||'#'}" target="_blank" onclick="event.stopPropagation()">${(j.title||"岗位").slice(0,40)}</a></td><td>${j.company||"−"}</td><td style="font-size:12px;color:var(--text-secondary);white-space:nowrap">${ts}</td><td style="font-size:12px;color:var(--text-secondary)">${note||j.initial_message||""}</td></tr>`;
  }).join("");
  $("job-header-count").textContent = `共 ${jobTotal} 条记录`;
  updateJobPagination();
  $$("#jobs tr").forEach(tr => tr.addEventListener("click", () => { const j = currentJobData.find(x => x.seq === parseInt(tr.dataset.jobSeq)); if (j) openJobDrawer(j); }));
}

function renderReplyLogs(logs) {
  const tbody = $("reply-logs");
  if (!tbody) return;
  const total = logs.length;
  $("reply-header-count").textContent = "共 " + total + " 条记录";
  tbody.innerHTML = logs.map((l, i) => {
    const ts = l.created_at ? l.created_at.slice(0, 16).replace("T", " ") : "−";
    return `<tr><td style="color:var(--text-secondary);font-size:12px;text-align:center">${l.id}</td><td style="font-size:12px;color:var(--text-secondary);white-space:nowrap">${ts}</td><td>${l.company || "−"}</td><td>${l.title || "−"}</td><td style="font-size:12px;color:var(--text-secondary);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(l.message || "").slice(0, 80)}</td></tr>`;
  }).join("");
}

async function checkVer() {
  if (loading) return;
  let url = "/api/jobs/version";
  if (jobStatusFilter) url += "&status=" + encodeURIComponent(jobStatusFilter);
  if (jobSearch) url += "&search=" + encodeURIComponent(jobSearch);
  const v = await api(url), token = `${v.batch_id||""}|${v.count||0}|${v.latest_updated_at||""}`;
  if (token === lastVer) return; lastVer = token; loading = true;
  try { await loadJobs(); } finally { loading = false; }
}

// ══════ Resumes ════════════════════════════
async function uploadResume(e) { e.preventDefault();
  const inp = $("resume-file"); if (!inp.files?.[0]) return;
  const fd = new FormData(); fd.append("file", inp.files[0]);
  await fetch("/api/resumes/upload", { method: "POST", body: fd }); inp.value = ""; await loadResumes();
}
async function analyzeText() {
  const t = $("resume-text").value.trim(); if (!t) return;
  await api("/api/resumes/text", { method: "POST", body: JSON.stringify({ text:t, filename:"paste.txt" }) });
  $("resume-text").value = ""; await loadResumes();
}
async function activateResume(id) { await api(`/api/resumes/${id}/activate`, { method: "POST" }); await loadResumes(); }
async function deleteResume(id) { if (!confirm("确定要删除该简历吗？")) return; await api(`/api/resumes/${id}`, { method: "DELETE" }); await loadResumes(); }
async function loadResumes() {
  try {
    const resumes = await api("/api/resumes");
    $("resumes").innerHTML = resumes.map(r => {
      const name = r.analysis?.name || r.filename || "未命名", skills = (r.analysis?.core_skills||[]).slice(0,4).join("、");
      return `<div class="resume-card"><span class="rname">${name}${r.is_active?' <span class="rtag">当前</span>':''}</span><span class="rskills">${skills}</span><button class="btn btn-ghost btn-sm resume-remove-btn" data-rid="${r.id}">删除</button><button class="btn btn-ghost btn-sm resume-activate-btn" data-rid="${r.id}" ${r.is_active?'disabled':''}>${r.is_active?'已设':'启用'}</button></div>`;
    }).join("");
    $$(".resume-card .resume-activate-btn").forEach(btn => btn.addEventListener("click", () => activateResume(btn.dataset.rid)));
    $$(".resume-card .resume-remove-btn").forEach(btn => btn.addEventListener("click", () => deleteResume(btn.dataset.rid)));
    const active = resumes.find(r => r.is_active);
    const detail = $("resume-detail");
    if (active && active.analysis) {
      const a = active.analysis;
      detail.style.display = "block";
      detail.innerHTML = `<strong>当前简历：${a.name||active.filename}</strong><br>状态：已解析 · 最近用于岗位评分<br>${a.name ? `姓名：${a.name}<br>` : ""}${a.experience_years ? `经验：${a.experience_years} 年<br>` : ""}${a.current_role ? `当前角色：${a.current_role}<br>` : ""}${a.salary_expectation ? `期望薪资：${a.salary_expectation}<br>` : ""}${a.core_skills?.length ? `核心技能：${a.core_skills.join("、")}<br>` : ""}${a.summary ? `摘要：${a.summary}` : ""}`;
    } else { detail.style.display = "none"; }
  } catch(e) {}
}

// ══════ Datacenter ═══════════════════════
async function loadDatacenter() {
  try {
    const all = await api("/api/jobs?limit=1");
    const sent = await api("/api/jobs?limit=1&status=chat_started");
    const sent2 = await api("/api/jobs?limit=1&status=sent");
    const skipped = await api("/api/jobs?limit=1&status=skipped");
    const errors = await api("/api/jobs?limit=1&status=error");
    const totalSent = (sent.total||0) + (sent2.total||0);
    const totalSkipped = skipped.total||0;
    const totalErrors = errors.total||0;
    const totalScanned = (all.total||0);
    const dcScanned = $("dc-scanned"); if (dcScanned) dcScanned.textContent = totalScanned;
    const dcSent = $("dc-sent"); if (dcSent) dcSent.textContent = totalSent;
    const dcSkipped = $("dc-skipped"); if (dcSkipped) dcSkipped.textContent = totalSkipped;
    const dcErrors = $("dc-errors"); if (dcErrors) dcErrors.textContent = totalErrors;
    loadHotKeywords();
  } catch(e) {}
}

let _hotwordsInited = false;
async function loadHotKeywords() {
  const cloud = $("hotwords-cloud");
  if (!cloud) return;
  try {
    let kw = await api("/api/jobs/keywords?limit=20");
    // Auto-trigger one-time full analysis if no keywords yet
    if (!kw?.length && !_hotwordsInited) {
      _hotwordsInited = true;
      cloud.innerHTML = '<span style="color:var(--text-muted);font-size:13px">正在初始化热词分析…</span>';
      try {
        const r = await api("/api/jobs/keywords/analyze", { method: "POST" });
        kw = await api("/api/jobs/keywords?limit=20");
      } catch(e) {}
    }
    _hotwordsInited = true;
    if (!kw?.length) { cloud.innerHTML = '<span style="color:var(--text-muted);font-size:13px">暂无热词数据，投递扫描后自动生成</span>'; return; }
    const maxC = kw[0].count;
    const catColors = { skill: "#DBEAFE", tool: "#D1FAE5", knowledge: "#FEF3C7" };
    const catLabels = { skill: "技能", tool: "工具", knowledge: "知识" };
    cloud.style.justifyContent = "center";
    cloud.innerHTML = kw.map(k => {
      const size = 14 + Math.round((k.count / maxC) * 20);
      const bg = catColors[k.category] || '#EFF6FF';
      return `<span class="hotwords-tag" style="font-size:${size}px;background:${bg}">${k.word}<span style="font-size:9px;color:var(--text-muted);margin-left:4px">${k.count}</span><span style="font-size:8px;background:var(--border-light);padding:1px 5px;border-radius:4px;margin-left:4px">${catLabels[k.category]||"技能"}</span></span>`;
    }).join("");
  } catch(e) {
    cloud.innerHTML = `<span style="color:var(--danger);font-size:13px">加载失败: ${e.message}</span>`;
  }
}

// ══════ Init ═══════════════════════════════
async function init() { checkHealth(); loadSettings(); loadJobs(); loadVersion(); }
async function loadVersion() {
  try { const v = await api("/api/version"); $("app-version").textContent = "v" + (v.version || "?"); } catch(e) { $("app-version").textContent = ""; }
}

$("pw-browser").addEventListener("click", () => toggleBrowser());
$("pw-auto").addEventListener("click", () => toggleAuto());
$("pw-reply").addEventListener("click", () => toggleReply());
$("save-settings").addEventListener("click", () => saveSettings());
$("upload-form").addEventListener("submit", e => uploadResume(e));
$("analyze-text").addEventListener("click", () => analyzeText());
$("job-status-filter").addEventListener("change", () => { jobStatusFilter = $("job-status-filter").value; jobPage = 1; loadJobs(); });
$("job-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { jobSearch = $("job-search").value.trim(); jobPage = 1; loadJobs(); }, 400); });
$("job-prev").addEventListener("click", () => { if (jobPage > 1) { jobPage--; loadJobs(); } });
$("job-next").addEventListener("click", () => { const tp = Math.max(1, Math.ceil(jobTotal / jobPageSize)); if (jobPage < tp) { jobPage++; loadJobs(); } });



init(); updateModeUI();
setInterval(checkHealth, 8000);
setInterval(checkVer, 2000);
