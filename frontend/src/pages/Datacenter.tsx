import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Hotword } from "../types";

const catColors: Record<string, string> = { skill: "#DBEAFE", tool: "#D1FAE5", knowledge: "#FEF3C7" };
const catLabels: Record<string, string> = { skill: "技能", tool: "工具", knowledge: "知识" };

export default function Datacenter() {
  const [scanned, setScanned] = useState(0);
  const [sent, setSent] = useState(0);
  const [skipped, setSkipped] = useState(0);
  const [errors, setErrors] = useState(0);
  const [hotwords, setHotwords] = useState<Hotword[] | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const inited = useRef(false);

  useEffect(() => {
    (async () => {
      try {
        const [all, cs, st, sk, er] = await Promise.all([
          api.listJobs({ limit: "1" }),
          api.listJobs({ limit: "1", status: "chat_started" }),
          api.listJobs({ limit: "1", status: "sent" }),
          api.listJobs({ limit: "1", status: "skipped" }),
          api.listJobs({ limit: "1", status: "error" }),
        ]);
        setScanned(all.total);
        setSent((cs.total || 0) + (st.total || 0));
        setSkipped(sk.total || 0);
        setErrors(er.total || 0);
      } catch {}
    })();
    loadHotwords();
  }, []);

  const loadHotwords = async () => {
    try {
      let kw = await api.getKeywords(100);
      if (!kw?.length && !inited.current) {
        inited.current = true;
        setAnalyzing(true);
        try { await api.analyzeKeywords(); kw = await api.getKeywords(100); } catch { kw = []; }
        setAnalyzing(false);
      }
      inited.current = true;
      setHotwords(kw);
    } catch { setHotwords([]); }
  };

  return (
    <section className="page active">
      <div className="topbar">
        <div className="topbar-title">
          <h2>数据中心</h2>
          <p>投递数据总览与 JD 高频技能词分析</p>
        </div>
        <div className="topbar-right" />
      </div>

      <div className="section row4">
        <Kpi label="已扫描" value={String(scanned)} color="blue" />
        <Kpi label="已发送" value={String(sent)} color="green" />
        <Kpi label="已跳过" value={String(skipped)} color="yellow" />
        <Kpi label="异常" value={String(errors)} color="red" />
      </div>

      <div className="section">
        <div className="card">
          <div className="card-header">
            <h3>热词分析</h3>
            <p>AI 从任职要求中提取技术/工具/知识高频词，点击按钮批量分析</p>
          </div>
          <div className="card-body" style={{ padding: "20px 24px" }}>
            <div className="hotwords-cloud">
              {analyzing && <span style={{ color: "var(--text-muted)", fontSize: 13 }}>正在初始化热词分析…</span>}
              {!analyzing && hotwords === null && <span style={{ color: "var(--text-muted)", fontSize: 13 }}>加载中…</span>}
              {!analyzing && hotwords && hotwords.length === 0 && <span style={{ color: "var(--text-muted)", fontSize: 13 }}>暂无热词数据，投递扫描后自动生成</span>}
              {!analyzing && hotwords && hotwords.length > 0 && (() => {
                const mx = hotwords[0].count;
                return hotwords.map(k => {
                  const sz = 14 + Math.round((k.count / mx) * 20);
                  const bg = catColors[k.category] || "#EFF6FF";
                  return (
                    <span key={k.word} className="hotwords-tag" style={{ fontSize: sz, background: bg }}>
                      {k.word}
                      <span style={{ fontSize: 9, color: "var(--text-muted)", marginLeft: 4 }}>{k.count}</span>
                      <span style={{ fontSize: 8, background: "var(--border-light)", padding: "1px 5px", borderRadius: 4, marginLeft: 4 }}>{catLabels[k.category] || "技能"}</span>
                    </span>
                  );
                });
              })()}
            </div>
          </div>
        </div>
      </div>
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
