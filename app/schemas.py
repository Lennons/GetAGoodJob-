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
    api_base_url: Optional[str] = None
    model: Optional[str] = None
    auto_send_initial: Optional[bool] = None
    auto_reply: Optional[bool] = None
    daily_chat_limit: Optional[int] = None
    cooldown_min_ms: Optional[int] = None
    cooldown_max_ms: Optional[int] = None
    min_score_to_chat: Optional[int] = None
    stop_on_risk_prompt: Optional[bool] = None
    allow_contact_info_in_messages: Optional[bool] = None
    target_cities: Optional[list[str]] = None
    target_city: Optional[str] = None
    target_job_keyword: Optional[str] = None
    target_roles: Optional[list[str]] = None
    salary_expectation: Optional[str] = None
    blocked_companies: Optional[list[str]] = None
    blocked_keywords: Optional[list[str]] = None
    preferred_keywords: Optional[list[str]] = None


class TextResumeIn(BaseModel):
    text: str
    filename: str = "pasted-resume.txt"


class EventIn(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AutomationPollIn(BaseModel):
    last_command_id: Optional[str] = None
    url: str = ""
    status: str = "online"
    running: bool = False
    queue_count: int = 0
