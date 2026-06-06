from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.services.resume_parser import fallback_analyze_resume
from app.services.text import compact_text, keyword_hits


class DeepSeekClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.deepseek_api_key
        self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self.model = model or settings.deepseek_model

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def chat_json(self, messages: list[dict[str, str]], *, model: Optional[str] = None, max_tokens: int = 1200) -> dict:
        if not self.configured:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")

        payload = {
            "model": model or self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()

        content = body["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start : end + 1])
            raise


async def analyze_resume(text: str, *, model: Optional[str] = None, base_url: Optional[str] = None) -> dict:
    client = DeepSeekClient(base_url=base_url, model=model)
    if not client.configured:
        return fallback_analyze_resume(text)

    messages = [
        {
            "role": "system",
            "content": (
                "你是求职简历分析助手。只输出 JSON，不要输出 Markdown。"
                "不要编造简历中没有的信息。字段：name,target_roles,years,core_skills,industries,"
                "highlights,constraints,summary。"
            ),
        },
        {"role": "user", "content": f"请分析这份简历：\n{compact_text(text, 16000)}"},
    ]
    return await client.chat_json(messages, model=model, max_tokens=1400)


def fallback_evaluate_job(resume: dict, job: dict, settings: dict) -> dict:
    text = " ".join(
        [
            str(job.get("title", "")),
            str(job.get("company", "")),
            str(job.get("salary", "")),
            str(job.get("city", "")),
            str(job.get("description", "")),
        ]
    )
    blocked_hits = keyword_hits(text, settings.get("blocked_keywords", []))
    preferred_hits = keyword_hits(text, settings.get("preferred_keywords", []))
    skill_hits = keyword_hits(text, resume.get("core_skills", []))

    score = 45 + min(len(skill_hits) * 9, 30) + min(len(preferred_hits) * 5, 15) - len(blocked_hits) * 20
    score = max(0, min(100, score))
    decision = "chat" if score >= int(settings.get("min_score_to_chat", 72)) and not blocked_hits else "skip"
    reasons = []
    if skill_hits:
        reasons.append(f"技能匹配：{', '.join(skill_hits[:6])}")
    if preferred_hits:
        reasons.append(f"命中偏好：{', '.join(preferred_hits[:6])}")
    if not reasons:
        reasons.append("信息不足，建议人工复核")

    return {
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "risks": blocked_hits,
        "best_resume_angle": resume.get("summary", "")[:140],
        "initial_message": build_fallback_initial_message(resume, job) if decision == "chat" else "",
    }


def build_fallback_initial_message(resume: dict, job: dict) -> str:
    title = compact_text(job.get("title", ""), 40) or "这个岗位"
    skills = "、".join((resume.get("core_skills") or [])[:4])
    if skills:
        return f"您好，我对{title}很感兴趣。我过往经验和{skills}相关，想进一步了解岗位职责和面试安排，方便的话期待沟通。"
    return f"您好，我对{title}很感兴趣，想进一步了解岗位职责和团队情况，方便的话期待沟通。"


async def evaluate_job(resume: dict, job: dict, settings: dict) -> dict:
    client = DeepSeekClient(base_url=settings.get("api_base_url"), model=settings.get("model"))
    if not client.configured:
        return fallback_evaluate_job(resume, job, settings)

    prompt = {
        "resume_analysis": resume,
        "job": job,
        "settings": {
            "target_cities": settings.get("target_cities", []),
            "target_roles": settings.get("target_roles", []),
            "salary_expectation": settings.get("salary_expectation", ""),
            "blocked_keywords": settings.get("blocked_keywords", []),
            "preferred_keywords": settings.get("preferred_keywords", []),
            "min_score_to_chat": settings.get("min_score_to_chat", 72),
            "allow_contact_info_in_messages": settings.get("allow_contact_info_in_messages", False),
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是严谨的求职岗位匹配助手。只输出 JSON，不要 Markdown。"
                "字段：score(0-100),decision(chat|skip|review),reasons数组,risks数组,"
                "best_resume_angle,initial_message。"
                "只有当岗位适合开聊且 score >= min_score_to_chat 时，initial_message 才生成求职者发给招聘方的首句沟通话术；"
                "如果 decision=skip 或 score < min_score_to_chat，initial_message 必须是空字符串。"
                "首句要求：\n"
                "1. 中文，120字以内\n"
                "2. 先点出自己与岗位的匹配点（技能/经验，1-2句即可）\n"
                "3. 再表达对该岗位要求的理解或看法（说明你认真看过JD，展示专业度）\n"
                "4. 语气自然真诚，像真人写的，不要模板化\n"
                "5. 不要用「您好，我叫XXX」格式，直接用内容开场\n"
                "6. 不包含电话、微信、邮箱，除非设置允许"
            ),
        },
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    return await client.chat_json(messages, max_tokens=1200)


async def generate_initial_message(resume: dict, job: dict, settings: dict) -> dict:
    result = await evaluate_job(resume, job, settings)
    return {"message": compact_text(result.get("initial_message", ""), 500) or build_fallback_initial_message(resume, job)}


async def generate_reply(resume: dict, job: Optional[dict], messages_in: list[dict], settings: dict) -> dict:
    client = DeepSeekClient(base_url=settings.get("api_base_url"), model=settings.get("model"))
    if not client.configured:
        return {
            "action": "reply",
            "message": "您好，可以的。我这边方便继续沟通，想进一步了解岗位职责、团队情况和面试安排。",
            "need_human": False,
            "reason": "fallback reply",
        }

    payload = {
        "resume_analysis": resume,
        "job": job or {},
        "conversation": messages_in[-12:],
        "rules": {
            "do_not_fabricate": True,
            "do_not_share_contact_info_unless_allowed": not settings.get("allow_contact_info_in_messages", False),
            "if_uncertain_need_human": True,
        },
    }
    messages = [
        {
            "role": "system",
            "content": (
                "你是求职沟通助手。只输出 JSON，不要 Markdown。"
                "根据招聘方最新回复，生成求职者回复。字段：action(reply|wait|decline),"
                "message,need_human,reason。"
                "不要编造简历没有的信息；涉及薪资、入职时间、证件、隐私、线下面试冲突、收费等敏感内容时，need_human=true。"
                "message 控制在 120 字以内，语气自然。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    return await client.chat_json(messages, max_tokens=900)
