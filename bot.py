import logging
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, MONOBANK_INTERVAL, MONOBANK_JAR_NAME, MONOBANK_TOKEN
import session_store
from monobank import get_jar_id, get_jar_transactions, poll_monobank, process_transaction
from session_logic import (
    approve_transaction,
    ask_next_stack,
    build_pending_edit_text,
    build_pending_keyboard,
    build_pending_transaction_text,
    build_pot_text,
    cancel_auto_approve_job,
    complete_session,
    finalize_session_results,
    get_player_totals,
    reset_auto_approve_job,
    restore_auto_approve_jobs,
    send_stack_edit_message,
)
from stats import (
    build_overall_stats_text,
    build_player_stats_back_keyboard,
    build_player_stats_text,
    build_stats_keyboard,
    read_stats_rows,
)
from utils import title_name

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    unfinished_chat = session_store.get_unfinished_session_chat_id()

    if unfinished_chat is not None:
        existing_session = session_store.get_session(unfinished_chat)
        if unfinished_chat == chat_id:
            if existing_session and existing_session.get("ending_mode"):
                await update.message.reply_text(
                    "⚠️ У цьому чаті є незавершена сесія.\n"
                    "Продовжуйте введення фінальних стеків."
                )
            else:
                await update.message.reply_text("⚠️ Сесія вже активна в цьому чаті.")
        else:
            await update.message.reply_text(
                "⚠️ Інша незавершена сесія вже є в іншому чаті.\n"
                "У цій версії дозволена лише одна активна сесія одночасно."
            )
        return

    session_store.sessions[chat_id] = session_store.build_new_session()
    session_store.persist_runtime_state(context.application)

    await update.message.reply_text(
        "🃏 *Покер-сесію розпочато!*\n\n"

        "/end\\_session — завершити сесію\n"
        "/pot — переглянути поточний банк",
        parse_mode=ParseMode.MARKDOWN,
    )


