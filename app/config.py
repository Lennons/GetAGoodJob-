from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "127.0.0.1"
    app_port: int = 8788
    database_url: str = Field(
        default="mysql+pymysql://boss_user:boss_password@127.0.0.1:3306/boss_chat_assistant?charset=utf8mb4"
    )
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"


DEFAULT_SETTINGS: dict[str, Any] = {
    "active_resume_id": None,
    "api_key": "",
    "model": "deepseek-v4-flash",
    "auto_send_initial": True,
    "auto_reply": True,
    "daily_chat_limit": 50,
    "cooldown_min_ms": 9000,
    "cooldown_max_ms": 18000,
    "reply_poll_seconds": 8,
    "min_score_to_chat": 55,
    "stop_on_risk_prompt": True,
    "allow_contact_info_in_messages": False,
    "target_cities": [],
    "target_job_keyword": "产品经理",
    "salary_expectation": "",
    "blocked_companies": [],
    "blocked_keywords": ["培训贷", "收费", "押金", "加盟", "纯销售", "电话销售"],
}


@lru_cache
def get_settings() -> Settings:
    return Settings()
