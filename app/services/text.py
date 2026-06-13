from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def compact_text(value: Any, limit: int = 12000) -> str:
    text = str(value or "")
    text = text.replace("\r", "\n").replace("\t", " ")
    text = re.sub(r"[ \u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def clean_jd_text(text: str) -> str:
    """Remove BOSS Zhipin UI boilerplate, keeping only actual JD content."""
    if not text:
        return ""

    # ── Phase 1: line-level cleaning ──
    lines = text.split('\n')
    cleaned = []

    junk_exact = {
        "举报", "微信扫码分享", "不合适", "收藏", "立即沟通",
        "去App", "与BOSS随时沟通", "前往App",
        "刚刚活跃", "今日活跃", "本周活跃", "本月活跃", "昨日活跃",
        "3日内活跃", "2日内活跃", "1日内活跃", "今日回复", "本周回复",
        "在线", "离线",
        "试试咨询",
        "点击查看地图", "查看更多信息", "展开全文", "收起", "查看地图",
        "用App聊工作，面试机会翻倍", "打开App", "直聊", "立即开聊",
    }

    addr_re = re.compile(r'[区路楼楼层号栋座]')
    hr_re = re.compile(r'^[\u4e00-\u9fa5]{2,4}$')
    hr_dept_re = re.compile(r'^[\u4e00-\u9fa5a-zA-Z]+\s*[·|]\s*(人事|HR|招聘|经理|主管)$')
    jd_header_re = re.compile(r'^(职位描述|岗位职责|任职要求|职位要求|岗位要求|工作职责|职责描述)[：:]')

    cities = (
        "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆",
        "武汉", "西安", "南京", "天津", "苏州", "长沙", "郑州",
        "东莞", "青岛", "沈阳", "宁波", "昆明", "大连", "厦门",
        "合肥", "佛山", "福州", "哈尔滨", "济南", "温州", "长春",
        "石家庄", "常州", "泉州", "南宁", "贵阳", "南昌", "太原",
        "烟台", "嘉兴", "南通", "金华", "珠海", "惠州", "徐州",
        "海口", "乌鲁木齐", "兰州", "中山", "湖州", "绍兴",
    )

    for line in lines:
        s = line.strip()
        if not s:
            cleaned.append(line)
            continue
        if s in junk_exact:
            continue
        if hr_dept_re.match(s):
            continue
        if hr_re.match(s) and len(s) <= 4:
            continue
        if any(s.startswith(c) for c in cities) and addr_re.search(s):
            continue
        if s == "工作地址":
            continue
        cleaned.append(line)

    text = '\n'.join(cleaned)

    # ── Phase 2: inline cleaning ──
    # Strip BOSS UI tokens
    inline_junk = [
        "举报", "微信扫码分享", "不合适", "收藏", "立即沟通",
        "去App，与BOSS随时沟通", "与BOSS随时沟通", "前往App，与BOSS随时沟通", "前往App",
        "刚刚活跃", "今日活跃", "本周活跃", "本月活跃", "昨日活跃",
        "3日内活跃", "2日内活跃", "1日内活跃", "今日回复", "本周回复",
        "如3日内活跃", "如2日内活跃", "如1日内活跃",
        "试试咨询", "猎头顾问", "猎头",
    ]
    for junk in sorted(inline_junk, key=len, reverse=True):
        text = text.replace(junk, '')

    # Remove "XX · 人事/HR" patterns inline
    text = re.sub(r'[\u4e00-\u9fa5a-zA-Z]{2,20}\s*[·|]\s*(人事|HR|招聘经理|招聘主管)', '', text)

    # Remove standalone 2-3 char Chinese name at very beginning
    text = re.sub(r'^([\u4e00-\u9fa5]{2,3})\s+(?=[\u4e00-\u9fa5])', '', text)

    # Remove address lines inline: "工作地址 XX区XX路..." 
    text = re.sub(r'(工作地址)\s+[\u4e00-\u9fa5a-zA-Z0-9·\-\s]{6,60}(?=\s|$)', '', text)

    # Remove "点击查看地图", "查看更多信息" inline
    text = text.replace('点击查看地图', '').replace('查看更多信息', '')

    # Fix: deduplicate "职位描述 职位描述：" pattern
    text = re.sub(
        r'(职位描述|岗位职责|任职要求|职位要求|岗位要求|工作职责|职责描述)\s+\1[：:]',
        r'\1：',
        text,
    )

    # Remove orphaned " · " or "· " after junk was stripped
    text = re.sub(r'\s*[·|]\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[·|]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+[·|]\s+', ' ', text)
    
    # Clean up: collapse whitespace
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [kw for kw in keywords if kw and kw.lower() in lowered]


def normalize_source_key(job: dict[str, Any]) -> str:
    return compact_text(
        job.get("source_key")
        or job.get("sourceKey")
        or job.get("url")
        or f"{job.get('company', '')}:{job.get('title', '')}:{job.get('salary', '')}",
        512,
    )
