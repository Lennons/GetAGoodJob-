from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.services.resume_parser import fallback_analyze_resume
from app.services.text import compact_text, keyword_hits


class DeepSeekClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None):
        settings = get_settings()
        self.api_key = api_key or settings.deepseek_api_key
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
    """Score salary match — returns negative penalty when gap >= 30% of expected."""
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
    gap = min(abs(job_min - exp_max), abs(job_max - exp_min))
    if gap >= exp_min * 0.3:
        return -100
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
    city_matched_cities = []
    if target_cities:
        city_lower = city.lower()
        city_matched_cities = [c for c in target_cities if c and c.lower() in city_lower]
        city_score = min(len(city_matched_cities) * 15, 15)
    else:
        city_score = 10

    # ── Dimension 2: Salary match (0-15) ──────────────
    salary_expectation = settings.get("salary_expectation", "") or resume.get("salary_expectation", "")
    salary_score = _score_salary(salary, salary_expectation)

    # ── Dimension 3: Skill overlap (0-35) ─────────────
    skill_score = min(len(skill_hits) * 7, 35)

    # ── Dimension 4: Role alignment (0-15) ────────────
    target_roles = resume.get("target_roles", [])
    role_score = _score_role(title, target_roles)

    # ── Dimension 5: Industry match (0-10) ────────────
    industry_score = min(len(industry_hits) * 5, 10)

    # ── Dimension 6: Experience fit (0-10) ────────────
    resume_years = resume.get("years", 3) or 3
    experience_score = _score_experience(text, resume_years, resume.get("experience_years"))

    # ── Total ────────────────────────────────────
    score = city_score + salary_score + skill_score + role_score + industry_score + experience_score
    score = min(score, 100)

    # ── Penalty: blocked keywords ──────────────────────
    if blocked_hits:
        score = max(0, score - len(blocked_hits) * 40)
    score = max(0, min(100, score))

    risks = blocked_hits[:]

    # ── Build descriptive reasons ────────────────────
    if city_score >= 15:
        reasons.append(f"城市匹配：{city}（目标城市）")
    elif city_score < 5 and target_cities:
        reasons.append(f"城市不匹配：岗位位于{city}，目标城市为{'/'.join(target_cities[:3])}")

    if salary_score >= 12:
        reasons.append(f"薪资匹配：{salary}")
    elif salary_score < 5 and salary_expectation:
        reasons.append(f"薪资待确认：岗位{salary}，期望{salary_expectation}")

    if skill_hits:
        reasons.append(f"技能覆盖{len(skill_hits)}项（{', '.join(skill_hits[:4])}）")
    elif skill_score < 10:
        reasons.append("技能匹配度低：简历核心技能与JD要求重合较少")

    if role_score >= 12:
        reasons.append(f"岗位方向匹配：{title[:30]}")
    elif role_score < 8:
        reasons.append(f"岗位方向偏差：JD标题{title[:30]}与求职者目标不完全一致")

    if industry_score >= 5:
        reasons.append(f"行业匹配：{', '.join(industry_hits[:3])}")
    elif industry_score == 0:
        reasons.append("行业不匹配：JD行业背景与求职者经验无重合")

    if experience_score >= 8:
        reasons.append(f"经验匹配：求职者{resume_years}年经验符合要求")
    elif experience_score < 5:
        reasons.append(f"经验偏差：求职者{resume_years}年经验与JD要求不完全匹配")

    if city_score < 5:
        reasons.append("城市匹配度低")
    if salary_score < 5:
        reasons.append("薪资匹配度低")

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


