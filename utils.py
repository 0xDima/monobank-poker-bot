def normalize_name(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def title_name(value: str) -> str:
    cleaned = " ".join((value or "").strip().split())
    return cleaned.title() if cleaned else "Unknown"
