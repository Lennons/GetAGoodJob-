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
from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, init_db
from app.models import Conversation, Event, Job, Resume, Setting
from app.schemas import (
    EventIn,
    InitialMessageIn,
    JobEvaluationIn,
    ReplyIn,
    SettingsPatch,
    TextResumeIn,
)
from app.services.automation_engine import AutomationEngine, get_engine
from app.services.browser_manager import BrowserManager, ensure_browser, get_browser
from app.services.reply_monitor import get_reply_monitor

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

app.mount("/assets", StaticFiles(directory=PUBLIC_DIR / "assets"), name="assets")


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
        "file_path": resume.file_path,
        "analysis": resume.analysis,
        "is_active": resume.is_active,
        "created_at": dt(resume.created_at),
        "updated_at": dt(resume.updated_at),
    }


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "seq": job.seq,
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
        # Already sent — don't downgrade status, just refresh metadata
        if existing.status in {"chat_started", "sent"} and values.get("status") == "evaluated":
            values.pop("status", None)
            for key, value in values.items():
                setattr(existing, key, value)
            db.commit()
            db.refresh(existing)
            return existing
        # Previously skipped but now passes — update in place (no duplicate)
        if existing.status == "skipped" and values.get("status") not in ("skipped",):
            pass  # fall through to generic update below
        # Generic update in place
        for key, value in values.items():
            setattr(existing, key, value)
        if not batch_id and existing.batch_id:
            pass
        db.commit()
        db.refresh(existing)
        return existing

    # Auto-assign sequence number for new jobs
    max_seq = db.scalar(select(func.max(Job.seq))) or 0
    values["seq"] = max_seq + 1
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
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    bm = BrowserManager.instance()
    app_settings = get_app_settings(db)
    return {
        "ok": True,
        "deepseek_configured": bool(settings.deepseek_api_key or app_settings.get("api_key")),
        "model": settings.deepseek_model,
        "browser_running": bm.running,
        "browser_url": bm.page_url if bm.running else "",
    }


@app.get("/api/version")
def app_version() -> dict[str, Any]:
    return {"version": "1.1.1"}


# ── Browser (Playwright) ───────────────────────────

