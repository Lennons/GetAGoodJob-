from __future__ import annotations

import asyncio
import json
import threading
import time as time_module
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, init_db
from app.models import Conversation, Event, Job, Resume, Setting
from app.schemas import (
    AutomationPollIn,
    EventIn,
    InitialMessageIn,
    JobEvaluationIn,
    ReplyIn,
    SettingsPatch,
    TextResumeIn,
)
from app.services.automation import (
    get_automation_state,
    issue_automation_command,
    update_runner_status,
)
from app.services.automation_engine import AutomationEngine, get_engine
from app.services.browser_manager import BrowserManager, ensure_browser, get_browser
from app.services.deepseek import (
    analyze_resume,
    evaluate_job,
    generate_initial_message,
    generate_reply,
)
from app.services.resume_parser import extract_resume_text
from app.services.settings import get_app_settings, update_app_settings
from app.services.text import compact_text, normalize_source_key

# ── Constants ──────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIR = ROOT / "uploads"
PUBLIC_DIR = ROOT / "public"
EXTENSION_DIR = ROOT / "extension"
CHROME_PROFILE_DIR = Path.home() / ".boss-chat-assistant-chrome-profile"
BOSS_JOBS_URL = "https://www.zhipin.com/web/geek/jobs?city=101040100"

# ── App ────────────────────────────────────────────

app = FastAPI(title="BOSS Chat Assistant", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/assets", StaticFiles(directory=PUBLIC_DIR), name="assets")


@app.on_event("startup")
def on_startup() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


# ── Shared helpers ─────────────────────────────────

def dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def resume_to_dict(resume: Resume) -> dict[str, Any]:
    return {
        "id": resume.id,
        "filename": resume.filename,
        "analysis": resume.analysis,
        "is_active": resume.is_active,
        "created_at": dt(resume.created_at),
        "updated_at": dt(resume.updated_at),
    }


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "resume_id": job.resume_id,
        "source_key": job.source_key,
        "url": job.url,
        "title": job.title,
        "company": job.company,
        "salary": job.salary,
        "city": job.city,
        "description": job.description,
        "raw": job.raw,
        "score": job.score,
        "decision": job.decision,
        "status": job.status,
        "reasons": job.reasons,
        "risks": job.risks,
        "initial_message": job.initial_message,
        "created_at": dt(job.created_at),
        "updated_at": dt(job.updated_at),
    }


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "payload": event.payload,
        "created_at": dt(event.created_at),
    }


def get_active_resume(db: Session, resume_id: Optional[str] = None) -> Resume:
    if resume_id:
        resume = db.get(Resume, resume_id)
    else:
        settings = get_app_settings(db)
        active_resume_id = settings.get("active_resume_id")
        resume = db.get(Resume, active_resume_id) if active_resume_id else None
        if not resume:
            resume = db.scalar(
                select(Resume).where(Resume.is_active.is_(True)).order_by(desc(Resume.created_at))
            )
    if not resume:
        raise HTTPException(status_code=400, detail="请先上传并激活一份简历")
    return resume


def set_active_resume(db: Session, resume: Resume) -> None:
    db.query(Resume).update({Resume.is_active: False}, synchronize_session=False)
    db.query(Resume).filter(Resume.id == resume.id).update(
        {Resume.is_active: True}, synchronize_session=False
    )
    resume.is_active = True
    settings = get_app_settings(db)
    settings["active_resume_id"] = resume.id
    row = db.get(Setting, "global")
    if row:
        row.value = settings
    else:
        db.add(Setting(key="global", value=settings))


