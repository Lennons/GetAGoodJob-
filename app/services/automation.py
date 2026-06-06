from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import Setting
from app.services.text import now_iso


AUTOMATION_COMMAND_KEY = "automation_command"
AUTOMATION_RUNNER_KEY = "automation_runner"

DEFAULT_AUTOMATION_STATE: dict[str, Any] = {
    "command": None,
    "runner": {
        "last_seen": None,
        "url": "",
        "status": "offline",
        "running": False,
        "queue_count": 0,
    },
}


def get_setting_json(db: Session, key: str, default: Any) -> Any:
    row = db.get(Setting, key)
    if not row:
        if default is None:
            return None
        row = Setting(key=key, value=default)
        db.add(row)
        try:
            db.commit()
            db.refresh(row)
        except IntegrityError:
            db.rollback()
            row = db.get(Setting, key)
    return row.value if row and row.value is not None else default


def set_setting_json(db: Session, key: str, value: Any) -> Any:
    row = db.get(Setting, key)
    if value is None:
        if row:
            db.delete(row)
            db.commit()
        return None
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()
    return value


def get_automation_state(db: Session) -> dict[str, Any]:
    command = get_setting_json(db, AUTOMATION_COMMAND_KEY, None)
    runner = get_setting_json(db, AUTOMATION_RUNNER_KEY, DEFAULT_AUTOMATION_STATE["runner"].copy())

    state = DEFAULT_AUTOMATION_STATE.copy()
    state["command"] = command
    merged_runner = DEFAULT_AUTOMATION_STATE["runner"].copy()
    merged_runner.update(runner or {})
    state["runner"] = merged_runner
    return state


def save_automation_state(db: Session, state: dict[str, Any]) -> dict[str, Any]:
    set_setting_json(db, AUTOMATION_COMMAND_KEY, state.get("command"))
    set_setting_json(db, AUTOMATION_RUNNER_KEY, state.get("runner") or DEFAULT_AUTOMATION_STATE["runner"].copy())
    return state


def issue_automation_command(db: Session, action: str) -> dict[str, Any]:
    command = {
        "id": str(uuid4()),
        "action": action,
        "created_at": now_iso(),
    }
    set_setting_json(db, AUTOMATION_COMMAND_KEY, command)
    return get_automation_state(db)


def update_runner_status(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    runner = get_setting_json(db, AUTOMATION_RUNNER_KEY, DEFAULT_AUTOMATION_STATE["runner"].copy()) or {}
    runner.update(
        {
            "last_seen": now_iso(),
            "url": payload.get("url") or "",
            "status": payload.get("status") or "online",
            "running": bool(payload.get("running", False)),
            "queue_count": int(payload.get("queue_count") or 0),
        }
    )
    set_setting_json(db, AUTOMATION_RUNNER_KEY, runner)
    return get_automation_state(db)
