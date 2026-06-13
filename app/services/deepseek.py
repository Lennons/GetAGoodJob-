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
            last_error = None
            for retry in range(3):
                try:
                    response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                    if response.status_code in (429, 502, 503, 504):
                        await asyncio.sleep(2 * (retry + 1))
                        continue
                    response.raise_for_status()
                    body = response.json()
                    break
                except Exception as exc:
                    last_error = exc
                    if retry < 2:
                        await asyncio.sleep(2 * (retry + 1))
            else:
                raise last_error or Exception("DeepSeek API 不可用")

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



def _normalize_salary(raw: str) -> str:
    """统一薪资单位：13K-26K·13薪 → 13000-26000·13薪, 15k→15000."""
    if not raw:
        return ""
    # 分离薪资区间和后缀（如 ·13薪）
    main = raw
    suffix = ""
    m_sep = re.match(r'^(.*?)([·/]\s*.*)$', raw)
    if m_sep:
        main = m_sep.group(1)
        suffix = m_sep.group(2)
    has_k = bool(re.search(r'\d+\s*[kK]', main))
    cleaned = re.sub(r'[kK]', '', main)
    nums = [int(n) for n in re.findall(r'\d+', cleaned)]
    if not nums:
        return raw
    if has_k and max(nums) < 1000:
        cleaned = re.sub(r'(\d+)', lambda m: str(int(m.group(1)) * 1000), cleaned)
    return cleaned + suffix


def _score_salary(job_salary: str, expected: str, ratio: float = 0.7) -> int:
    """Score salary match — returns negative penalty when job_max < exp_min * ratio."""
    if not expected or not job_salary:
        return 8  # no data, neutral

    # 统一单位（15k→15000、20000 保持不变）
    expected = _normalize_salary(expected)
    job_salary = _normalize_salary(job_salary)

    exp_nums = [int(n) for n in re.findall(r'\d+', expected)]
    job_nums = [int(n) for n in re.findall(r'\d+', job_salary)]

    if not exp_nums or not job_nums:
        return 8

    exp_min = min(exp_nums)
    exp_max = max(exp_nums)
    job_min = min(job_nums)
    job_max = max(job_nums)

    # 岗位最高薪资 < 期望最低 × ratio → 直接跳过
    if job_max < exp_min * ratio:
        return -100
    # 有区间重叠 → 满分
    if job_min <= exp_max and job_max >= exp_min:
        return 15
    # 岗位最高 ≥ 期望最低×ratio 但最低 < 期望最低 → 降分
    if job_min < exp_min:
        return 5
    return 12