def upsert_job(
    db: Session,
    job_payload: dict[str, Any],
    resume_id: Optional[str] = None,
    evaluation: Optional[dict] = None,
    batch_id: Optional[str] = None,
) -> Job:
    source_key = normalize_source_key(job_payload)
    existing = db.scalar(select(Job).where(Job.source_key == source_key))
    evaluation = evaluation or {}
    values = {
        "resume_id": resume_id,
        "source_key": source_key,
        "url": compact_text(job_payload.get("url"), 1024),
        "title": compact_text(job_payload.get("title"), 255),
        "company": compact_text(job_payload.get("company"), 255),
        "salary": compact_text(job_payload.get("salary"), 255),
        "city": compact_text(job_payload.get("city"), 255),
        "description": compact_text(job_payload.get("description"), 30000),
        "raw": job_payload.get("raw") or job_payload,
        "score": int(evaluation.get("score") or 0),
        "decision": compact_text(evaluation.get("decision") or "review", 32),
        "status": compact_text(evaluation.get("status") or "evaluated", 32),
        "reasons": evaluation.get("reasons") or [],
        "risks": evaluation.get("risks") or [],
        "initial_message": compact_text(evaluation.get("initial_message"), 1000),
    }
    if batch_id:
        values["batch_id"] = batch_id
    if existing:
        if existing.status in {"chat_started", "sent"} and values.get("status") == "evaluated":
            values.pop("status", None)
        for key, value in values.items():
            setattr(existing, key, value)
        # Keep original batch_id if not provided
        if not batch_id and existing.batch_id:
            pass  # don't overwrite existing batch_id
        db.commit()
        db.refresh(existing)
        return existing

    job = Job(**values)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Job).where(Job.source_key == source_key))
        if not existing:
            raise
        return existing
    db.refresh(job)
    return job


# ── Dashboard ──────────────────────────────────────

@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(PUBLIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    bm = BrowserManager.instance()
    return {
        "ok": True,
        "deepseek_configured": bool(settings.deepseek_api_key),
        "model": settings.deepseek_model,
        "browser_running": bm.running,
        "browser_url": bm.page_url if bm.running else "",
    }


# ── Browser (Playwright) ───────────────────────────

@app.post("/api/setup/launch-browser")
async def launch_browser() -> dict[str, Any]:
    """使用 Patchright 启动持久化浏览器。"""
    try:
        bm = await ensure_browser()
        return {
            "ok": True,
            "url": bm.page_url or BOSS_JOBS_URL,
            "profile_dir": str(CHROME_PROFILE_DIR),
            "browser_running": True,
        }
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"patchright 未安装。请执行: pip install patchright && patchright install chrome\n{exc}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"启动浏览器失败：{exc}")


@app.get("/api/setup/browser-status")
async def browser_status() -> dict[str, Any]:
    """获取浏览器状态。"""
    bm = BrowserManager.instance()
    if not bm.running:
        return {"running": False, "url": "", "profile_dir": str(CHROME_PROFILE_DIR)}
    return {
        "running": True,
        "url": bm.page_url or "",
        "profile_dir": str(CHROME_PROFILE_DIR),
    }


@app.post("/api/setup/stop-browser")
async def stop_browser() -> dict[str, Any]:
    """关闭浏览器窗口。"""
    bm = BrowserManager.instance()
    if bm.running:
        await bm.close_browser()
    return {"ok": True}


# ── Automation (Playwright engine) ─────────────────

# In-memory progress store for the automation task
_automation_progress: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "message": "",
    "stats": {"sent": 0, "skipped": 0, "errors": 0, "total": 0},
}


@app.get("/api/automation/playwright/status")
def playwright_automation_status() -> dict[str, Any]:
    """获取 Playwright 自动化进度。"""
    engine = get_engine()
    return {
        "running": engine.running or _automation_progress.get("running", False),
        "status": engine.status or _automation_progress.get("status", "idle"),
        "message": _automation_progress.get("message", ""),
        "stats": engine.stats or _automation_progress.get("stats", {}),
        "batch_id": _automation_progress.get("batch_id", ""),
    }


def _on_automation_progress(progress: dict):
    """进度回调。"""
    _automation_progress["status"] = progress.get("status", "")
    _automation_progress["message"] = progress.get("message", "")
    _automation_progress["stats"] = progress.get("stats", {})