async def evaluate_and_extract_keywords(resume: dict, job: dict, settings: dict) -> dict:
    """Single AI call: evaluate job match AND extract skill/tool/knowledge keywords.
    Returns: {"evaluation": {...}, "keywords": [{"word": "...", "category": "..."}]}
    """
    client = DeepSeekClient(api_key=settings.get("api_key"), model=settings.get("model"))
    if not client.configured:
        return {
            "evaluation": fallback_evaluate_job(resume, job, settings),
            "keywords": _fallback_extract_keywords(job.get("description", "")),
        }

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
                "你是一位严谨的求职岗位匹配兼 JD 分析助手。只输出 JSON，不要 Markdown。\n\n"
                "返回字段：\n"
                "score(0-100), decision(chat|skip|review), reasons 数组, risks 数组, "
                "best_resume_angle, initial_message,\n"
                "keywords 数组（每个词含 word 和 category，category 为 skill|tool|knowledge 之一）。\n\n"
                "===== 评分规则（每条理由须具体描述差异，禁止笼统标签）=====\n"
                "从 6 个维度综合考量，每个维度生成1-2条精准且描述性的理由：\n"
                "1. 城市匹配(0-15) 2. 薪资匹配(0-15)\n"
                "3. 技能覆盖(0-35) 4. 岗位匹配(0-15)\n"
                "5. 行业匹配(0-10) 6. 经验匹配(0-10)\n"
                "如果岗位描述命中屏蔽关键词，直接 skip。\n"
                "只有当岗位适合开聊且 score >= min_score_to_chat 时，initial_message 才生成求职者发给招聘方的首句沟通话术；"
                "如果 decision=skip 或 score < min_score_to_chat，initial_message 必须是空字符串。\n"
                "首句要求：中文 120 字以内，先点出匹配点再表达对 JD 理解，自然真诚不模板化，"
                "不用「您好我叫 XXX」格式，不包含电话微信邮箱（除非设置允许）。\n\n"
                "===== reasons 数组要求 =====\n"
                "每条 reason 必须包含具体信息：\n"
                "✓ \"候选人专注于音乐数据与AI应用方向，JD为出海工具产品经理，行业方向差异大\"\n"
                "✓ \"岗位base成都，候选人base重庆，距离近但非完全匹配\"\n"
                "✗ 禁止：\"岗位匹配：产品经理\"、\"经验匹配\" 等笼统标签。\n"
                "reasons 至少写 3 条。即使 decision=skip 也写具体原因。\n\n"
                "===== 关键词提取 =====\n"
                "从岗位描述中（重点关注任职要求部分）提取三类关键词：\n"
                "1. skill: 专业技能（如 产品设计、需求分析、数据分析、用户调研）\n"
                "2. tool: 工具/平台/框架（如 Python、SQL、Figma、Jira、Axure）\n"
                "3. knowledge: 领域知识（如 电商、SaaS、用户增长、AIGC）\n"
                "每个词 2-6 字的中文或英文缩写，去重，最多 20 个词。\n"
                "只提取有意义的技术/工具/知识词，不要提取公司名、地名、福利、学历要求等通用词。\n"
                "即使 decision=skip，也照常提取 keywords。"
            ),
        },
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    try:
        result = await client.chat_json(messages, max_tokens=2400)
        return {
            "evaluation": {
                "score": result.get("score", 0),
                "decision": result.get("decision", "review"),
                "reasons": result.get("reasons", []),
                "risks": result.get("risks", []),
                "best_resume_angle": result.get("best_resume_angle", ""),
                "initial_message": result.get("initial_message", ""),
            },
            "keywords": result.get("keywords", []) or [],
        }
    except Exception:
        return {
            "evaluation": fallback_evaluate_job(resume, job, settings),
            "keywords": _fallback_extract_keywords(job.get("description", "")),
        }


async def generate_initial_message(resume: dict, job: dict, settings: dict) -> dict:
    result = await evaluate_job(resume, job, settings)
    return {"message": compact_text(result.get("initial_message", ""), 500) or build_fallback_initial_message(resume, job)}



