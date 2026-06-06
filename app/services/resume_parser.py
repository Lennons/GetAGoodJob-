from __future__ import annotations

from pathlib import Path

from docx import Document
from pypdf import PdfReader

from app.services.text import compact_text


def extract_resume_text(path: str, filename: str) -> str:
    ext = Path(filename or path).suffix.lower()

    if ext == ".docx":
        doc = Document(path)
        paragraphs = [paragraph.text for paragraph in doc.paragraphs]
        return compact_text("\n".join(paragraphs), 20000)

    if ext == ".pdf":
        reader = PdfReader(path)
        pages = [page.extract_text() or "" for page in reader.pages]
        return compact_text("\n".join(pages), 20000)

    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    return compact_text(raw, 20000)


def fallback_analyze_resume(text: str) -> dict:
    lowered = text.lower()
    skills = [
        "javascript",
        "typescript",
        "react",
        "vue",
        "node",
        "python",
        "java",
        "go",
        "rust",
        "php",
        "mysql",
        "postgresql",
        "mongodb",
        "redis",
        "docker",
        "kubernetes",
        "aws",
        "阿里云",
        "腾讯云",
        "大模型",
        "机器学习",
        "数据分析",
        "产品",
        "运营",
        "销售",
    ]
    found_skills = [skill for skill in skills if skill.lower() in lowered]
    highlights = [item.strip() for item in compact_text(text, 900).replace("。", "\n").split("\n") if item.strip()]

    return {
        "name": "",
        "target_roles": [],
        "years": None,
        "core_skills": found_skills[:20],
        "industries": [],
        "highlights": highlights[:6],
        "constraints": [],
        "summary": compact_text(text, 280),
    }
