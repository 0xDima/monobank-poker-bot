import logging
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from config import AUTO_APPROVE_DELAY, EXPORT_DIR
import session_store
from stats import append_session_results_to_csv
from utils import normalize_name, title_name

logger = logging.getLogger(__name__)


def build_pending_keyboard(tx_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Підтвердити", callback_data=f"tx:approve:{tx_id}"),
                InlineKeyboardButton("❌ Відхилити", callback_data=f"tx:reject:{tx_id}"),
            ],
            [
                InlineKeyboardButton("✏️ Змінити ім'я", callback_data=f"tx:edit_name:{tx_id}"),
                InlineKeyboardButton("💵 Змінити суму", callback_data=f"tx:edit_sum:{tx_id}"),
            ],
        ]
    )


def build_pending_transaction_text(pending: dict) -> str:
    return (
        "🆕 *Нове поповнення*\n\n"
        f"👤 *{title_name(pending['name'])}*\n"
        f"💵 *{pending['amount']:.0f}₴*"
    )


def build_pending_edit_text(pending: dict, mode: str, error_text: Optional[str] = None) -> str:
    if mode == "name":
        prompt = "Надішліть нове ім'я"
        header = "✏️ *Редагування імені*"
    else:
        prompt = "Надішліть нову суму"
        header = "💵 *Редагування суми*"

    lines = [
        header,
        "",
        f"👤 Поточне ім'я: *{title_name(pending['name'])}*",
        f"💵 Поточна сума: *{pending['amount']:.0f}₴*",
        "",
    ]

    if error_text:
        lines.extend([f"⚠️ {error_text}", ""])

    lines.append(prompt)
    return "\n".join(lines)


def cancel_auto_approve_job(application: Optional[Application], tx_id: str) -> None:
    if application is None or application.job_queue is None:
        return

    job_name = f"auto_approve_{tx_id}"
    for job in application.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()


def reset_auto_approve_job(
    application: Application,
    chat_id: int,
    tx_id: str,
    delay_seconds: Optional[float] = None,
) -> None:
    if application.job_queue is None:
        return

    delay = AUTO_APPROVE_DELAY if delay_seconds is None else max(0.0, delay_seconds)
    job_name = f"auto_approve_{tx_id}"
    cancel_auto_approve_job(application, tx_id)

    application.job_queue.run_once(
        auto_approve_transaction,
        when=delay,
        data={"chat_id": chat_id, "tx_id": tx_id},
        name=job_name,
    )


def restore_auto_approve_jobs(application: Application) -> None:
    now_ts = datetime.now().timestamp()

    for chat_id, session in session_store.sessions.items():
        if not session.get("active"):
            continue

        for tx_id, pending in session.get("pending", {}).items():
            created_at = pending.get("created_at", now_ts)
            try:
                created_at_ts = float(created_at)
            except (TypeError, ValueError):
                created_at_ts = now_ts

            remaining = AUTO_APPROVE_DELAY - (now_ts - created_at_ts)
            reset_auto_approve_job(
                application,
                chat_id,
                str(tx_id),
                delay_seconds=remaining,
            )


def build_mismatch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Залишити як є", callback_data="end:leave_as_is"),
                InlineKeyboardButton("✏️ Змінити стек", callback_data="end:edit_stacks"),
            ]
        ]
    )


def build_stack_edit_keyboard(players: list[dict], final_stacks: dict) -> InlineKeyboardMarkup:
    rows = []
    for player in players:
        key = player["name_key"]
        current_stack = final_stacks.get(key, {}).get("final_stack")
        if current_stack is None:
            label = f"✏️ {player['name']}"
        else:
            label = f"✏️ {player['name']} ({current_stack:.2f} грн)"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"stack:edit:{key}")
        ])

    rows.append([
        InlineKeyboardButton("🔁 Перевірити знову", callback_data="stack:recheck")
    ])
    return InlineKeyboardMarkup(rows)


def group_buyins_by_player(buyins: list[dict]) -> list[tuple[str, float, int, list[float]]]:
    grouped: dict[str, dict] = {}

    for entry in buyins:
        key = entry["name_key"]
        if key not in grouped:
            grouped[key] = {
                "display_name": entry["name"],
                "total": 0.0,
                "count": 0,
                "amounts": [],
            }
        grouped[key]["total"] += entry["amount"]
        grouped[key]["count"] += 1
        grouped[key]["amounts"].append(entry["amount"])

    result = [
        (
            data["display_name"],
            data["total"],
            data["count"],
            sorted(data["amounts"], reverse=True),
        )
        for data in grouped.values()
    ]
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def get_player_totals(buyins: list[dict]) -> list[dict]:
    grouped = {}

    for entry in buyins:
        key = entry["name_key"]
        if key not in grouped:
            grouped[key] = {
                "name": entry["name"],
                "name_key": key,
                "buyin_total": 0.0,
            }
        grouped[key]["buyin_total"] += entry["amount"]

    players = list(grouped.values())
    players.sort(key=lambda x: x["buyin_total"], reverse=True)
    return players