@app.post("/api/setup/launch-browser")
async def launch_browser() -> dict[str, Any]:
    """使用 Patchright 启动持久化浏览器。"""
    try:
        bm = await ensure_browser()
        return {
            "ok": True,
            "url": bm.page_url or BOSS_JOBS_URL,
            "profile_dir": str(Path.home() / ".boss-chat-assistant-chrome-data"),
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
        return {"running": False, "url": "", "profile_dir": str(Path.home() / ".boss-chat-assistant-chrome-data")}
    return {
        "running": True,
        "url": bm.page_url or "",
        "profile_dir": str(Path.home() / ".boss-chat-assistant-chrome-data"),
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

_reply_monitor_task: asyncio.Task | None = None


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

        # Reply monitor is manually started via /api/reply-monitor/start button
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
        # Don't stop reply monitor — it's a persistent background service
        # if monitor_task and not monitor_task.done():
        #     get_reply_monitor().stop()
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
@app.post("/api/automation/poll")
def automation_poll() -> dict[str, Any]:
    """返回 Playwright 自动化进度，供前端轮询。"""
    from app.services.browser_manager import get_browser
    bm = get_browser()
    engine = get_engine()
    stats = engine.stats or _automation_progress.get("stats", {})

    return {
        "running": engine.running or _automation_progress.get("running", False),
        "status": engine.status or _automation_progress.get("status", "idle"),
        "message": _automation_progress.get("message", ""),
        "last_action": _automation_progress.get("message", ""),
        "batch_id": _automation_progress.get("batch_id", ""),
        "browser_running": bm.running,
        "browser_url": bm.page_url if bm.running else "",
        "sent": stats.get("sent", 0),
        "skipped": stats.get("skipped", 0),
        "errors": stats.get("errors", 0),
        "current": stats.get("sent", 0) + stats.get("skipped", 0) + stats.get("errors", 0),
        "total": stats.get("total", 0),
        "progress_pct": round((stats.get("sent", 0) + stats.get("skipped", 0) + stats.get("errors", 0)) / max(stats.get("total", 1), 1) * 100),
        "eta": _automation_progress.get("eta", ""),
    }


# ── Reply Monitor (persistent background) ─────────

@app.post("/api/reply-monitor/start")
async def start_reply_monitor(db: Session = Depends(get_db)):
    global _reply_monitor_task
    monitor = get_reply_monitor()
    if monitor.running:
        return {"ok": True, "status": "already_running", "replied_count": monitor.replied_count}

    bm = get_browser()
    if not bm.running:
        raise HTTPException(status_code=400, detail="浏览器未启动，请先在浏览器中打开BOSS直聘并确保插件连接")

    settings = get_app_settings(db)
    resume = get_active_resume(db)
    if not resume.analysis:
        raise HTTPException(status_code=400, detail="请先上传并分析简历")

    if _reply_monitor_task and not _reply_monitor_task.done():
        _reply_monitor_task.cancel()

    _reply_monitor_task = asyncio.create_task(monitor.start(settings, resume.analysis))
    await asyncio.sleep(1)  # Let it start
    return {"ok": True, "status": monitor.status, "replied_count": monitor.replied_count}


@app.post("/api/reply-monitor/stop")
async def stop_reply_monitor():
    global _reply_monitor_task
    monitor = get_reply_monitor()
    monitor.stop()
    if _reply_monitor_task and not _reply_monitor_task.done():
        _reply_monitor_task.cancel()
        try:
            await asyncio.wait_for(_reply_monitor_task, timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    return {"ok": True, "status": monitor.status, "replied_count": monitor.replied_count}


@app.get("/api/reply-monitor/status")
async def reply_monitor_status():
    monitor = get_reply_monitor()
    return {
        "running": monitor.running,
        "status": monitor.status,
        "replied_count": monitor.replied_count,
    }


# ── Reply Logs ────────────────────────────────────

@app.post("/api/reply-logs")
def create_reply_log(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    """记录一条自动回复。"""
    from app.models import AutoReplyLog
    log = AutoReplyLog(
        contact_name=payload.get("contact_name", ""),
        company=payload.get("company", ""),
        title=payload.get("title", ""),
        role=payload.get("role", ""),
        message=payload.get("message", ""),
        conversation_id=payload.get("conversation_id"),
        job_url=payload.get("job_url", ""),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return {
        "id": log.id,
        "company": log.company,
        "title": log.title,
        "role": log.role,
        "message": log.message,
        "job_url": log.job_url,
        "created_at": dt(log.created_at),
    }


@app.get("/api/reply-logs")
def list_reply_logs(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)) -> dict[str, Any]:
    """获取自动回复日志列表。"""
    from app.models import AutoReplyLog
    total = db.scalar(select(func.count(AutoReplyLog.id))) or 0
    logs = db.scalars(
        select(AutoReplyLog).order_by(desc(AutoReplyLog.id)).offset(offset).limit(min(limit, 200))
    ).all()
    items = [
        {
            "id": log.id,
            "contact_name": log.contact_name,
            "company": log.company,
            "title": log.title,
            "role": log.role,
            "message": log.message,
            "job_url": log.job_url,
            "created_at": dt(log.created_at),
        } for log in logs
    ]
    return {"total": total, "logs": items}


@app.get("/api/reply-logs/count")
def reply_logs_count(db: Session = Depends(get_db)) -> dict[str, Any]:
    """获取自动回复总数。"""
    from app.models import AutoReplyLog
    count = db.scalar(select(func.count(AutoReplyLog.id))) or 0
    return {"total": count}


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
        raw_text, model=settings.get("model"), api_key=settings.get("api_key")
    )

    has_active = (
        db.scalar(select(func.count(Resume.id)).where(Resume.is_active.is_(True))) > 0
    )
    resume = Resume(
        filename=file.filename or target.name,
        file_path=str(target),
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
        raw_text, model=settings.get("model"), api_key=settings.get("api_key")
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


@app.delete("/api/resumes/{resume_id}")
def delete_resume(resume_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    resume = db.get(Resume, resume_id)
    if not resume:
        raise HTTPException(status_code=404, detail="简历不存在")
    # Clear FK references from jobs and conversations using raw SQL
    db.execute(text("UPDATE jobs SET resume_id = NULL WHERE resume_id = :rid"), {"rid": resume_id})
    db.execute(text("UPDATE conversations SET resume_id = NULL WHERE resume_id = :rid"), {"rid": resume_id})
    db.flush()
    # If this was the active resume, clear it from settings
    settings = get_app_settings(db)
    if settings.get("active_resume_id") == resume_id:
        settings["active_resume_id"] = None
        row = db.get(Setting, "global")
        if row:
            row.value = settings
    db.delete(resume)
    db.commit()
    return {"deleted": True, "id": resume_id}

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
def list_jobs(limit: int = 20, offset: int = 0, status: Optional[str] = None, batch_id: Optional[str] = None, search: Optional[str] = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(Job).order_by(desc(Job.seq))
    if batch_id:
        q = q.where(Job.batch_id == batch_id)
    if status:
        q = q.where(Job.status == status)
    if search:
        pattern = f"%{search}%"
        q = q.where((Job.title.ilike(pattern)) | (Job.company.ilike(pattern)))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    jobs = db.scalars(q.offset(offset).limit(min(limit, 500))).all()
    return {"jobs": [job_to_dict(j) for j in jobs], "total": total, "offset": offset, "limit": limit}


@app.post("/api/jobs/lookup")
def lookup_job(payload: dict, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Look up a job by source_key and return score + basic info."""
    source_key = payload.get("source_key", "")
    if not source_key:
        return {}
    job = db.scalar(select(Job).where(Job.source_key == source_key))
    if not job:
        return {}
    return job_to_dict(job)

@app.get("/api/jobs/keywords")
def job_keywords(limit: int = 30, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Return hot keywords aggregated from the job_keywords table."""
    from app.models import JobKeyword

    # Count word frequencies
    rows = db.execute(
        select(JobKeyword.word, func.count(JobKeyword.id).label("cnt"))
        .group_by(JobKeyword.word)
        .order_by(desc("cnt"))
        .limit(limit)
    ).all()

    if not rows:
        return []

    max_count = rows[0][1] if rows else 1
    # Get sample category for each word
    result = []
    for word, cnt in rows:
        sample = db.scalar(
            select(JobKeyword.category)
            .where(JobKeyword.word == word)
            .limit(1)
        ) or "skill"
        result.append({
            "word": word,
            "count": cnt,
            "category": sample,
            "weight": round(cnt / max_count, 2),
        })
    return result


@app.post("/api/jobs/keywords/analyze")
async def analyze_job_keywords(db: Session = Depends(get_db)):
    """Trigger keyword extraction for all analysable jobs."""
    from app.models import JobKeyword
    from app.services.deepseek import extract_job_keywords
    from app.services.settings import get_app_settings

    settings = get_app_settings(db)

    # Find jobs that have descriptions but haven't been analyzed yet
    from sqlalchemy import text as _text
    existing = db.execute(_text(
        "SELECT DISTINCT job_id FROM job_keywords"
    )).fetchall()
    analyzed_ids = {r[0] for r in existing}

    jobs = db.scalars(
        select(Job).where(
            Job.description != None,
            Job.description != "",
            Job.id.notin_(analyzed_ids) if analyzed_ids else True,
        )
    ).all()

    if not jobs:
        return {"analyzed": 0, "keywords_added": 0, "message": "所有岗位已分析完毕"}

    total_added = 0
    for job in jobs:
        try:
            text = job.description or ""
            if not text:
                continue
            keywords = await extract_job_keywords(text, settings)
            for kw in keywords:
                db.add(JobKeyword(
                    job_id=job.id,
                    word=kw.get("word", ""),
                    category=kw.get("category", "skill"),
                ))
                total_added += 1
        except Exception:
            continue

    db.commit()
    return {
        "analyzed": len(jobs),
        "keywords_added": total_added,
    }


@app.post("/api/jobs/keywords/extract/{job_id}")
async def extract_single_job_keywords(job_id: str, db: Session = Depends(get_db)):
    """Extract and store keywords for a single job."""
    from app.models import JobKeyword
    from app.services.deepseek import extract_job_keywords
    from app.services.settings import get_app_settings

    job = db.get(Job, job_id)
    if not job or not job.description:
        return {"job_id": job_id, "keywords": []}

    # Delete old keywords for this job
    from sqlalchemy import delete as _delete
    db.execute(_delete(JobKeyword).where(JobKeyword.job_id == job_id))
    db.flush()

    settings = get_app_settings(db)
    text = job.description or ""
    keywords = await extract_job_keywords(text, settings)

    added = []
    for kw in keywords:
        entry = JobKeyword(
            job_id=job_id,
            word=kw.get("word", ""),
            category=kw.get("category", "skill"),
        )
        db.add(entry)
        added.append({"word": kw.get("word"), "category": kw.get("category")})

    db.commit()
    return {"job_id": job_id, "keywords": added}


@app.post("/api/jobs/keywords/analyze-by-source")
async def analyze_by_source(payload: dict, db: Session = Depends(get_db)):
    """Trigger keyword extraction for a job by source_key.
    If `keywords` is provided in payload, store them directly without calling AI."""
    from app.models import JobKeyword
    from app.services.deepseek import extract_job_keywords
    from app.services.settings import get_app_settings

    source_key = payload.get("source_key", "")
    if not source_key:
        return {"ok": False, "reason": "no source_key"}

    job = db.scalar(select(Job).where(Job.source_key == source_key))
    if not job or not job.description:
        return {"ok": False, "reason": "job not found"}

    # Check if already analyzed
    existing = db.scalar(select(JobKeyword).where(JobKeyword.job_id == job.id).limit(1))
    if existing:
        return {"ok": True, "reason": "already analyzed", "job_id": job.id}

    # Use pre-extracted keywords if provided, otherwise call AI
    pre_keywords = payload.get("keywords")
    if pre_keywords and isinstance(pre_keywords, list):
        keywords = pre_keywords
    else:
        settings = get_app_settings(db)
        keywords = await extract_job_keywords(job.description, settings)

    for kw in keywords:
        db.add(JobKeyword(
            job_id=job.id,
            word=kw.get("word", ""),
            category=kw.get("category", "skill"),
        ))

    db.commit()
    return {"ok": True, "job_id": job.id, "keywords_count": len(keywords)}

@app.delete("/api/jobs/errors")
def delete_error_jobs(db: Session = Depends(get_db)) -> dict[str, Any]:
    from sqlalchemy import delete
    stmt = delete(Job).where(Job.status == "error")
    result = db.execute(stmt)
    db.commit()
    return {"deleted": result.rowcount}

@app.delete("/api/jobs")
def delete_jobs_by_status(status: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from sqlalchemy import delete
    stmt = delete(Job).where(Job.status == status)
    result = db.execute(stmt)
    db.commit()
    return {"deleted": result.rowcount, "status": status}

@app.get("/api/jobs/version")
def jobs_version(status: Optional[str] = None, batch_id: Optional[str] = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(func.count(Job.id), func.max(Job.updated_at))
    if batch_id:
        q = q.where(Job.batch_id == batch_id)
    if status:
        q = q.where(Job.status == status)
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
