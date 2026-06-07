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


async def analyze_resume(text: str, *, model: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    client = DeepSeekClient(api_key=api_key, model=model)
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



def _score_salary(job_salary: str, expected: str) -> int:
    """Score salary match (0-15)."""
    if not expected or not job_salary:
        return 8  # no data, neutral

    exp_nums = [int(n) for n in re.findall(r'\d+', expected)]
    job_nums = [int(n) for n in re.findall(r'\d+', job_salary)]

    if not exp_nums or not job_nums:
        return 8

    exp_min = min(exp_nums)
    exp_max = max(exp_nums)
    job_min = min(job_nums)
    job_max = max(job_nums)

    # Overlap check
    if job_min <= exp_max and job_max >= exp_min:
        return 15
    # Close: within 30% above or below
    gap = min(abs(job_min - exp_max), abs(job_max - exp_min))
    if gap <= exp_min * 0.3:
        return 10
    if gap <= exp_min * 0.5:
        return 5
    return 2


def _score_role(job_title: str, target_roles: list[str]) -> int:
    """Score how well the job title matches target roles (0-15)."""
    if not target_roles:
        return 8
    title_lower = job_title.lower()
    for role in target_roles:
        if role and role.lower() in title_lower:
            return 15
    # Partial match: check word overlap
    title_words = set(job_title)
    for role in target_roles:
        if role:
            role_words = set(role)
            overlap = len(title_words & role_words) / max(len(role_words), 1)
            if overlap >= 0.5:
                return 10
    return 3


def _score_experience(job_text: str, resume_years: int, extra_years=None) -> int:
    """Score experience level match (0-10)."""
    years = extra_years if extra_years else resume_years
    if not years:
        return 5

    # Try to find experience requirement in JD
    exp_patterns = [
        r'(\d+)[-~](\d+)年',
        r'(\d+)年以上',
        r'经验(\d+)[-~](\d+)年',
        r'(\d+)[-~](\d+)岁',
    ]
    for pat in exp_patterns:
        m = re.search(pat, job_text)
        if m:
            req_max = int(m.group(2)) if m.lastindex >= 2 else int(m.group(1)) + 3
            req_min = int(m.group(1))
            if req_min <= years <= req_max + 3:
                return 10
            if years >= req_max:
                return 8
            if years >= req_min - 1:
                return 6
            return 3
    return 5  # no experience requirement found, neutral



def fallback_evaluate_job(resume: dict, job: dict, settings: dict) -> dict:
    text = " ".join([
        str(job.get("title", "")),
        str(job.get("company", "")),
        str(job.get("salary", "")),
        str(job.get("city", "")),
        str(job.get("description", "")),
    ])
    title = str(job.get("title", ""))
    city = str(job.get("city", ""))
    salary = str(job.get("salary", ""))

    blocked_hits = keyword_hits(text, settings.get("blocked_keywords", []))
    skill_hits = keyword_hits(text, resume.get("core_skills", []))
    industry_hits = keyword_hits(text, resume.get("industries", []))
    reasons = []

    # ── Dimension 1: City match (0-15) ─────────────────
    target_cities = settings.get("target_cities", [])
    city_score = 0
    if target_cities:
        city_lower = city.lower()
        matched = [c for c in target_cities if c and c.lower() in city_lower]
        city_score = min(len(matched) * 15, 15)
    else:
        city_score = 10  # no preference set, give moderate
    if city_score >= 15:
        reasons.append(f"城市匹配：{city}")

    # ── Dimension 2: Salary match (0-15) ──────────────
    salary_expectation = settings.get("salary_expectation", "") or resume.get("salary_expectation", "")
    salary_score = _score_salary(salary, salary_expectation)
    if salary_score >= 10:
        reasons.append(f"薪资匹配：{salary}")

    # ── Dimension 3: Skill overlap (0-35) ─────────────
    skill_score = min(len(skill_hits) * 7, 35)
    if skill_hits:
        reasons.append(f"技能匹配：{', '.join(skill_hits[:6])} (+{skill_score}分)")

    # ── Dimension 4: Role alignment (0-15) ────────────
    target_roles = resume.get("target_roles", [])
    role_score = _score_role(title, target_roles)
    if role_score >= 10:
        reasons.append(f"岗位匹配：{title[:20]}")

    # ── Dimension 5: Industry match (0-10) ────────────
    industry_score = min(len(industry_hits) * 5, 10)
    if industry_score:
        reasons.append(f"行业匹配：{', '.join(industry_hits[:3])}")

    # ── Dimension 6: Experience fit (0-10) ────────────
    experience_score = _score_experience(text, resume.get("years", 3), resume.get("experience_years"))
    if experience_score >= 8:
        reasons.append("经验匹配")

    # ── Total ────────────────────────────────────
    score = city_score + salary_score + skill_score + role_score + industry_score + experience_score
    score = min(score, 100)

    # ── Penalty: blocked keywords ──────────────────────
    if blocked_hits:
        score = max(0, score - len(blocked_hits) * 40)
    score = max(0, min(100, score))

    risks = blocked_hits[:]
    if city_score < 5:
        reasons.append("城市可能不匹配")
    if salary_score < 5:
        reasons.append("薪资待确认")

    decision = "chat" if score >= int(settings.get("min_score_to_chat", 72)) and not blocked_hits else "skip"
    if not reasons:
        reasons.append("信息不足，建议人工复核")

    _score_detail = {
        "city": city_score, "salary": salary_score, "skill": skill_score,
        "role": role_score, "industry": industry_score, "experience": experience_score,
    }

    return {
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "risks": risks,
        "best_resume_angle": resume.get("summary", "")[:140],
        "score_detail": _score_detail,
        "initial_message": build_fallback_initial_message(resume, job) if decision == "chat" else "",
    }


def build_fallback_initial_message(resume: dict, job: dict) -> str:
    title = compact_text(job.get("title", ""), 40) or "这个岗位"
    skills = "、".join((resume.get("core_skills") or [])[:4])
    if skills:
        return f"您好，我对{title}很感兴趣。我过往经验和{skills}相关，想进一步了解岗位职责和面试安排，方便的话期待沟通。"
    return f"您好，我对{title}很感兴趣，想进一步了解岗位职责和团队情况，方便的话期待沟通。"


async def evaluate_job(resume: dict, job: dict, settings: dict) -> dict:
    client = DeepSeekClient(api_key=settings.get("api_key"), model=settings.get("model"))
    if not client.configured:
        return fallback_evaluate_job(resume, job, settings)

    prompt = {
        "resume_analysis": resume,
        "job": job,
        "settings": {
            "target_cities": settings.get("target_cities", []),
            "salary_expectation": settings.get("salary_expectation", ""),
            "blocked_keywords": settings.get("blocked_keywords", []),
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
                "评分从 6 个维度综合考量：\n"
                "1. 城市匹配(0-15) 2. 薪资匹配(0-15)\n"
                "3. 技能覆盖(0-35) 4. 岗位匹配(0-15)\n"
                "5. 行业匹配(0-10) 6. 经验匹配(0-10)\n"
                "如果岗位描述命中屏蔽关键词，直接 skip。"
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
    client = DeepSeekClient(api_key=settings.get("api_key"), model=settings.get("model"))
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