async def _run_automation_task(settings: dict, resume_analysis: dict, mode: str = "expected", search_keyword: str = ""):
    """后台运行自动化任务。"""
    _automation_progress["running"] = True
    _automation_progress["status"] = "starting"
    _automation_progress["message"] = "正在初始化..."

    try:
        # Ensure browser is running
        bm = await ensure_browser()

        # NEVER navigate — use whatever page the user is currently on
        await asyncio.sleep(3)  # Brief wait for any in-flight page render

        # Generate batch ID for this run (jobs show in dashboard by batch)
        batch_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        _automation_progress["batch_id"] = batch_id

        # Get already-processed job URLs for dedup
        from app.database import SessionLocal as _SL
        _db = _SL()
        try:
            already_sent = set(
                row[0] for row in _db.query(Job.source_key)
                .filter(Job.status.in_(["chat_started", "sent"]))
                .all()
            )
        finally:
            _db.close()

        # Run automation engine
        engine = get_engine()
        result = await engine.run(
            settings=settings,
            resume_analysis=resume_analysis,
            on_progress=_on_automation_progress,
            already_sent=already_sent,
            batch_id=batch_id,
            mode=mode,
            search_keyword=search_keyword,
        )
        _automation_progress["message"] = result.get("message", "完成")
        _automation_progress["stats"] = result.get("stats", {})

    except Exception as exc:
        _automation_progress["status"] = "error"
        _automation_progress["message"] = str(exc)
    finally:
        _automation_progress["running"] = False


@app.post("/api/automation/playwright/start")
async def start_playwright_automation(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """启动 Playwright 驱动的全自动投递。"""
    if _automation_progress.get("running"):
        raise HTTPException(status_code=400, detail="自动化任务已在运行中")
    
    mode = payload.get("mode", "expected")
    search_keyword = payload.get("search_keyword", "")

    resume = get_active_resume(db)
    if not resume.analysis:
        raise HTTPException(status_code=400, detail="请先上传并分析简历")

    settings = get_app_settings(db)

    # Check daily quota
    start = datetime.combine(date.today(), time.min)
    used = (
        db.scalar(
            select(func.count(Event.id)).where(
                Event.type == "chat_started", Event.created_at >= start
            )
        )
        or 0
    )
    limit = int(settings.get("daily_chat_limit") or 50)
    if used >= limit:
        raise HTTPException(status_code=400, detail=f"今日开聊额度已用完：{used}/{limit}")

    # Record event
    db.add(Event(type="automation_started", payload={"engine": "playwright", "mode": mode, "search_keyword": search_keyword}))
    db.commit()

    # Start background task
    background_tasks.add_task(_run_automation_task, settings, resume.analysis, mode, search_keyword)

    return {
        "ok": True,
        "message": f"自动化任务已启动，今日已用 {used}/{limit}",
        "quota": {"used": used, "limit": limit, "remaining": limit - used},
    }


@app.post("/api/automation/playwright/stop")
def stop_playwright_automation() -> dict[str, Any]:
    """停止 Playwright 自动化任务。"""
    engine = get_engine()
    engine.stop()
    _automation_progress["running"] = False
    _automation_progress["status"] = "stopped"
    _automation_progress["message"] = "已手动停止"
    return {"ok": True, "message": "已发送停止信号"}


# ── Legacy automation endpoints (extension-based) ──

@app.post("/api/setup/inject-runner")
async def inject_browser_runner() -> dict[str, Any]:
    """注入 content.js 到当前页面（兼容旧扩展模式）。"""
    bm = BrowserManager.instance()
    if not bm.running:
        try:
            bm = await ensure_browser()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"浏览器未运行：{exc}")

    content_js = EXTENSION_DIR / "content.js"
    if not content_js.exists():
        raise HTTPException(status_code=500, detail="content.js 不存在")

    script = content_js.read_text(encoding="utf-8")
    try:
        result = await bm.evaluate(script)
        return {"ok": True, "injected": {"result": result}}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"脚本注入失败：{exc}")