async def extract_job_keywords(job_text: str, settings: dict) -> list[dict]:
    """Extract technical skills, tools, and knowledge keywords from a job description using DeepSeek."""
    client = DeepSeekClient(api_key=settings.get("api_key"), model=settings.get("model"))
    if not client.configured:
        return _fallback_extract_keywords(job_text)

    # Focus on 任职要求 section
    text = job_text
    for marker in ["任职要求", "岗位要求", "职位要求"]:
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx:idx + 2000]
            break

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个JD技能分析助手。只输出JSON，不要Markdown。\n"
                "从岗位描述中提取以下三类关键词：\n"
                "1. skill: 专业技能（如 产品设计、需求分析、数据分析、用户调研）\n"
                "2. tool: 工具/平台/框架（如 Python、SQL、Figma、Jira、Axure）\n"
                "3. knowledge: 领域知识（如 电商、SaaS、用户增长、AIGC）\n"
                "每个词都应该是2-6个字的中文或英文缩写，去重。\n"
                "输出格式：{\"keywords\": [{\"word\": \"需求分析\", \"category\": \"skill\"}, ...]}\n"
                "只提取有意义的技术/工具/知识词，不要提取公司名、地名、福利、学历要求等通用词。\n"
                "限制最多输出20个词。"
            ),
        },
        {"role": "user", "content": compact_text(text, 3000)},
    ]
    try:
        result = await client.chat_json(messages, max_tokens=800)
        return result.get("keywords", [])
    except Exception:
        return _fallback_extract_keywords(job_text)


def _fallback_extract_keywords(text: str) -> list[dict]:
    """Fallback: regex-based extraction of potential skill/tool words from 任职要求."""
    SKILL_PATTERNS = [
        r"(产品设计|需求分析|数据分析|用户调研|用户研究|交互设计|原型设计|项目管理|数据分析|竞品分析|用户体验|产品规划|增长策略|产品运营|策略制定|流程优化|文档撰写|测试用例|验收测试|上线发布|版本管理)",
        r"(Python|SQL|Java|JavaScript|TypeScript|Go|Rust|React|Vue|Node|Docker|Kubernetes|Git|Figma|Sketch|Axure|Jira|Confluence|Notion|Excel|PPT|Word)",
        r"(人工智能|机器学习|深度学习|大模型|LLM|AIGC|RAG|Agent|自动化|数字孪生)",
        r"(SaaS|PaaS|IaaS|B端|C端|中台|电商|社交|短视频|直播|游戏|金融|教育|医疗|汽车|云计算|大数据|物联网)",
    ]
    keywords = []
    for marker in ["任职要求", "岗位要求", "职位要求"]:
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx:idx + 2000]
            break
    for pattern in SKILL_PATTERNS:
        for m in re.findall(pattern, text, re.IGNORECASE):
            if len(m) >= 2:
                cat = "skill"
                if any(c in m.lower() for c in ["python", "sql", "java", "figma", "jira", "git", "excel", "ppt"]):
                    cat = "tool"
                elif any(c in m for c in ["saas", "b端", "c端", "电商", "金融", "教育", "医疗"]):
                    cat = "knowledge"
                keywords.append({"word": m, "category": cat})
    return keywords


