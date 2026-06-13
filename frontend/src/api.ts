import type { JobListResponse, Resume, Settings as SettingsType, BrowserStatus, Quota, Hotword, ReplyStatus, ReplyLogListResponse } from "./types";

const BASE = "";

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export const api = {
  health: () => request<{ status: string }>("/api/health"),
  version: () => request<{ version: string }>("/api/version"),

  // Resumes
  listResumes: () => request<Resume[]>("/api/resumes"),
  uploadResume: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${BASE}/api/resumes/upload`, { method: "POST", body: fd }).then(r => {
      if (!r.ok) throw new Error("Upload failed");
      return r.json() as Promise<Resume>;
    });
  },
  analyzeText: (text: string) =>
    request<Resume>("/api/resumes/text", { method: "POST", body: JSON.stringify({ text, filename: "text-resume" }) }),
  activateResume: (resumeId: string) =>
    request<Resume>(`/api/resumes/${resumeId}/activate`, { method: "POST" }),
  deleteResume: (resumeId: string) =>
    request<void>(`/api/resumes/${resumeId}`, { method: "DELETE" }),

  // Settings
  getSettings: () => request<SettingsType>("/api/settings"),
  saveSettings: (s: Partial<SettingsType>) =>
    request<SettingsType>("/api/settings", { method: "PATCH", body: JSON.stringify(s) }),

  // Jobs
  listJobs: (params: Record<string, string>) => {
    const qs = new URLSearchParams(params).toString();
    return request<JobListResponse>(`/api/jobs?${qs}`);
  },
  jobsVersion: (params: Record<string, string>) => {
    const qs = new URLSearchParams(params).toString();
    return request<{ batch_id: string; count: number; latest_updated_at: string }>(`/api/jobs/version?${qs}`);
  },
  deleteErrorJobs: () => request<{ deleted: number }>("/api/jobs/errors", { method: "DELETE" }),

  // Keywords
  getKeywords: (limit = 100) => request<Hotword[]>(`/api/jobs/keywords?limit=${limit}`),
  analyzeKeywords: () => request<{ ok: boolean }>("/api/jobs/keywords/analyze", { method: "POST" }),

  // Browser
  startBrowser: () => request<BrowserStatus>("/api/setup/launch-browser", { method: "POST" }),
  stopBrowser: () => request<{ ok: boolean }>("/api/setup/stop-browser", { method: "POST" }),
  browserStatus: () => request<BrowserStatus>("/api/setup/browser-status"),

  // Automation
  startAuto: (payload: { mode: string; search_keyword?: string }) =>
    request<{ ok: boolean }>("/api/automation/playwright/start", { method: "POST", body: JSON.stringify(payload) }),
  stopAuto: () => request<{ ok: boolean }>("/api/automation/playwright/stop", { method: "POST" }),

  // Automation poll (original uses this)
  pollAutomation: (payload: { status: string; running: boolean }) =>
    request<Record<string, unknown>>("/api/automation/poll", { method: "POST", body: JSON.stringify(payload) }),
  getQuota: () => request<Quota>("/api/automation/quota"),

  // Reply
  startReply: () => request<ReplyStatus>("/api/reply-monitor/start", { method: "POST" }),
  stopReply: () => request<ReplyStatus>("/api/reply-monitor/stop", { method: "POST" }),
  replyStatus: () => request<ReplyStatus>("/api/reply-monitor/status"),

  // Reply logs
  listReplyLogs: (limit = 50, offset = 0) =>
    request<ReplyLogListResponse>(`/api/reply-logs?limit=${limit}&offset=${offset}`),
  replyLogsCount: () => request<{ total: number }>("/api/reply-logs/count"),
};