async def get_pot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.get_session(chat_id)

    if not session or not session["active"]:
        await update.message.reply_text("❌ Немає активної сесії.")
        return

    await update.message.reply_text(
        build_pot_text(session["buyins"]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    rows = read_stats_rows()
    if not rows:
        await update.message.reply_text("Поки що немає даних статистики.")
        return

    if context.args:
        player_query = " ".join(context.args).strip()
        text = build_player_stats_text(player_query, rows)
        reply_markup = None
    else:
        text = build_overall_stats_text(rows)
        reply_markup = build_stats_keyboard(rows)

    await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_stats_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    rows = read_stats_rows()
    if not rows:
        await query.edit_message_text("Поки що немає даних статистики.")
        return

    data = query.data or ""
    if data == "stats:leaderboard":
        await query.edit_message_text(
            text=build_overall_stats_text(rows),
            reply_markup=build_stats_keyboard(rows),
        )
        return

    if data.startswith("stats:player:"):
        player_key = data.split(":", 2)[2]
        await query.edit_message_text(
            text=build_player_stats_text(player_key, rows),
            reply_markup=build_player_stats_back_keyboard(),
        )
        return


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.get_session(chat_id)

    if not session:
        await update.message.reply_text("❌ Немає активної сесії для завершення.")
        return

    if session["ending_mode"]:
        await update.message.reply_text("⚠️ Сесія вже перебуває в режимі введення фінальних стеків.")
        return

    if not session["active"]:
        await update.message.reply_text("❌ Немає активної сесії для завершення.")
        return

    if not session["buyins"]:
        await update.message.reply_text("❌ У цій сесії немає підтверджених бай-інів.")
        return

    session["active"] = False
    session["ending_mode"] = True
    for tx_id in list(session["pending"].keys()):
        cancel_auto_approve_job(context.application, tx_id)
    session["pending"].clear()

    players = get_player_totals(session["buyins"])
    session["stack_entry_order"] = players
    session["stack_entry_index"] = 0
    session["final_stacks"] = {}
    session_store.persist_runtime_state(context.application)

    player_lines = [f"• {player['name']} — {player['buyin_total']:.0f} грн" for player in players]

    await update.message.reply_text(
        "🛑 Сесію закрито для нових бай-інів.\n\n"
        "Тепер введіть фінальні стеки гравців по одному.\n\n"
        "👥 Гравці:\n"
        + "\n".join(player_lines)
    )

    await ask_next_stack(chat_id, context)


async def handle_tx_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not query.message:
        return

    chat_id = query.message.chat.id
    session = session_store.get_session(chat_id)

    if not session:
        await query.edit_message_text("❌ Сесію не знайдено.")
        return

    data = query.data or ""

    if data.startswith("tx:"):
        if not session.get("active"):
            await query.edit_message_text("❌ Сесія більше не активна.")
            return

        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "tx":
            return

        action, tx_id = parts[1], parts[2]
        pending = session["pending"].get(tx_id)

        if action in {"approve", "reject", "edit_name", "edit_sum"} and not pending:
            await query.edit_message_text("⚠️ Цю очікуючу транзакцію більше не існує.")
            return

        if action == "approve":
            ok, text = await approve_transaction(chat_id, tx_id, context.application)
            await query.edit_message_text(text)
            return

        if action == "reject":
            cancel_auto_approve_job(context.application, tx_id)
            session["rejected"].append(pending)
            session["pending"].pop(tx_id, None)
            session_store.persist_runtime_state(context.application)
            await query.edit_message_text(
                f"❌ Відхилено: {title_name(pending['name'])} — {pending['amount']:.2f} грн"
            )
            return

        if action == "edit_name":
            session["edit_state"] = {"mode": "name", "tx_id": tx_id}
            cancel_auto_approve_job(context.application, tx_id)
            pending["message_id"] = query.message.message_id
            session_store.persist_runtime_state(context.application)
            await query.edit_message_text(
                build_pending_edit_text(pending, "name"),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if action == "edit_sum":
            session["edit_state"] = {"mode": "sum", "tx_id": tx_id}
            cancel_auto_approve_job(context.application, tx_id)
            pending["message_id"] = query.message.message_id
            session_store.persist_runtime_state(context.application)
            await query.edit_message_text(
                build_pending_edit_text(pending, "sum"),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    if data == "end:leave_as_is":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Збережаю сесію з поточними фінальними стеками.")
        await complete_session(chat_id, context)
        return

    if data == "end:edit_stacks":
        await query.edit_message_reply_markup(reply_markup=None)
        await send_stack_edit_message(chat_id, context)
        return

    if data.startswith("stack:edit:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            return

        name_key = parts[2]
        player = next((player for player in session["stack_entry_order"] if player["name_key"] == name_key), None)
        if not player:
            await query.message.reply_text("❌ Гравця не знайдено.")
            return

        current_stack = session["final_stacks"].get(name_key, {}).get("final_stack")
        session["stack_edit_target"] = name_key
        session["edit_state"] = {"mode": "final_stack_edit", "name_key": name_key}
        session_store.persist_runtime_state(context.application)

        current_stack_text = "не задано" if current_stack is None else f"{current_stack:.2f} грн"
        await query.message.reply_text(
            f"✏️ Надішліть *новий фінальний стек* для *{player['name']}*.\n"
            f"Поточний стек: *{current_stack_text}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "stack:recheck":
        await query.edit_message_reply_markup(reply_markup=None)
        await finalize_session_results(chat_id, context)
        return


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id
    session = session_store.get_session(chat_id)

    if not session:
        return

    raw_text = (update.message.text or "").strip()
    edit_state = session.get("edit_state")
    if edit_state:
        mode = edit_state.get("mode")

        if mode in {"name", "sum"}:
            tx_id = edit_state["tx_id"]
            pending = session["pending"].get(tx_id)

            if not pending:
                session["edit_state"] = None
                session_store.persist_runtime_state(context.application)
                await update.message.reply_text("⚠️ Цю очікуючу транзакцію більше не існує.")
                return

            if mode == "name":
                if not raw_text:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=pending["message_id"],
                        text=build_pending_edit_text(pending, "name", "Ім'я не може бути порожнім."),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                pending["name"] = raw_text
                session["edit_state"] = None

                reset_auto_approve_job(context.application, chat_id, tx_id)
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pending["message_id"],
                    text=build_pending_transaction_text(pending),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_pending_keyboard(tx_id),
                )
                session_store.persist_runtime_state(context.application)
                return

            if mode == "sum":
                try:
                    amount = float(raw_text.replace(",", "."))
                except ValueError:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=pending["message_id"],
                        text=build_pending_edit_text(pending, "sum", "Сума має бути числом."),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                if amount <= 0:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=pending["message_id"],
                        text=build_pending_edit_text(pending, "sum", "Сума має бути більше нуля."),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

                pending["amount"] = amount
                session["edit_state"] = None

                reset_auto_approve_job(context.application, chat_id, tx_id)
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pending["message_id"],
                    text=build_pending_transaction_text(pending),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_pending_keyboard(tx_id),
                )
                session_store.persist_runtime_state(context.application)
                return

        if mode == "final_stack_edit":
            try:
                final_stack = float(raw_text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("❌ Фінальний стек має бути числом.")
                return

            if final_stack < 0:
                await update.message.reply_text("❌ Фінальний стек не може бути від'ємним.")
                return

            name_key = edit_state["name_key"]
            player = next((player for player in session["stack_entry_order"] if player["name_key"] == name_key), None)
            if not player:
                session["edit_state"] = None
                session["stack_edit_target"] = None
                await update.message.reply_text("❌ Гравця не знайдено.")
                return

            session["final_stacks"][name_key] = {
                "name": player["name"],
                "final_stack": final_stack,
            }
            session["edit_state"] = None
            session["stack_edit_target"] = None
            session_store.persist_runtime_state(context.application)

            await update.message.reply_text(
                f"✅ Оновлено фінальний стек для *{player['name']}*: *{final_stack:.2f} грн*",
                parse_mode=ParseMode.MARKDOWN,
            )
            await send_stack_edit_message(chat_id, context)
            return

    if session.get("ending_mode"):
        order = session["stack_entry_order"]
        idx = session["stack_entry_index"]

        if idx >= len(order):
            return

        try:
            final_stack = float(raw_text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Фінальний стек має бути числом.")
            return

        if final_stack < 0:
            await update.message.reply_text("❌ Фінальний стек не може бути від'ємним.")
            return

        player = order[idx]
        session["final_stacks"][player["name_key"]] = {
            "name": player["name"],
            "final_stack": final_stack,
        }
        session["stack_entry_index"] += 1
        session_store.persist_runtime_state(context.application)

        await update.message.reply_text(
            f"✅*{player['name']}*: *{final_stack:.0f} фішок*",
            parse_mode=ParseMode.MARKDOWN,
        )

        await ask_next_stack(chat_id, context)
        return


async def test_tx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.get_session(chat_id)

    if not session or not session["active"]:
        await update.message.reply_text("❌ Немає активної сесії.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /test_tx <name> <amount>")
        return

    name = context.args[0]

    try:
        amount = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Сума має бути числом.")
        return

    tx = {
        "id": f"test_{int(datetime.now().timestamp())}",
        "amount": int(amount * 100),
        "comment": name,
        "description": name,
        "time": int(datetime.now().timestamp()),
    }

    await process_transaction(context.application, chat_id, tx)


async def post_init(application: Application) -> None:
    session_store.load_runtime_state()
    application.bot_data["seen_tx_ids"] = set(session_store.seen_tx_ids_cache)
    restore_auto_approve_jobs(application)

    if session_store.sessions:
        logger.info("Restored %d unfinished session(s)", len(session_store.sessions))

    if not MONOBANK_TOKEN:
        logger.warning("MONOBANK_TOKEN is missing. Monobank polling will not work.")
        return

    try:
        jar_id = get_jar_id(MONOBANK_TOKEN, MONOBANK_JAR_NAME)
        application.bot_data["jar_id"] = jar_id

        if not application.bot_data["seen_tx_ids"]:
            try:
                initial = get_jar_transactions(
                    MONOBANK_TOKEN,
                    jar_id,
                    int(datetime.now().timestamp()) - MONOBANK_INTERVAL,
                )
                for tx in initial:
                    tx_id = str(tx.get("id"))
                    if tx_id:
                        application.bot_data["seen_tx_ids"].add(tx_id)
                session_store.persist_runtime_state(application)
            except Exception as exc:
                logger.warning("Failed to seed seen transactions: %s", exc)

        application.job_queue.run_repeating(
            poll_monobank,
            interval=MONOBANK_INTERVAL,
            first=MONOBANK_INTERVAL,
            name="monobank_poll",
        )

        logger.info("Monobank polling started for jar '%s' (%s)", MONOBANK_JAR_NAME, jar_id)
    except Exception as exc:
        logger.exception("Failed to initialize Monobank polling: %s", exc)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("Missing BOT_TOKEN environment variable.")
    if not MONOBANK_TOKEN:
        logger.warning("Missing MONOBANK_TOKEN environment variable.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start_session", start_session))
    app.add_handler(CommandHandler("pot", get_pot))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("end_session", end_session))
    app.add_handler(CommandHandler("test_tx", test_tx))

    app.add_handler(CallbackQueryHandler(handle_stats_action, pattern=r"^stats:"))
    app.add_handler(CallbackQueryHandler(handle_tx_action, pattern=r"^(tx:|end:|stack:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_input))

    logger.info("Poker Monobank bot is running...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
