import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Job, ReplyLog } from "../types";
import JobDrawer from "../components/JobDrawer";

type Mode = "recommend" | "expected" | "search";

function cleanReason(r: string) {
  return String(r || "").replace(/:\s*['"]?\w+_not_found['"]?/g, "").replace(/:\s*'NoneType'.*/g, "").replace(/:\s*name\s+'re'.*/g, "").replace(/:\s*job_card_not_found/g, "").replace(/：\s*job_card_not_found/g, "");
}

export default function Dashboard() {
  const [browserRunning, setBrowserRunning] = useState(false);
  
  const [replyRunning, setReplyRunning] = useState(false);
  const [replyCount, setReplyCount] = useState(0);
  const [mode, setMode] = useState<Mode>("expected");
  const [searchKw, setSearchKw] = useState("");
  const [pct, setPct] = useState<string | number>("−");
  const [progressMsg, setProgressMsg] = useState("点击「开始投递」启动自动化流程");
  const [lastAction, setLastAction] = useState("就绪，等待任务启动");
  const [eta, setEta] = useState("");
  const [counter, setCounter] = useState("0 / 0");
  const [barW, setBarW] = useState(0);
  const [taskStatus, setTaskStatus] = useState("就绪");
  const [sent, setSent] = useState(0);
  const [skipped, setSkipped] = useState(0);
  const [errors, setErrors] = useState(0);
  const [total, setTotal] = useState(0);
  const [current, setCurrent] = useState(0);
  const [quotaUsed, setQuotaUsed] = useState(0);
  const [quotaLimit, setQuotaLimit] = useState(0);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobTotal, setJobTotal] = useState(0);
  const [jobPage, setJobPage] = useState(1);
  const [jobPageSize] = useState(10);
  const [jobStatusFilter, setJobStatusFilter] = useState("");
  const [jobSearch, setJobSearch] = useState("");
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [replyLogs, setReplyLogs] = useState<ReplyLog[]>([]);
  const [replyLogsTotal, setReplyLogsTotal] = useState(0);
  const [batchId, setBatchId] = useState("");
  const [lastVer, setLastVer] = useState("");
  const [loading, setLoading] = useState(false);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pollTimer = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const runningRef = useRef(false);

  const loadJobs = useCallback(async () => {
    const params: Record<string, string> = { limit: String(jobPageSize), offset: String((jobPage - 1) * jobPageSize) };
    if (jobStatusFilter) params.status = jobStatusFilter;
    if (jobSearch) params.search = jobSearch;
    try {
      const res = await api.listJobs(params);
      setJobs(res.jobs);
      setJobTotal(res.total);
    } catch {}
  }, [jobPage, jobPageSize, jobStatusFilter, jobSearch]);

  const loadReplyLogs = useCallback(async () => {
    try {
      const r = await api.listReplyLogs(50, 0);
      setReplyLogs(r.logs);
      setReplyLogsTotal(r.total);
    } catch {}
  }, []);

  const checkVer = useCallback(async () => {
    if (loading) return;
    const params: Record<string, string> = {};
    if (jobStatusFilter) params.status = jobStatusFilter;
    if (jobSearch) params.search = jobSearch;
    try {
      const v = await api.jobsVersion(params);
      const token = `${v.batch_id || ""}|${v.count || 0}|${v.latest_updated_at || ""}`;
      if (token !== lastVer) {
        setLastVer(token);
        setLoading(true);
        try { await loadJobs(); } finally { setLoading(false); }
      }
    } catch {}
  }, [loading, jobStatusFilter, jobSearch, lastVer, loadJobs]);

  const pollAutomation = useCallback(async () => {
    try {
      const d = await api.pollAutomation({ status: "online", running: runningRef.current });
      if (d.batch_id && String(d.batch_id) !== batchId) {
        setBatchId(String(d.batch_id));
        setLastVer("");
        loadJobs();
      }
      const isRunning = !!d.running;
      if (isRunning && !runningRef.current) { runningRef.current = true; }
      if (!isRunning && runningRef.current) {
        runningRef.current = false;
        setTaskStatus("就绪");
        setProgressMsg(d.status === "completed" ? "完成" : "已停止");
        setLastAction(d.status === "completed" ? "完成" : "已停止");
        loadJobs();
      }
      if (d.message) setProgressMsg(String(d.message));
      if (d.last_action) setLastAction(String(d.last_action));
      if (d.progress_pct != null) {
        setPct(d.progress_pct + "%");
        setBarW(Number(d.progress_pct));
      }
      if (d.eta) setEta(String(d.eta));
      if (d.browser_running != null) setBrowserRunning(!!d.browser_running);
      if (d.sent != null) setSent(Number(d.sent));
      if (d.skipped != null) setSkipped(Number(d.skipped));
      if (d.errors != null) setErrors(Number(d.errors));
      if (d.current != null) setCurrent(Number(d.current));
      if (d.total != null) setTotal(Number(d.total));
      if (d.current != null && d.total != null) setCounter(`${d.current} / ${d.total}`);
      try {
        const q = await api.getQuota();
        setQuotaUsed(q.used || 0);
        setQuotaLimit(q.limit || 0);
      } catch {}
      try {
        const rp = await api.replyStatus();
        setReplyRunning(rp.running);
        setReplyCount(rp.replied_count || 0);
      } catch {}
      checkVer();
    } catch {}
  }, [batchId, loadJobs, checkVer]);

    // On mount: restore automation state from backend
  useEffect(() => {
    (async () => {
      try {
        const d = await api.pollAutomation({ status: "online", running: false });
        if (d.running) {
          runningRef.current = true;
          if (pollTimer.current) clearInterval(pollTimer.current);
          pollTimer.current = setInterval(pollAutomation, 1500);
        }
        if (d.browser_running != null) setBrowserRunning(!!d.browser_running);
        if (d.message) setProgressMsg(String(d.message));
        if (d.last_action) setLastAction(String(d.last_action));
        if (d.progress_pct != null) { setPct(d.progress_pct + "%"); setBarW(Number(d.progress_pct)); }
        if (d.eta) setEta(String(d.eta));
        if (d.sent != null) setSent(Number(d.sent));
        if (d.skipped != null) setSkipped(Number(d.skipped));
        if (d.errors != null) setErrors(Number(d.errors));
        if (d.current != null) setCurrent(Number(d.current));
        if (d.total != null) { setTotal(Number(d.total)); setCounter(`${d.current} / ${d.total}`); }
        const q = await api.getQuota();
        setQuotaUsed(q.used || 0);
        setQuotaLimit(q.limit || 0);
        const rp = await api.replyStatus();
        setReplyRunning(rp.running);
        setReplyCount(rp.replied_count || 0);
      } catch {}
    })();
  }, []);

  // Reload data when filters/page change
  useEffect(() => {
    loadJobs();
    loadReplyLogs();
  }, [loadJobs, loadReplyLogs]);

  const toggleBrowser = async () => {
    if (browserRunning) {
      try { await api.stopBrowser(); } catch {}
      setBrowserRunning(false);
    } else {
      try { await api.startBrowser(); setBrowserRunning(true); } catch {}
    }
  };

  const toggleAuto = async () => {
    if (runningRef.current) {
      try {
        await api.stopAuto();
        runningRef.current = false;
        if (pollTimer.current) { clearInterval(pollTimer.current); pollTimer.current = undefined; }
      } catch {}
      setTaskStatus("就绪");
      setLastAction("手动停止");
      return;
    }
    try {
      await api.startAuto({ mode, search_keyword: mode === "search" ? searchKw : undefined });
      runningRef.current = true;
      if (pollTimer.current) clearInterval(pollTimer.current);
      pollTimer.current = setInterval(pollAutomation, 1500);
      pollAutomation();
    } catch (e: unknown) {
      setLastAction("启动失败：" + (e instanceof Error ? e.message : ""));
    }
  };

  const toggleReply = async () => {
    if (replyRunning) {
      try {
        const r = await api.stopReply();
        setReplyRunning(false);
        setLastAction(`自动回复已关闭 (${r.replied_count || 0}条已回复)`);
      } catch {}
    } else {
      try {
        const r = await api.startReply();
        if (r.running || r.status === "already_running") {
          setReplyRunning(true);
          setLastAction(`自动回复已开启 (${r.replied_count || 0}条已回复)`);
        }
      } catch (e: unknown) {
        setLastAction("启动失败：" + (e instanceof Error ? e.message : ""));
      }
    }
  };

  const handleJobSearch = (v: string) => {
    setJobSearch(v);
    clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => { setJobPage(1); }, 400);
  };

  const totalPages = Math.max(1, Math.ceil(jobTotal / jobPageSize));

  return (
    <section className="page active">
      <div className="topbar">
        <div className="topbar-title">
          <h2>自动投递控制台</h2>
          <p>从岗位筛选、AI 评分到个性化开场白发送的全链路控制中心</p>
        </div>
        <div className="topbar-right" />
      </div>

      <div className="section row-auto">
        <div className="card">
          <div className="card-header">
            <h3>自动投递区</h3>
            <p>选择投递来源、关键词与执行动作</p>
          </div>
          <div className="card-body">
            <div className="delivery-grid">
              {(["recommend", "expected", "search"] as Mode[]).map(m => (
                <label key={m} className={`mode-card${mode === m ? " selected" : ""}`} onClick={() => setMode(m)}>
                  <input type="radio" name="pw-mode" checked={mode === m} readOnly />
                  <span className="mode-radio"><span className="mode-radio-inner" /></span>
                  <span className="mode-info">
                    <strong>{m === "recommend" ? "推荐" : m === "expected" ? "岗位推荐" : "搜索框"}</strong>
                    <span>{m === "recommend" ? "使用 BOSS 首页推荐流" : m === "expected" ? "按设置中的目标岗位 + 筛选城市" : "输入关键词后回车搜索"}</span>
                  </span>
                </label>
              ))}
            </div>
            <div className="delivery-actions">
              {mode === "search" && (
                <div className="search-box">
                  <input className="field-input" value={searchKw} onChange={e => setSearchKw(e.target.value)} placeholder="输入搜索关键词…" />
                </div>
              )}
              <button className="btn btn-primary" onClick={toggleBrowser}>
                {browserRunning ? "关闭浏览器" : "启动浏览器"}
              </button>
              <button className="btn btn-success" onClick={toggleAuto} disabled={!browserRunning}>
                {runningRef.current ? "停止投递" : "开始投递"}
              </button>
              <button className="btn btn-accent" onClick={toggleReply}>
                {replyRunning ? "关闭自动回复" : "开启自动回复"}
              </button>
            </div>
          </div>
        </div>

        <div className="progress-card">
          <h3>当前进度</h3>
          <div className="subtitle">{progressMsg}</div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
            <div className="progress-pct">{typeof pct === "number" ? pct + "%" : pct}</div>
            <span style={{ fontSize: 13, opacity: 0.7 }}>{counter}</span>
          </div>
          <div className="progress-bar-wrap">
            <div className="progress-bar-fill" style={{ width: `${barW}%` }} />
          </div>
          <div className="progress-eta">{eta}</div>
          <div className="progress-last">{lastAction}</div>
        </div>
      </div>

      <div className="section row4">
        <Kpi label="浏览器" value={browserRunning ? "已启动" : "未启动"} color="blue" />
        <Kpi label="任务状态" value={taskStatus} color="green" />
        <Kpi label="已发送" value={String(sent)} color="green" />
        <Kpi label="今日额度" value={quotaUsed + " / " + quotaLimit} color="indigo" />
      </div>
      <div className="section row4">
        <Kpi label="已跳过" value={String(skipped)} color="yellow" />
        <Kpi label="错误" value={String(errors)} color="red" />
        <Kpi label="回复" value={String(replyCount)} color="green" />
        <Kpi label="进度" value={current + "/" + total} color="blue" />
      </div>

      <div className="section">
        <div className="card">
          <div className="card-header"><h3>岗位列表</h3><p>AI 评分、跳过原因与自动回复详情，点击行查看</p></div>
          <div className="table-controls">
            <div className="table-controls-left">
              <select className="field-select" value={jobStatusFilter} onChange={e => { setJobStatusFilter(e.target.value); setJobPage(1); }}>
                <option value="">全部状态</option>
                <option value="evaluated">已评分</option>
                <option value="sent">已发送</option>
                <option value="chat_started">已沟通</option>
                <option value="skipped">已跳过</option>
                <option value="error">异常</option>
              </select>
              <input className="field-input" placeholder="搜索岗位名称…" value={jobSearch} onChange={e => handleJobSearch(e.target.value)} style={{ width: 180 }} />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>共 {jobTotal} 条记录</span>
            </div>
            <div className="table-controls-right">
              <button className="pagination-btn" disabled={jobPage <= 1} onClick={() => setJobPage(p => p - 1)}>←</button>
              <span className="pagination-info">{jobPage}/{totalPages}</span>
              <button className="pagination-btn" disabled={jobPage >= totalPages} onClick={() => setJobPage(p => p + 1)}>→</button>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 40, textAlign: "center" }}>#</th>
                  <th style={{ width: 44, textAlign: "center", fontWeight: 700 }}>分</th>
                  <th style={{ width: 80 }}>状态</th>
                  <th>岗位</th>
                  <th style={{ width: 130 }}>公司</th>
                  <th style={{ width: 100 }}>采集时间</th>
                  <th>备注</th>
                </tr>
              </thead>
              <tbody>
                {jobs.length === 0 && (
                  <tr><td colSpan={7} style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>暂无岗位数据</td></tr>
                )}
                {jobs.map((j, i) => {
                  const seq = j.seq ?? (jobTotal - ((jobPage - 1) * jobPageSize + i));
                  const st = j.status || j.decision || "";
                  const tagCls = ["sent", "chat_started"].includes(st) ? "tag-sent" : ["skipped", "skip"].includes(st) ? "tag-skip" : st === "error" ? "tag-err" : "tag-eval";
                  const rawReasons = (j.reasons || []).map(cleanReason).filter(r => r);
                  const base = rawReasons.filter(r => !/^分数\s*\d+\s*低于/.test(r));
                  const scoreLine = rawReasons.filter(r => /^分数\s*\d+\s*低于/.test(r));
                  const reasons = [...base, ...scoreLine].join("；");
                  const risks = (j.risks || []).map(r => "⚠" + r).join("；");
                  const note = [reasons, risks].filter(Boolean).join(" ").slice(0, 200);
                  const ts = j.created_at ? j.created_at.slice(0, 16).replace("T", " ") : "−";
                  return (
                    <tr key={j.id || seq} onClick={() => setSelectedJob(j)}>
                      <td style={{ color: "var(--text-secondary)", fontSize: 12, textAlign: "center" }}>{seq}</td>
                      <td style={{ textAlign: "center", fontWeight: 700 }}>{j.score ?? "−"}</td>
                      <td><span className={`tag ${tagCls}`}>{st || "−"}</span></td>
                      <td>
                        {j.url
                          ? <a href={j.url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} style={{ fontWeight: 600 }}>{(j.title || "岗位").slice(0, 40)}</a>
                          : <span style={{ fontWeight: 600 }}>{(j.title || "岗位").slice(0, 40)}</span>
                        }
                      </td>
                      <td>{j.company || "−"}</td>
                      <td style={{ fontSize: 12, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{ts}</td>
                      <td style={{ fontSize: 12, color: "var(--text-secondary)", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {note || j.initial_message || ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* 自动回复列表 */}
      <div className="section">
        <div className="card">
          <div className="card-header">
            <h3>自动回复列表</h3>
            <p>AI 自动回复的消息历史，按名字-公司-职位组合记录，共 {replyLogsTotal} 条</p>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 50, textAlign: "center" }}>#</th>
                  <th style={{ width: 160 }}>时间</th>
                  <th style={{ width: 200 }}>名字 · 职位</th>
                  <th style={{ width: 350 }}>回复内容</th>
                </tr>
              </thead>
              <tbody>
                {replyLogs.length === 0 && (
                  <tr><td colSpan={4} style={{ textAlign: "center", padding: 24, color: "var(--text-muted)" }}>暂无自动回复记录</td></tr>
                )}
                {replyLogs.map((log, i) => {
                  const ts = log.created_at ? log.created_at.slice(0, 16).replace("T", " ") : "−";
                  return (
                    <tr key={log.id}>
                      <td style={{ color: "var(--text-secondary)", fontSize: 12, textAlign: "center" }}>{replyLogsTotal - i}</td>
                      <td style={{ fontSize: 12, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{ts}</td>
                      <td style={{ fontWeight: 600 }}>
                        {log.job_url 
                          ? <a href={log.job_url} target="_blank" rel="noreferrer">{[log.title, log.company].filter(Boolean).join(" − ") || "−"}</a>
                          : ( [log.title, log.company].filter(Boolean).join(" − ") || "−" )
                        }
                      </td>
                      <td style={{ fontSize: 12, color: "var(--text-secondary)", maxWidth: 350, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{log.message || "−"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <JobDrawer job={selectedJob} onClose={() => setSelectedJob(null)} />
    </section>
  );
}

function Kpi({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="kpi-card">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${color}`}>{value}</div>
    </div>
  );
}