def _apply_salary_penalty(evaluation: dict, job: dict, settings: dict, font_url: str = "") -> dict:
    """Post-process: if job_max < exp_min * ratio, force skip."""
    salary_expectation = settings.get("salary_expectation", "") or ""
    if not salary_expectation:
        # 即使无薪资期望也要尝试提取 salary_display
        evaluation["salary_display"] = _normalize_salary(str(job.get("salary", ""))) or evaluation.get("job_salary", "")
        return evaluation

    ratio = float(settings.get("salary_intercept_ratio", 0.7))
    job_salary = str(job.get("salary", ""))
    salary_score = _score_salary(job_salary, salary_expectation, ratio)

    # BOSS 字体混淆：salary 字段里的数字被替换成私有区 Unicode，\d+ 匹配不到。
    # 如果直接解析失败（得分 8 表示无数据），从 AI 评分理由里兜底提取岗位薪资数字。
    if salary_score == 8:
        for reason in evaluation.get("reasons", []):
            if job_salary := _extract_job_salary_from_reason(reason):
                salary_score = _score_salary(job_salary, salary_expectation, ratio)
                break

    # 提取可读薪资用于前端展示（优先级：AI直接返回 > 解析salary > AI理由提取）
    display_salary = evaluation.get("job_salary", "")
    if not display_salary or not re.search(r'\d+', display_salary):
        display_salary = _normalize_salary(str(job.get("salary", "")))
    if not display_salary or not re.search(r'\d+', display_salary):
        display_salary = job_salary if salary_score != 8 else ""
    if not display_salary:
        for reason in evaluation.get("reasons", []):
            if v := _extract_job_salary_from_reason(reason):
                display_salary = v
                break
    # 最后尝试 BOSS 字体解码
    if not display_salary and font_url:
        display_salary = _decode_boss_salary(job_salary, font_url)
    evaluation["salary_display"] = display_salary

    if salary_score < 0:
        evaluation["score"] = 0
        evaluation["decision"] = "skip"
        reasons = list(evaluation.get("reasons", []))
        reasons.append(f"薪资硬拦截：岗位{job_salary}，期望{salary_expectation}×{ratio}，差距过大直接跳过")
        evaluation["reasons"] = reasons
    return evaluation


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
    salary_ratio = float(settings.get("salary_intercept_ratio", 0.7))
    salary_score = _score_salary(salary, salary_expectation, salary_ratio)

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

    return _apply_salary_penalty({
        "score": score,
        "decision": decision,
        "reasons": reasons,
        "risks": risks,
        "best_resume_angle": resume.get("summary", "")[:140],
        "score_detail": _score_detail,
        "initial_message": build_fallback_initial_message(resume, job) if decision == "chat" else "",
    }, job, settings)


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
    result = await client.chat_json(messages, max_tokens=1200)
    return _apply_salary_penalty(result, job, settings)


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
        evaluation = {
            "score": result.get("score", 0),
            "decision": result.get("decision", "review"),
            "reasons": result.get("reasons", []),
            "risks": result.get("risks", []),
            "best_resume_angle": result.get("best_resume_angle", ""),
            "initial_message": result.get("initial_message", ""),
        }
        evaluation = _apply_salary_penalty(evaluation, job, settings)
        return {
            "evaluation": evaluation,
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

    # All intent analysis goes through AI — no keyword pre-checks
    last_boss_msgs = [m for m in messages_in[-6:] if m.get("role") == "boss"]
    boss_text = " ".join([(m.get("content", "") or "").lower() for m in last_boss_msgs]) if last_boss_msgs else ""

    payload = {
        "resume_analysis": resume,
        "job": job or {},
        "conversation": messages_in[-12:],
        "job_score": job_score,
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
                "你是求职沟通助手，帮助候选人与招聘方进行自然、有针对性的一对一沟通。只输出 JSON，不要 Markdown。\n\n"
                "字段：action(reply|wait|decline|send_resume|rebuttal), message, need_human, reason。\n\n"
                "=== 意图判断（由你全权决定 action） ===\n"
                "1. 对方明确索要简历/作品/附件（如「发一份简历」「发下简历」「简历发我看下」）→ action=send_resume\n"
                "2. 对方明确拒绝（「不合适」「不考虑」等）且 job_score >= 80 → action=rebuttal 挽回\n"
                "3. 对方说「审核后联系你」「会把简历推荐给部门」「HR后续联系」「通过后通知你」等自己处理简历的话 → 不是索要简历！action=reply，简短感谢\n"
                "4. 对方介绍薪资/工时/福利/公司背景/项目等 → action=reply，结合自己对行业和岗位的真实认知来回应\n"
                "5. 对方问事实类问题 → action=reply，从 resume_analysis 找对应信息诚实回答\n"
                "6. 对方问看法/想法类问题 → action=reply，用自己的专业判断回答\n"
                "7. 闲聊/寒暄 → action=reply，自然友好\n"
                "8. 系统消息/自动回复 → action=wait\n\n"
                "=== 回复原则 ===\n"
                "- 每个回复都要看对方说了什么，结合 context（简历、岗位、对话）给出有针对性的回答\n"
                "- 介绍薪资待遇时 → 简短确认收到，可结合自己对市场行情的认知（「这个范围和市场水平差不多」「了解了，感谢具体介绍」），不评价高低\n"
                "- 介绍公司背景时 → 如有了解可说一两句，没了解也可以说听起来不错\n"
                "- 介绍上班时间时 → 简短确认，可以说「作息挺合理的」之类自然的评价\n"
                "- 简历有的信息善用；简历没的信息诚实说不知道\n"
                "- 语气真诚自然，像真人在聊天，不死板不模板化\n"
                "- 不要以「您好，我叫XXX」开场\n"
                "- message 控制在 30-120 字\n\n"
                "=== 敏感内容 ===\n"
                "涉及入职时间、证件、隐私、线下面试、收费等 → need_human=true"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    return await client.chat_json(messages, max_tokens=1000)


def _fallback_generate_reply(messages_in: list[dict], job_score: int = 0) -> dict:
    """Fallback: detect resume request / rejection without AI."""
    last_boss_msgs = [m for m in messages_in[-6:] if m.get("role") == "boss"]
    boss_text = " ".join([(m.get("content", "") or "").lower() for m in last_boss_msgs]) if last_boss_msgs else ""

    # Boss is asking for resume? Exclude cases where boss says they'll handle/forward it
    asks_keywords = ["发一份", "发下简历", "发一下简历", "发个简历", "看下简历", "看看简历", "发我简历", "给个简历"]
    asks_resume = any(kw in boss_text for kw in asks_keywords) or (
        any(kw in boss_text for kw in ["简历", "附件", "resume", "cv"]) and
        not any(kw in boss_text for kw in ["发您的", "把您的", "将简历", "把简历", "您的简历已", "审核通过", "收到您的简历", "已收到", "收到简历", "看过您的", "看了您的", "转发给", "推给", "推荐给", "发给", "提交给", "上传到"])
    )

    if asks_resume:
        return {
            "action": "send_resume",
            "message": "您好，这是我的简历，请您查收。简历中有相关项目经验，方便的话可以安排面试进一步沟通，面试才能真正了解一个人。",
            "need_human": False,
            "reason": "fallback: boss asked for resume + interview pitch",
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
