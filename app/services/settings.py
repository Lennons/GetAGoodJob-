from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import DEFAULT_SETTINGS
from app.models import Setting


SETTINGS_KEY = "global"


def get_app_settings(db: Session) -> dict[str, Any]:
    row = db.get(Setting, SETTINGS_KEY)
    if not row:
        row = Setting(key=SETTINGS_KEY, value=DEFAULT_SETTINGS.copy())
        db.add(row)
        db.commit()
        db.refresh(row)
    merged = DEFAULT_SETTINGS.copy()
    merged.update(row.value or {})
    return merged


def update_app_settings(db: Session, patch: dict[str, Any]) -> dict[str, Any]:
    clean_patch = {key: value for key, value in patch.items() if value is not None and key in DEFAULT_SETTINGS}
    current = get_app_settings(db)
    current.update(clean_patch)
    row = db.get(Setting, SETTINGS_KEY)
    if not row:
        row = Setting(key=SETTINGS_KEY, value=current)
        db.add(row)
    else:
        row.value = current
    db.commit()
    db.refresh(row)
    return current
