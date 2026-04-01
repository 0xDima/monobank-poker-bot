import csv
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import STATS_CSV_PATH
from utils import normalize_name

logger = logging.getLogger(__name__)

STATS_CSV_HEADERS = [
    "session_id",
    "session_date",
    "player_name",
    "buyins_total",
    "final_stack",
    "net_result",
]


def get_session_started_at(session: dict) -> datetime:
    started_at_raw = session.get("started_at_raw")
    if isinstance(started_at_raw, datetime):
        return started_at_raw

    if isinstance(started_at_raw, str):
        try:
            return datetime.fromisoformat(started_at_raw)
        except ValueError:
            pass

    started_at = session.get("started_at")
    if isinstance(started_at, str):
        try:
            return datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    return datetime.now()


def build_session_id(chat_id: int, session: dict) -> str:
    started_at = get_session_started_at(session)
    return f"{started_at.strftime('%Y%m%d%H%M%S')}_{chat_id}"


def format_amount_for_csv(value: float) -> str:
    return f"{float(value):.2f}"


def format_money(value: float) -> str:
    value = float(value)
    if abs(value - round(value)) < 0.01:
        return f"{round(value):.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_signed_money(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{format_money(value)}"


def ensure_stats_csv_exists() -> None:
    STATS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATS_CSV_PATH.exists() and STATS_CSV_PATH.stat().st_size > 0:
        return

    with STATS_CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=STATS_CSV_HEADERS)
        writer.writeheader()


def append_session_results_to_csv(chat_id: int, session: dict) -> None:
    results = session.get("results") or []
    if not results:
        return

    ensure_stats_csv_exists()

    started_at = get_session_started_at(session)
    session_id = build_session_id(chat_id, session)
    session_date = started_at.strftime("%Y-%m-%d")

    with STATS_CSV_PATH.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=STATS_CSV_HEADERS)
        for result in results:
            writer.writerow(
                {
                    "session_id": session_id,
                    "session_date": session_date,
                    "player_name": result["name"],
                    "buyins_total": format_amount_for_csv(result["buyin_total"]),
                    "final_stack": format_amount_for_csv(result["final_stack"]),
                    "net_result": format_amount_for_csv(result["net"]),
                }
            )


def read_stats_rows() -> list[dict]:
    if not STATS_CSV_PATH.exists():
        return []

    try:
        with STATS_CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            rows = []
            for row in reader:
                if not row:
                    continue

                try:
                    rows.append(
                        {
                            "session_id": (row.get("session_id") or "").strip(),
                            "session_date": (row.get("session_date") or "").strip(),
                            "player_name": (row.get("player_name") or "").strip(),
                            "buyins_total": float(row.get("buyins_total") or 0),
                            "final_stack": float(row.get("final_stack") or 0),
                            "net_result": float(row.get("net_result") or 0),
                        }
                    )
                except (TypeError, ValueError):
                    logger.warning("Skipping malformed stats row: %s", row)

            return rows
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.exception("Failed to read stats CSV: %s", exc)
        return []


def get_stats_player_groups(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        player_name = row["player_name"]
        if player_name:
            grouped[normalize_name(player_name)].append(row)

    if not grouped:
        return []

    player_groups = []
    for player_key, player_rows in grouped.items():
        name_counts: dict[str, int] = defaultdict(int)
        for row in player_rows:
            name_counts[row["player_name"]] += 1
        display_name = max(
            name_counts.items(),
            key=lambda item: (item[1], item[0].lower()),
        )[0]

        total_buyins = sum(row["buyins_total"] for row in player_rows)
        total_net = sum(row["net_result"] for row in player_rows)
        best_session = max(row["net_result"] for row in player_rows)
        worst_session = min(row["net_result"] for row in player_rows)
        player_groups.append(
            {
                "player_key": player_key,
                "player_name": display_name,
                "sessions_played": len(player_rows),
                "total_buyins": total_buyins,
                "total_final_stacks": sum(row["final_stack"] for row in player_rows),
                "total_net": total_net,
                "best_session": best_session,
                "worst_session": worst_session,
                "rows": player_rows,
            }
        )

    player_groups.sort(
        key=lambda item: (-item["total_net"], item["player_name"].lower())
    )
    return player_groups


def build_stats_keyboard(rows: list[dict]) -> Optional[InlineKeyboardMarkup]:
    player_groups = get_stats_player_groups(rows)
    if not player_groups:
        return None

    buttons = [
        InlineKeyboardButton(
            group["player_name"],
            callback_data=f"stats:player:{group['player_key']}",
        )
        for group in player_groups
    ]

    keyboard_rows = [
        buttons[idx:idx + 2]
        for idx in range(0, len(buttons), 2)
    ]
    return InlineKeyboardMarkup(keyboard_rows)


def build_player_stats_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ До загальної статистики", callback_data="stats:leaderboard")]]
    )


def build_overall_stats_text(rows: list[dict]) -> str:
    leaderboard = get_stats_player_groups(rows)
    if not leaderboard:
        return "Поки що немає даних статистики."

    digit_icons = {
        "0": "0️⃣",
        "1": "1️⃣",
        "2": "2️⃣",
        "3": "3️⃣",
        "4": "4️⃣",
        "5": "5️⃣",
        "6": "6️⃣",
        "7": "7️⃣",
        "8": "8️⃣",
        "9": "9️⃣",
    }

    def format_rank_label(rank: int) -> str:
        if rank == 10:
            return "🔟"
        return "".join(digit_icons[digit] for digit in str(rank))

    lines = ["📈 Poker Stats", ""]
    for idx, item in enumerate(leaderboard, start=1):
        rank_label = format_rank_label(idx)
        lines.extend(
            [
                f"{rank_label} {item['player_name']}",
                f"💰 Профіт: {format_signed_money(item['total_net'])} грн",
                f"🎮 Сесій: {item['sessions_played']}",
                f"🪙 Бай-іни: {format_money(item['total_buyins'])} грн",
                f"🏆 Найкраща: {format_signed_money(item['best_session'])} грн",
                f"📉 Найгірша: {format_signed_money(item['worst_session'])} грн",
                "",
            ]
        )
        if idx != len(leaderboard):
            lines.extend(["━━━━━━━━━━━━━━", ""])

    return "\n".join(lines).rstrip()


def build_player_stats_text(player_query: str, rows: list[dict]) -> str:
    player_key = normalize_name(player_query)
    player_group = next(
        (group for group in get_stats_player_groups(rows) if group["player_key"] == player_key),
        None,
    )

    if not player_group:
        return f"Не знайшов статистику для гравця: {player_query}"

    display_name = player_group["player_name"]
    total_buyins = player_group["total_buyins"]
    total_final_stacks = player_group["total_final_stacks"]
    total_net = player_group["total_net"]
    sessions_played = player_group["sessions_played"]
    average_result = total_net / sessions_played if sessions_played else 0.0
    best_session = player_group["best_session"]
    worst_session = player_group["worst_session"]

    return "\n".join(
        [
            f"📈 Статистика гравця: {display_name}",
            "",
            f"🎮 Сесій: {sessions_played}",
            f"🪙 Загальні бай-іни: {format_money(total_buyins)} грн",
            f"🏦 Загальні фінальні стеки: {format_money(total_final_stacks)} грн",
            "",
            f"💰 Загальний результат: {format_signed_money(total_net)} грн",
            f"📊 Середній результат: {format_signed_money(average_result)} грн",
            "",
            f"🏆 Найкраща сесія: {format_signed_money(best_session)} грн",
            f"📉 Найгірша сесія: {format_signed_money(worst_session)} грн",
        ]
    )
