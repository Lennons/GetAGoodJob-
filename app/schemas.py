from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class JobIn(BaseModel):
    source_key: Optional[str] = None
    url: str = ""
    title: str = ""
    company: str = ""
    salary: str = ""
    city: str = ""
    description: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class JobEvaluationIn(BaseModel):
    job: JobIn
    resume_id: Optional[str] = None
    batch_id: Optional[str] = None
    evaluation: Optional[dict[str, Any]] = None


class InitialMessageIn(BaseModel):
    job: JobIn
    resume_id: Optional[str] = None


class ChatMessage(BaseModel):
    role: Literal["boss", "user", "assistant", "system"]
    content: str


class ReplyIn(BaseModel):
    job: Optional[JobIn] = None
    job_id: Optional[str] = None
    resume_id: Optional[str] = None
    messages: list[ChatMessage]


class SettingsPatch(BaseModel):
    active_resume_id: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    auto_send_initial: Optional[bool] = None
    daily_chat_limit: Optional[int] = None
    cooldown_min_ms: Optional[int] = None
    cooldown_max_ms: Optional[int] = None
    reply_poll_seconds: Optional[int] = None
    min_score_to_chat: Optional[int] = None
    stop_on_risk_prompt: Optional[bool] = None
    deep_delivery: Optional[bool] = None
    allow_contact_info_in_messages: Optional[bool] = None
    target_cities: Optional[list[str]] = None
    filter_city: Optional[str] = None
    target_job_keyword: Optional[str] = None
    salary_expectation: Optional[str] = None
    salary_intercept_ratio: Optional[float] = None
    blocked_companies: Optional[list[str]] = None
    blocked_keywords: Optional[list[str]] = None


class TextResumeIn(BaseModel):
    text: str
    filename: str = "pasted-resume.txt"


class EventIn(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AutoReplyLogOut(BaseModel):
    id: int
    contact_name: str = ""
    company: str = ""
    title: str = ""
    message: str = ""
    created_at: Optional[str] = None
