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
