export interface Resume {
  id: string;
  filename: string;
  file_path: string;
  analysis: {
    name?: string;
    experience_years?: number;
    current_role?: string;
    salary_expectation?: string;
    salary_intercept_ratio?: number;
    core_skills?: string[];
    summary?: string;
  } | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Job {
  id: string;
  seq: number;
  resume_id: string;
  source_key: string;
  url: string;
  title: string;
  company: string;
  salary: string;
  city: string;
  description: string;
  raw: Record<string, unknown>;
  score: number;
  decision: string;
  status: string;
  reasons: string[];
  risks: string[];
  initial_message: string;
  created_at: string;
  updated_at: string;
}

export interface JobListResponse {
  jobs: Job[];
  total: number;
  offset: number;
  limit: number;
}

export interface Settings {
  [key: string]: string | number | boolean;
  api_key: string;
  model: string;
  daily_chat_limit: number;
  cooldown_min_ms: number;
  cooldown_max_ms: number;
  reply_poll_seconds: number;
  min_score_to_chat: number;
  target_job_keyword: string;
  target_cities: string;
  filter_city: string;
  blocked_keywords: string;
  auto_send_initial: boolean;
  stop_on_risk_prompt: boolean;
  deep_delivery: boolean;
  allow_contact_info_in_messages: boolean;
}

export interface AutomatonStatus {
  running: boolean;
  status: string;
  message: string;
  stats: { sent: number; skipped: number; errors: number; total: number };
  batch_id: string;
}

export interface ReplyStatus {
  running: boolean;
  status: string;
  replied_count: number;
}

export interface ReplyLog {
  id: number;
  contact_name: string;
  company: string;
  title: string;
  role: string;
  message: string;
  job_url: string;
  created_at: string;
}

export interface ReplyLogListResponse {
  total: number;
  logs: ReplyLog[];
}

export interface Hotword {
  word: string;
  count: number;
  category: string;
}

export interface Quota {
  used: number;
  limit: number;
  remaining: number;
}

export interface BrowserStatus {
  running: boolean;
  url: string;
  profile_dir: string;
}

export type PageName = "dashboard" | "settings" | "datacenter";
