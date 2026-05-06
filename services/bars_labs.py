import datetime
import re
from typing import Optional


def normalize_bars_labs_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    aliases = {
        "all": "all",
        "все": "all",
        "все анализы": "all",
        "latest": "latest",
        "fresh": "latest",
        "свежие": "latest",
        "свежие анализы": "latest",
        "dynamics": "dynamics",
        "dynamic": "dynamics",
        "динамика": "dynamics",
        "динамика по показателям": "dynamics",
    }
    return aliases.get(normalized, "latest")


def parse_bars_laboratory_datetime(value: str) -> Optional[datetime.datetime]:
    text = str(value or "").strip()
    candidates = [text]
    match = re.search(r"\b\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.datetime.strptime(candidate, pattern)
            except ValueError:
                continue
    if match and len(match.group(0)) > 10:
        try:
            return datetime.datetime.strptime(match.group(0)[:16], "%d.%m.%Y %H:%M")
        except ValueError:
            pass
    return None


def split_bars_laboratory_result_items(result: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for part in re.split(r"\s*;\s*", str(result or "")):
        part = part.strip()
        if not part or ":" not in part:
            continue
        indicator, value = part.split(":", 1)
        indicator = re.sub(r"\s+", " ", indicator).strip(" .:-")
        value = re.sub(r"\s+", " ", value).strip(" .:-")
        if indicator and value:
            items.append((indicator, value))
    return items
