import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Resume, Settings as SettingsType } from "../types";

const DEFAULT: SettingsType = {
  api_key: "", model: "deepseek-v4-flash", daily_chat_limit: 80,
  cooldown_min_ms: 15000, cooldown_max_ms: 30000, reply_poll_seconds: 8,
  min_score_to_chat: 30, target_job_keyword: "产品经理",
  target_cities: "", filter_city: "重庆", blocked_keywords: "",
  auto_send_initial: true, stop_on_risk_prompt: true,
  deep_delivery: false, allow_contact_info_in_messages: false,
};

export default function SettingsPage() {
  const [s, setS] = useState<SettingsType>(DEFAULT);
  const [resumes, setResumes] = useState<Resume[]>([]);
  const [resumeText, setResumeText] = useState("");
  const [saving, setSaving] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.getSettings().then(setS).catch(() => {});
    api.listResumes().then(setResumes).catch(() => {});
  }, []);

  const update = (k: string, v: string | number | boolean) => setS(prev => ({ ...prev, [k]: v }));

  const save = async () => {
    setSaving(true);
    try { await api.saveSettings(s); alert("设置已保存"); }
    catch (e: unknown) { alert("保存失败: " + (e instanceof Error ? e.message : "")); }
    finally { setSaving(false); }
  };

  const uploadResume = async (e: React.FormEvent) => {
    e.preventDefault();
    const f = fileRef.current?.files?.[0]; if (!f) return;
    try { const r = await api.uploadResume(f); setResumes(prev => [r, ...prev]); }
    catch (err: unknown) { alert("上传失败: " + (err instanceof Error ? err.message : "")); }
  };

  const analyzeText = async () => {
    if (!resumeText.trim()) return;
    try { const r = await api.analyzeText(resumeText); setResumes(prev => [r, ...prev]); setResumeText(""); }
    catch (err: unknown) { alert("分析失败: " + (err instanceof Error ? err.message : "")); }
  };

  const activate = async (id: string) => {
    try { await api.activateResume(id); setResumes(prev => prev.map(r => ({ ...r, is_active: r.id === id }))); }
    catch (err: unknown) { alert("激活失败: " + (err instanceof Error ? err.message : "")); }
  };

  const del = async (id: string) => {
    if (!confirm("确定要删除该简历吗？")) return;
    try { await api.deleteResume(id); setResumes(prev => prev.filter(r => r.id !== id)); }
    catch (err: unknown) { alert("删除失败: " + (err instanceof Error ? err.message : "")); }
  };

  const active = resumes.find(r => r.is_active);

  return (
    <section className="page active">
      <div className="topbar">
        <div className="topbar-title">
          <h2>设置中心</h2>
          <p>API、自动回复、关键词和简历管理</p>
        </div>
        <div className="topbar-right">
          <button className="btn btn-primary" onClick={save} disabled={saving}>{saving ? "保存中…" : "保存设置"}</button>
        </div>
      </div>

      <div className="section">
        <div className="card">
          <div className="card-header">
            <h3>API 与自动化设置</h3>
            <p>DeepSeek API、投递间隔、回复监控</p>
          </div>
          <div className="card-body">
            <div className="settings-grid">
              <Field label="API Key"><input className="field-input" type="password" value={s.api_key} onChange={e => update("api_key", e.target.value)} /></Field>
              <Field label="模型">
                <select className="field-select" value={s.model} onChange={e => update("model", e.target.value)}>
                  <option value="deepseek-v4-flash">deepseek-v4-flash</option>
                  <option value="deepseek-v4-pro">deepseek-v4-pro</option>
                </select>
              </Field>
              <Field label="每日沟通上限"><input className="field-input" type="number" min={1} max={500} value={s.daily_chat_limit} onChange={e => update("daily_chat_limit", Number(e.target.value))} /></Field>
              <Field label="期望薪资"><input className="field-input" placeholder="例：15k-25k 或 15000-25000" value={(s.salary_expectation as string) || ''} onChange={e => setS(prev => ({ ...prev, salary_expectation: e.target.value }))} /></Field>
              <Field label="最低开聊分数线"><input className="field-input" type="number" min={0} max={100} value={s.min_score_to_chat} onChange={e => update("min_score_to_chat", Number(e.target.value))} /></Field>
              <Field label="最短发送间隔 (ms)"><input className="field-input" type="number" min={3000} step={1000} value={s.cooldown_min_ms} onChange={e => update("cooldown_min_ms", Number(e.target.value))} /></Field>
              <Field label="最长发送间隔 (ms)"><input className="field-input" type="number" min={5000} step={1000} value={s.cooldown_max_ms} onChange={e => update("cooldown_max_ms", Number(e.target.value))} /></Field>
              <Field label="回复扫描间隔 (秒)"><input className="field-input" type="number" min={3} max={120} step={1} value={s.reply_poll_seconds} onChange={e => update("reply_poll_seconds", Number(e.target.value))} /></Field>
            </div>
            <div className="field-row">
              <Toggle label="自动发送首句" checked={s.auto_send_initial} onChange={v => update("auto_send_initial", v)} />
              <Toggle label="遇到风控停止" checked={s.stop_on_risk_prompt} onChange={v => update("stop_on_risk_prompt", v)} />
              <Toggle label="深度投递" checked={s.deep_delivery} onChange={v => update("deep_delivery", v)} />
              <Toggle label="允许联系方式" checked={s.allow_contact_info_in_messages} onChange={v => update("allow_contact_info_in_messages", v)} />
            </div>
          </div>
        </div>
      </div>

      <div className="section">
        <div className="card">
          <div className="card-header">
            <h3>关键词设置</h3>
            <p>目标岗位、目标城市、筛选城市与屏蔽词</p>
          </div>
          <div className="card-body">
            <div className="settings-grid">
              <Field label="目标岗位"><input className="field-input" placeholder="产品经理" value={s.target_job_keyword} onChange={e => update("target_job_keyword", e.target.value)} /></Field>
              <div className="field span2"><span className="field-label">目标城市</span><input className="field-input" value={s.target_cities} onChange={e => update("target_cities", e.target.value)} /></div>
              <Field label="筛选城市"><input className="field-input" placeholder="重庆" value={s.filter_city} onChange={e => update("filter_city", e.target.value)} /></Field>
              <div className="field span2"><span className="field-label">屏蔽关键词</span><input className="field-input" value={s.blocked_keywords} onChange={e => update("blocked_keywords", e.target.value)} /></div>
            </div>
          </div>
        </div>
      </div>

      <div className="section">
        <div className="card">
          <div className="card-header">
            <h3>简历管理</h3>
            <p>上传 PDF / DOCX / TXT / MD，也可粘贴文本，AI 自动解析</p>
          </div>
          <div className="card-body">
            <div className="resume-upload-row">
              <form className="upload-row" onSubmit={uploadResume}>
                <input ref={fileRef} type="file" accept=".txt,.md,.pdf,.docx" className="field-input" />
                <button type="submit" className="btn btn-primary btn-sm">上传并 AI 分析</button>
              </form>
            </div>
            <textarea className="field-input field-textarea" rows={3} placeholder="或直接粘贴简历文本…" value={resumeText} onChange={e => setResumeText(e.target.value)} />
            <div style={{ marginTop: 8 }}><button className="btn btn-ghost btn-sm" onClick={analyzeText}>分析文本</button></div>
            <div className="resume-list">
              {resumes.map(r => {
                const name = r.analysis?.name || r.filename || "未命名";
                const skills = (r.analysis?.core_skills || []).slice(0, 4).join("、");
                return (
                  <div key={r.id} className="resume-card">
                    <span className="rname">{name}{r.is_active ? <span className="rtag">当前</span> : ""}</span>
                    <span className="rskills">{skills}</span>
                    <button className="btn btn-ghost btn-sm" onClick={() => del(r.id)}>删除</button>
                    <button className="btn btn-ghost btn-sm" onClick={() => activate(r.id)} disabled={r.is_active}>{r.is_active ? "已设" : "启用"}</button>
                  </div>
                );
              })}
              {resumes.length === 0 && <div style={{ color: "var(--text-muted)", fontSize: 13, padding: "12px 0" }}>暂无简历</div>}
            </div>
            {active?.analysis && (
              <div className="resume-detail">
                <strong>当前简历：{active.analysis.name || active.filename}</strong><br />
                状态：已解析 · 最近用于岗位评分<br />
                {active.analysis.name && <>姓名：{active.analysis.name}<br /></>}
                {active.analysis.experience_years != null && <>经验：{active.analysis.experience_years} 年<br /></>}
                {active.analysis.current_role && <>当前角色：{active.analysis.current_role}<br /></>}
                {active.analysis.salary_expectation && <>期望薪资：{active.analysis.salary_expectation}<br /></>}
                {active.analysis.core_skills?.length ? <>核心技能：{active.analysis.core_skills.join("、")}<br /></> : ""}
                {active.analysis.summary && <>摘要：{active.analysis.summary}</>}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="field"><span className="field-label">{label}</span>{children}</div>;
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return <label className="toggle"><input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />{label}</label>;
}
