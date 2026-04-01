import json
from datetime import datetime
from typing import Optional

from telegram.ext import Application

from config import STATE_FILE

sessions: dict = {}
seen_tx_ids_cache: set[str] = set()


def format_dt(ts: Optional[int] = None) -> str:
    if ts is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def build_new_session() -> dict:
    return {
        "active": True,
        "started_at_raw": datetime.now(),
        "started_at": format_dt(),
        "buyins": [],
        "pending": {},
        "rejected": [],
        "edit_state": None,
        "ending_mode": False,
        "stack_entry_order": [],
        "stack_entry_index": 0,
        "final_stacks": {},
        "results": [],
        "stack_edit_target": None,
    }


def serialize_session(session: dict) -> dict:
    serialized = dict(session)
    started_at_raw = serialized.get("started_at_raw")
    if isinstance(started_at_raw, datetime):
        serialized["started_at_raw"] = started_at_raw.isoformat()
    return serialized


def deserialize_session(raw_session: dict) -> dict:
    session = build_new_session()
    session.update(raw_session)

    started_at_raw = session.get("started_at_raw")
    if isinstance(started_at_raw, str):
        try:
            session["started_at_raw"] = datetime.fromisoformat(started_at_raw)
        except ValueError:
            session["started_at_raw"] = datetime.now()
    elif not isinstance(started_at_raw, datetime):
        session["started_at_raw"] = datetime.now()

    session["pending"] = {
        str(tx_id): pending
        for tx_id, pending in (session.get("pending") or {}).items()
    }
    session["buyins"] = list(session.get("buyins") or [])
    session["rejected"] = list(session.get("rejected") or [])
    session["stack_entry_order"] = list(session.get("stack_entry_order") or [])
    session["final_stacks"] = dict(session.get("final_stacks") or {})
    session["results"] = list(session.get("results") or [])
    return session


def persist_runtime_state(application: Optional[Application] = None) -> None:
    global seen_tx_ids_cache

    if application is not None:
        seen_ids = application.bot_data.get("seen_tx_ids", set())
        seen_tx_ids_cache = {str(tx_id) for tx_id in seen_ids if tx_id}

    if not sessions and not seen_tx_ids_cache:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return

    payload = {
        "sessions": {
            str(chat_id): serialize_session(session)
            for chat_id, session in sessions.items()
        },
        "seen_tx_ids": sorted(seen_tx_ids_cache),
    }
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_runtime_state() -> None:
    global seen_tx_ids_cache

    sessions.clear()
    seen_tx_ids_cache = set()

    if not STATE_FILE.exists():
        return

    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return

    raw_sessions = payload.get("sessions", {})
    if isinstance(raw_sessions, dict):
        for chat_id, raw_session in raw_sessions.items():
            try:
                sessions[int(chat_id)] = deserialize_session(raw_session)
            except Exception:
                continue

    seen_tx_ids_cache = {
        str(tx_id) for tx_id in payload.get("seen_tx_ids", []) if tx_id
    }


def clear_seen_tx_ids(application: Optional[Application] = None) -> None:
    seen_tx_ids_cache.clear()
    if application is not None:
        application.bot_data["seen_tx_ids"] = set()


def get_active_session_chat_id() -> Optional[int]:
    for chat_id, session in sessions.items():
        if session.get("active"):
            return chat_id
    return None


def get_unfinished_session_chat_id() -> Optional[int]:
    for chat_id, session in sessions.items():
        if session.get("active") or session.get("ending_mode"):
            return chat_id
    return None


def get_session(chat_id: int) -> Optional[dict]:
    return sessions.get(chat_id)
