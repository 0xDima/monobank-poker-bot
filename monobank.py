import logging
from datetime import datetime

import requests
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from config import MONOBANK_API_URL, MONOBANK_INTERVAL, MONOBANK_TOKEN
import session_store
from session_logic import (
    build_pending_keyboard,
    build_pending_transaction_text,
    reset_auto_approve_job,
)

logger = logging.getLogger(__name__)


def get_jar_id(token: str, jar_name: str) -> str:
    headers = {"X-Token": token}
    response = requests.get(
        f"{MONOBANK_API_URL}/personal/client-info",
        headers=headers,
        timeout=20,
    )

    if response.status_code == 429:
        raise RuntimeError("Monobank rate limit hit. Wait at least 60 seconds.")

    response.raise_for_status()

    jars = response.json().get("jars", [])
    for jar in jars:
        if jar.get("title") == jar_name:
            return jar["id"]

    raise ValueError(f"Jar '{jar_name}' not found.")


def get_jar_transactions(token: str, jar_id: str, from_time: int) -> list:
    headers = {"X-Token": token}
    now = int(datetime.now().timestamp())
    url = f"{MONOBANK_API_URL}/personal/statement/{jar_id}/{from_time}/{now}"

    response = requests.get(url, headers=headers, timeout=20)

    if response.status_code == 429:
        raise RuntimeError("Monobank rate limit hit. Wait at least 60 seconds.")
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        return []
    return data


def resolve_transaction_name(tx: dict) -> str:
    raw_description = " ".join((tx.get("description") or "").strip().split())
    raw_comment = " ".join((tx.get("comment") or "").strip().split())

    if raw_description in {"З Білої картки", "З Чорної картки"}:
        return "Ігор Ребега"

    if raw_description.startswith("Від:"):
        sender_name = raw_description.split(":", 1)[1].strip()
        if sender_name:
            return sender_name

    return raw_description or raw_comment or "unknown"


async def poll_monobank(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    bot_state = app.bot_data
    jar_id = bot_state.get("jar_id")

    if not jar_id:
        return

    active_chat = session_store.get_active_session_chat_id()
    if active_chat is None:
        return

    session = session_store.sessions.get(active_chat)
    if not session or not session["active"]:
        return

    from_time = int(datetime.now().timestamp()) - MONOBANK_INTERVAL

    try:
        transactions = get_jar_transactions(MONOBANK_TOKEN, jar_id, from_time)
    except RuntimeError as exc:
        logger.warning("Monobank warning: %s", exc)
        return
    except Exception as exc:
        logger.exception("Failed to fetch Monobank transactions: %s", exc)
        return

    for tx in transactions:
        await process_transaction(app, active_chat, tx)


async def process_transaction(application: Application, active_chat: int, tx: dict) -> None:
    session = session_store.sessions.get(active_chat)
    if not session or not session["active"]:
        return

    seen_ids: set = application.bot_data.setdefault("seen_tx_ids", set())

    tx_id = str(tx.get("id"))
    if not tx_id or tx_id in seen_ids:
        return

    seen_ids.add(tx_id)
    session_store.persist_runtime_state(application)

    amount = abs(tx.get("amount", 0) / 100)
    if amount <= 0:
        return

    raw_comment = (tx.get("comment") or "").strip()
    raw_description = (tx.get("description") or "").strip()
    raw_name = resolve_transaction_name(tx)
    tx_time_str = session_store.format_dt(tx.get("time"))

    if tx_id in session["pending"]:
        return

    session["pending"][tx_id] = {
        "tx_id": tx_id,
        "name": raw_name,
        "amount": amount,
        "time": tx_time_str,
        "source_comment": raw_comment,
        "source_description": raw_description,
        "created_at": datetime.now().timestamp(),
    }

    sent_message = await application.bot.send_message(
        chat_id=active_chat,
        text=build_pending_transaction_text(session["pending"][tx_id]),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_pending_keyboard(tx_id),
    )
    session["pending"][tx_id]["message_id"] = sent_message.message_id
    reset_auto_approve_job(application, active_chat, tx_id)
    session_store.persist_runtime_state(application)