def build_buyins_text(buyins: list[dict]) -> str:
    grouped = group_buyins_by_player(buyins)
    if not grouped:
        return "Ще немає підтверджених бай-інів."

    lines = ["🏷 *Бай-іни по гравцях:*", ""]
    for idx, (name, total, count, amounts) in enumerate(grouped, start=1):
        amount_list = ", ".join(f"{a:.2f}" for a in amounts)
        lines.append(f"{idx}. *{name}* — {total:.2f} грн ({count} бай-ін(ів))")
        lines.append(f"   ↳ {amount_list}")
    return "\n".join(lines)


def build_pot_text(buyins: list[dict]) -> str:
    if not buyins:
        return (
            "🏦 Банк: ₴0\n"
            "📥 Бай-інів: 0\n"
            "👥 Гравців: 0\n\n"
            "Ще немає підтверджених бай-інів."
        )

    total_bank = sum(entry["amount"] for entry in buyins)

    grouped = OrderedDict()
    for entry in buyins:
        name = entry["name"]
        if name not in grouped:
            grouped[name] = []
        grouped[name].append(entry)

    lines = [
        f"🏦 Банк: ₴{total_bank:.0f}",
        f"📥 Бай-інів: {len(buyins)}",
        f"👥 Гравців: {len(grouped)}",
        "",
        "",
    ]

    for name, entries in grouped.items():
        lines.append(f"👤 *{name}*")

        player_total = 0
        for entry in entries:
            amount = entry["amount"]
            player_total += amount

            time_str = datetime.strptime(
                entry["time"], "%Y-%m-%d %H:%M:%S"
            ).strftime("%H:%M")

            lines.append(f"• ₴{amount:.0f} · {time_str}")

        if len(entries) > 1:
            lines.append(f"   ↳ Разом: *₴{player_total:.0f}*")

        lines.append("")

    return "\n".join(lines).strip()


def save_session_to_txt(chat_id: int, session: dict) -> Path:
    started_raw: datetime = session["started_at_raw"]
    ended_raw = datetime.now()

    file_name = f"session_{started_raw.strftime('%Y-%m-%d_%H-%M-%S')}_chat_{chat_id}.txt"
    file_path = EXPORT_DIR / file_name

    approved = session["buyins"]
    rejected = session["rejected"]
    total = sum(x["amount"] for x in approved)
    grouped = group_buyins_by_player(approved)

    lines = [
        "ПІДСУМОК ПОКЕР-СЕСІЇ",
        f"Chat ID: {chat_id}",
        f"Початок: {started_raw.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Кінець: {ended_raw.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"ФІНАЛЬНИЙ БАНК: {total:.2f} грн",
        "",
        "ПІДСУМКИ ПО ГРАВЦЯХ",
    ]

    if grouped:
        for name, player_total, count, amounts in grouped:
            amount_list = ", ".join(f"{a:.2f}" for a in amounts)
            lines.append(f"- {name}: {player_total:.2f} грн ({count} бай-ін(ів)) [{amount_list}]")
    else:
        lines.append("- Немає підтверджених бай-інів")

    lines.extend(["", "УСІ ПІДТВЕРДЖЕНІ БАЙ-ІНИ"])
    if approved:
        for entry in approved:
            lines.append(
                f"- tx_id={entry['tx_id']} | {entry['name']} | {entry['amount']:.2f} | {entry['time']}"
            )
    else:
        lines.append("- Немає")

    lines.extend(["", "ФІНАЛЬНІ СТЕКИ"])
    if session.get("final_stacks"):
        for entry in session["final_stacks"].values():
            lines.append(f"- {entry['name']}: {entry['final_stack']:.2f} грн")
    else:
        lines.append("- Немає")

    lines.extend(["", "ФІНАЛЬНІ РЕЗУЛЬТАТИ"])
    if session.get("results"):
        for result in session["results"]:
            sign = "+" if result["net"] >= 0 else ""
            lines.append(
                f"- {result['name']} | Бай-іни: {result['buyin_total']:.2f} | "
                f"Фінальний стек: {result['final_stack']:.2f} | Результат: {sign}{result['net']:.2f}"
            )
    else:
        lines.append("- Немає")

    lines.extend(["", "ВІДХИЛЕНІ / ПРОІГНОРОВАНІ"])
    if rejected:
        for entry in rejected:
            lines.append(
                f"- tx_id={entry['tx_id']} | {entry['name']} | {entry['amount']:.2f} | {entry['time']}"
            )
    else:
        lines.append("- Немає")

    lines.extend(["", "ОЧІКУЮТЬ НА КІНЕЦЬ СЕСІЇ"])
    if session["pending"]:
        for entry in session["pending"].values():
            lines.append(
                f"- tx_id={entry['tx_id']} | {entry['name']} | {entry['amount']:.2f} | {entry['time']}"
            )
    else:
        lines.append("- Немає")

    file_path.write_text("\n".join(lines), encoding="utf-8")
    return file_path