@app.get("/api/automation/quota")
def automation_quota(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_app_settings(db)
    start = datetime.combine(date.today(), time.min)
    used = (
        db.scalar(
            select(func.count(Event.id)).where(
                Event.type == "chat_started", Event.created_at >= start
            )
        )
        or 0
    )
    limit = int(settings.get("daily_chat_limit") or 50)
    return {
        "used": used,
        "limit": limit,
        "remaining": max(limit - used, 0),
        "allowed": used < limit,
    }


@app.get("/api/automation/state")
def automation_state(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_automation_state(db)


@app.post("/api/automation/command/{action}")
def automation_command(action: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if action not in {"run", "sync", "start", "stop", "reply", "reset"}:
        raise HTTPException(status_code=400, detail="不支持的自动化命令")
    state = issue_automation_command(db, action)
    db.add(
        Event(
            type="automation_command",
            payload={"action": action, "command_id": state["command"]["id"]},
        )
    )
    db.commit()
    return state


@app.post("/api/automation/poll")
def automation_poll(payload: AutomationPollIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    state = update_runner_status(db, payload.model_dump())
    command = state.get("command")
    if command and command.get("created_at"):
        created_at = datetime.fromisoformat(command["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created_at).total_seconds() > 300:
            command = None
    if command and command.get("id") == payload.last_command_id:
        command = None
    return {"command": command, "state": state}


# ── Settings ──────────────────────────────────────

@app.get("/api/settings")
def read_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_app_settings(db)


@app.patch("/api/settings")
def patch_settings(payload: SettingsPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    return update_app_settings(db, payload.model_dump(exclude_unset=True))


# ── Resumes ───────────────────────────────────────

@app.post("/api/resumes/upload")
async def upload_resume(
    file: UploadFile = File(...), db: Session = Depends(get_db)
) -> dict[str, Any]:
    suffix = Path(file.filename or "resume.txt").suffix.lower()
    if suffix not in {".txt", ".md", ".pdf", ".docx"}:
        raise HTTPException(status_code=400, detail="仅支持 .txt/.md/.pdf/.docx 简历")

    target = UPLOAD_DIR / f"{uuid4()}{suffix}"
    content = await file.read()
    target.write_bytes(content)
    raw_text = extract_resume_text(str(target), file.filename or target.name)
    settings = get_app_settings(db)
    analysis = await analyze_resume(
        raw_text, model=settings.get("model"), base_url=settings.get("api_base_url")
    )

    has_active = (
        db.scalar(select(func.count(Resume.id)).where(Resume.is_active.is_(True))) > 0
    )
    resume = Resume(
        filename=file.filename or target.name,
        raw_text=raw_text,
        analysis=analysis,
        is_active=not has_active,
    )
    db.add(resume)
    if not has_active:
        db.flush()
        set_active_resume(db, resume)
    db.commit()
    db.refresh(resume)
    return resume_to_dict(resume)


@app.post("/api/resumes/text")
async def create_text_resume(
    payload: TextResumeIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    raw_text = compact_text(payload.text, 20000)
    if not raw_text:
        raise HTTPException(status_code=400, detail="简历文本不能为空")
    settings = get_app_settings(db)
    analysis = await analyze_resume(
        raw_text, model=settings.get("model"), base_url=settings.get("api_base_url")
    )
    has_active = (
        db.scalar(select(func.count(Resume.id)).where(Resume.is_active.is_(True))) > 0
    )
    resume = Resume(
        filename=payload.filename, raw_text=raw_text, analysis=analysis, is_active=not has_active
    )
    db.add(resume)
    if not has_active:
        db.flush()
        set_active_resume(db, resume)
    db.commit()
    db.refresh(resume)
    return resume_to_dict(resume)


@app.get("/api/resumes")
def list_resumes(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    resumes = db.scalars(select(Resume).order_by(desc(Resume.created_at))).all()
    return [resume_to_dict(r) for r in resumes]


@app.get("/api/resumes/active")
def active_resume(db: Session = Depends(get_db)) -> dict[str, Any]:
    return resume_to_dict(get_active_resume(db))


@app.post("/api/resumes/{resume_id}/activate")
def activate_resume(resume_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    resume = db.get(Resume, resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="简历不存在")
    set_active_resume(db, resume)
    db.commit()
    db.refresh(resume)
    return resume_to_dict(resume)


# ── Jobs ──────────────────────────────────────────

@app.post("/api/jobs/evaluate")
async def evaluate_job_endpoint(
    payload: JobEvaluationIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    resume = get_active_resume(db, payload.resume_id)
    settings = get_app_settings(db)
    job_payload = payload.job.model_dump()
    result = payload.evaluation or await evaluate_job(resume.analysis, job_payload, settings)
    batch_id = payload.batch_id or job_payload.get("batch_id", None)
    job = upsert_job(db, job_payload, resume_id=resume.id, evaluation=result, batch_id=batch_id)
    return {"job": job_to_dict(job), "evaluation": result}


@app.get("/api/jobs")
def list_jobs(limit: int = 200, batch_id: Optional[str] = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    q = select(Job).order_by(desc(Job.created_at))
    if batch_id:
        q = q.where(Job.batch_id == batch_id)
    jobs = db.scalars(q.limit(min(limit, 500))).all()
    return [job_to_dict(j) for j in jobs]


@app.get("/api/jobs/version")
def jobs_version(batch_id: Optional[str] = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(func.count(Job.id), func.max(Job.updated_at))
    if batch_id:
        q = q.where(Job.batch_id == batch_id)
    count, latest = db.execute(q).one()
    return {
        "batch_id": batch_id or "",
        "count": int(count or 0),
        "latest_updated_at": dt(latest),
    }


# ── Messages ──────────────────────────────────────

@app.post("/api/messages/initial")
async def initial_message(
    payload: InitialMessageIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    resume = get_active_resume(db, payload.resume_id)
    settings = get_app_settings(db)
    job_payload = payload.job.model_dump()
    result = await generate_initial_message(resume.analysis, job_payload, settings)
    return result


@app.post("/api/messages/reply")
async def reply_message(payload: ReplyIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    resume = get_active_resume(db, payload.resume_id)
    job = db.get(Job, payload.job_id) if payload.job_id else None
    job_payload = (
        payload.job.model_dump()
        if payload.job
        else (job_to_dict(job) if job else None)
    )
    settings = get_app_settings(db)
    message_dicts = [m.model_dump() for m in payload.messages]
    result = await generate_reply(resume.analysis, job_payload, message_dicts, settings)

    conversation = Conversation(
        job_id=job.id if job else None,
        resume_id=resume.id,
        messages=message_dicts,
        action=result.get("action", "reply"),
        ai_reply=compact_text(result.get("message"), 1000),
        need_human=bool(result.get("need_human", False)),
        reason=compact_text(result.get("reason"), 1000),
    )
    db.add(conversation)
    db.commit()
    return result


# ── Events ────────────────────────────────────────

@app.post("/api/events")
def add_event(payload: EventIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    event = Event(type=payload.type, payload=payload.payload)
    db.add(event)
    source_key = payload.payload.get("source_key") or payload.payload.get("sourceKey")
    if payload.type == "chat_started" and source_key:
        job = db.scalar(select(Job).where(Job.source_key == source_key))
        if job:
            job.status = "chat_started"
    if payload.type == "job_skipped" and source_key:
        job = db.scalar(select(Job).where(Job.source_key == source_key))
        if job:
            job.status = "skipped"
    db.commit()
    db.refresh(event)
    return event_to_dict(event)


@app.get("/api/events")
def list_events(limit: int = 200, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    events = db.scalars(
        select(Event).order_by(desc(Event.created_at)).limit(min(limit, 500))
    ).all()
    return [event_to_dict(e) for e in events]


# ── UUID helper ───────────────────────────────────

from uuid import uuid4 as _uuid4


def uuid4() -> str:
    return str(_uuid4())
