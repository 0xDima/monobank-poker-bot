import json
import os
import time

import requests

MONOBANK_API_URL = "https://api.monobank.ua"


def get_jar_id(token: str, jar_name: str) -> str:
    """Find the ID of a jar by its name."""
    headers = {"X-Token": token}
    response = requests.get(f"{MONOBANK_API_URL}/personal/client-info", headers=headers)

    if response.status_code == 429:
        raise RuntimeError("Rate limit hit — wait at least 60 seconds before retrying.")
    response.raise_for_status()

    jars = response.json().get("jars", [])
    for jar in jars:
        if jar.get("title") == jar_name:
            return jar["id"]

    raise ValueError(f"Jar '{jar_name}' not found.")


def get_jar_transactions(token: str, jar_id: str, from_time: int) -> list:
    """Fetch transactions for a jar from a given Unix timestamp."""
    headers = {"X-Token": token}
    now = int(time.time())
    url = f"{MONOBANK_API_URL}/personal/statement/{jar_id}/{from_time}/{now}"

    response = requests.get(url, headers=headers)

    if response.status_code == 429:
        raise RuntimeError("Rate limit hit — wait at least 60 seconds before retrying.")
    response.raise_for_status()

    return response.json()


def watch_jar(token: str, jar_name: str = "test", interval: int = 60) -> None:
    """Poll the jar and print new incoming transactions."""
    print(f"Looking up jar '{jar_name}'...")
    jar_id = get_jar_id(token, jar_name)
    print(f"Watching jar '{jar_name}' (id: {jar_id}) — checking every {interval}s\n")

    seen_ids = set()

    initial = get_jar_transactions(token, jar_id, int(time.time()) - interval)
    for tx in initial:
        seen_ids.add(tx["id"])

    while True:
        time.sleep(interval)

        from_time = int(time.time()) - interval
        try:
            transactions = get_jar_transactions(token, jar_id, from_time)
        except RuntimeError as exc:
            print(f"[warning] {exc}")
            continue
        except Exception as exc:
            print(f"[error] Failed to fetch transactions: {exc}")
            continue

        for tx in transactions:
            if tx["id"] in seen_ids:
                continue

            seen_ids.add(tx["id"])

            amount = tx.get("amount", 0) / 100
            balance = tx.get("balance", 0) / 100
            description = tx.get("description", "Unknown")
            tx_time = tx.get("time")

            print("--- New transaction ---")
            print(f"{description} — {amount:.2f} UAH (balance: {balance:.2f})")

            notification = {
                "name": description,
                "sum": amount,
                "time": tx_time,
            }
            print(json.dumps(notification, ensure_ascii=False, indent=2))
            print()


if __name__ == "__main__":
    token = os.getenv("MONOBANK_TOKEN", "").strip()
    jar_name = os.getenv("MONOBANK_JAR_NAME", "test").strip() or "test"
    interval = int(os.getenv("MONOBANK_INTERVAL", "60"))

    if not token:
        raise ValueError("Set MONOBANK_TOKEN in the environment before running this script.")

    watch_jar(token, jar_name=jar_name, interval=interval)