def build_results(session: dict) -> list[dict]:
    players = session["stack_entry_order"]
    final_stacks = session["final_stacks"]

    results = []
    for player in players:
        key = player["name_key"]
        buyin_total = player["buyin_total"]
        final_stack = final_stacks[key]["final_stack"]
        net = final_stack - buyin_total

        results.append({
            "name": player["name"],
            "buyin_total": buyin_total,
            "final_stack": final_stack,
            "net": net,
        })

    results.sort(key=lambda x: x["net"], reverse=True)
    return results


async def complete_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = session_store.get_session(chat_id)
    if not session:
        return

    total_pot = sum(x["amount"] for x in session["buyins"])
    started_at = session["started_at_raw"].strftime("#%m.%d.%Y")
    session["results"] = build_results(session)
    save_session_to_txt(chat_id, session)

    try:
        append_session_results_to_csv(chat_id, session)
    except Exception as exc:
        logger.exception("Failed to append session stats to CSV: %s", exc)

    lines = [
        started_at,
        "🏁 Сесію завершено",
        "",
        f"🏦 Банк: *{total_pot:.0f}* ",
        "",
        "📊 Підсумок:",
    ]

    for result in session["results"]:
        lines.append(f"• *{result['name']}*: {result['net']:+.0f} ")
        lines.append(
            f"  бай-іни *{result['buyin_total']:.0f}*  • стек *{result['final_stack']:.0f}* "
        )
        lines.append("")

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )

    session_store.sessions.pop(chat_id, None)
    session_store.clear_seen_tx_ids(context.application)
    session_store.persist_runtime_state(context.application)


async def send_stack_edit_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = session_store.get_session(chat_id)
    if not session:
        return

    total_pot = sum(x["amount"] for x in session["buyins"])
    total_final = sum(x["final_stack"] for x in session["final_stacks"].values())

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✏️ *Оберіть гравця, якому треба змінити фінальний стек:*\n\n"
            f"🏦 Банк: *{total_pot:.2f} грн*\n"
            f"📦 Сума фінальних стеків: *{total_final:.2f} грн*\n"
            f"➖ Різниця: *{(total_final - total_pot):.2f} грн*"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_stack_edit_keyboard(session["stack_entry_order"], session["final_stacks"]),
    )


async def approve_transaction(
    chat_id: int,
    tx_id: str,
    application: Optional[Application] = None,
) -> tuple[bool, str]:
    session = session_store.get_session(chat_id)
    if not session or not session["active"]:
        return False, "Немає активної сесії."

    pending = session["pending"].get(tx_id)
    if not pending:
        return False, "Очікуючу транзакцію не знайдено."

    approved_entry = {
        "tx_id": pending["tx_id"],
        "name": title_name(pending["name"]),
        "name_key": normalize_name(pending["name"]),
        "amount": pending["amount"],
        "time": pending["time"],
        "source_comment": pending.get("source_comment", ""),
        "source_description": pending.get("source_description", ""),
    }

    session["buyins"].append(approved_entry)
    session["pending"].pop(tx_id, None)
    cancel_auto_approve_job(application, tx_id)
    session_store.persist_runtime_state(application)

    total = sum(x["amount"] for x in session["buyins"])
    return True, f"👤 {approved_entry['name']}\n💵 {approved_entry['amount']:.0f}\n🏦 Банк: {total:.0f}"


async def ask_next_stack(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = session_store.get_session(chat_id)
    if not session:
        return

    order = session["stack_entry_order"]
    idx = session["stack_entry_index"]

    if idx >= len(order):
        await finalize_session_results(chat_id, context)
        return

    player = order[idx]
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🧾 Фіксуємо фінальний стек\n\n"
            f"👤 *{player['name']}*\n"
            f"📥 Загальна сума бай-інів: {player['buyin_total']:.0f}\n\n"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def finalize_session_results(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = session_store.get_session(chat_id)
    if not session:
        return

    players = session["stack_entry_order"]
    final_stacks = session["final_stacks"]
    total_pot = sum(x["amount"] for x in session["buyins"])
    total_final = sum(x["final_stack"] for x in final_stacks.values())

    missing_players = [p["name"] for p in players if p["name_key"] not in final_stacks]
    if missing_players:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Не для всіх гравців введено фінальні стеки.\n\n"
                "Ще бракує:\n" + "\n".join(f"- {name}" for name in missing_players)
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if abs(total_final - total_pot) > 0.01:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Фінальні стеки не збігаються з банком.\n\n"
                f"🏦 Банк: *{total_pot:.0f}*\n"
                f"📦 Стек: *{total_final:.0f}*\n"
                f"➖ Різниця: *{(total_final - total_pot):.2f} грн*\n\n"
                "Оберіть, що робити далі:"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_mismatch_keyboard(),
        )
        return

    await complete_session(chat_id, context)


async def auto_approve_transaction(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    if not job_data:
        return

    chat_id = job_data["chat_id"]
    tx_id = job_data["tx_id"]

    session = session_store.get_session(chat_id)
    if not session:
        return

    if not session.get("active"):
        return

    pending = session["pending"].get(tx_id)
    if not pending:
        return

    message_id = pending.get("message_id")

    ok, text = await approve_transaction(chat_id, tx_id, context.application)
    if not ok:
        return

    if message_id is not None:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