async def generate_reply(resume: dict, job: Optional[dict], messages_in: list[dict], settings: dict, job_score: int = 0) -> dict:
    client = DeepSeekClient(api_key=settings.get("api_key"), model=settings.get("model"))
    if not client.configured:
        return _fallback_generate_reply(messages_in, job_score)

    # Pre-check: detect boss intent
    last_boss_msgs = [m for m in messages_in[-6:] if m.get("role") == "boss"]
    boss_text = " ".join([(m.get("content", "") or "").lower() for m in last_boss_msgs]) if last_boss_msgs else ""

    asks_resume = any(kw in boss_text for kw in ["简历", "附件", "发一份", "发下", "发一下", "看下简历", "看看简历", "发个简历", "resume", "cv"])
    is_rejection = any(kw in boss_text for kw in ["不合适", "不考虑", "已招到", "不考虑了", "抱歉", "暂时不需要", "不太合适", "不匹配", "不符合", "暂停招聘", "停止招聘", "岗位已关闭", "已结束", "已满", "招到了", "满了", "暂时没有", "不适合", "经验不足", "期望不符"])

    if asks_resume:
        rule_extra = "action=send_resume, 生成一句简短得体的话术增加好感，表达简历已发送、期待后续沟通"
    elif is_rejection and job_score >= 80:
        rule_extra = (
            f"action=rebuttal, 招聘方表达了拒绝，但该岗位评分高达{job_score}分，匹配度很高。"
            "生成一段真诚得体的挽回话术（80-150字）："
            "1. 先简短感谢对方的回复"
            "2. 针对性强调1-2个与岗位高度匹配的亮点（技能/经验，不要编造）"
            "3. 表达对该岗位/公司的认同和热情"
            "4. 委婉请求再考虑一下，语气真诚不卑微"
        )
    else:
        rule_extra = ""

    payload = {
        "resume_analysis": resume,
        "job": job or {},
        "conversation": messages_in[-12:],
        "job_score": job_score,
        "asks_resume": asks_resume,
        "is_rejection": is_rejection,
        "rules": {
            "do_not_fabricate": True,
            "do_not_share_contact_info_unless_allowed": not settings.get("allow_contact_info_in_messages", False),
            "if_uncertain_need_human": True,
        },
    }
    if rule_extra:
        payload["rules"]["special_action"] = rule_extra

    messages = [
        {
            "role": "system",
            "content": (
                "你是求职沟通助手。只输出 JSON，不要 Markdown。"
                "字段：action(reply|wait|decline|send_resume|rebuttal), message, need_human, reason。"
                "如果招聘方索要简历/附件，action=send_resume，message 写成得体的话术（20-60字）。"
                "如果招聘方拒绝但 rules.special_action 要求 rebuttal，action=rebuttal，"
                "message 按 rules.special_action 要求生成80-150字的挽回话术。"
                "不要编造简历没有的信息；涉及薪资、入职时间、证件、隐私、线下面试冲突、收费等敏感内容时，need_human=true。"
                "message 控制在 150 字以内，语气自然。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    return await client.chat_json(messages, max_tokens=1000)


def _fallback_generate_reply(messages_in: list[dict], job_score: int = 0) -> dict:
    """Fallback: detect resume request / rejection without AI."""
    last_boss_msgs = [m for m in messages_in[-6:] if m.get("role") == "boss"]
    boss_text = " ".join([(m.get("content", "") or "").lower() for m in last_boss_msgs]) if last_boss_msgs else ""

    asks_resume = any(kw in boss_text for kw in ["简历", "附件", "发一份", "发下", "发一下", "看下简历", "看看简历", "发个简历", "resume", "cv"])

    if asks_resume:
        return {
            "action": "send_resume",
            "message": "您好，这是我的简历，请您查收。期待跟您进一步沟通！",
            "need_human": False,
            "reason": "fallback: boss asked for resume",
        }

    is_rejection = any(kw in boss_text for kw in ["不合适", "不考虑", "已招到", "不考虑了", "抱歉", "暂时不需要", "不太合适", "不匹配", "不符合", "暂停招聘", "不适合", "经验不足"])
    if is_rejection and job_score >= 80:
        return {
            "action": "rebuttal",
            "message": "感谢您的回复。我对这个岗位非常感兴趣，简历中的核心技能和岗位要求匹配度很高。如果能有机会进一步交流，我非常有信心能胜任。希望您能再考虑一下，谢谢！",
            "need_human": False,
            "reason": "fallback: high-score rejection, rebuttal",
        }

    return {
        "action": "reply",
        "message": "您好，可以的。我这边方便继续沟通，想进一步了解岗位职责、团队情况和面试安排。",
        "need_human": False,
        "reason": "fallback reply",
    }
