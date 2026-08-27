"""User-input validation. Never silently drop invalid lines."""

from __future__ import annotations

import re
from typing import Dict, List, Tuple
from urllib.parse import urlparse

URL_OK = re.compile(r"^(https?://|tg://|mailto:)", re.I)


class LineError:
    def __init__(self, line: int, text: str, reason: str):
        self.line = line
        self.text = text
        self.reason = reason

    def format(self) -> str:
        shown = self.text if len(self.text) < 80 else self.text[:77] + "..."
        return f"❌ Invalid on line {self.line}:\n`{shown}`\n{self.reason}"


def format_result(errors: List[LineError], saved: int, noun: str) -> str:
    parts = [e.format() for e in errors]
    if saved:
        parts.append(f"✅ {saved} valid {noun} saved.")
    elif errors:
        parts.append(f"No {noun} saved.")
    return "\n\n".join(parts) if parts else f"✅ {saved} valid {noun} saved."


def parse_word_list(raw: str, existing: List[str] | None = None) -> Tuple[List[str], List[LineError]]:
    existing_l = {w.lower() for w in (existing or [])}
    seen = set(existing_l)
    items: List[str] = []
    errors: List[LineError] = []
    parts = re.split(r"[\n,]", raw)
    for i, part in enumerate(parts, 1):
        word = part.strip()
        if not word:
            continue
        key = word.lower()
        if key in seen:
            errors.append(LineError(i, word, "Duplicate"))
            continue
        seen.add(key)
        items.append(word)
    return items, errors


def parse_replacements(raw: str, existing_from: List[str] | None = None) -> Tuple[List[Dict[str, str]], List[LineError]]:
    seen = {x.lower() for x in (existing_from or [])}
    items: List[Dict[str, str]] = []
    errors: List[LineError] = []
    for i, line in enumerate(raw.splitlines(), 1):
        trimmed = line.strip()
        if not trimmed:
            continue
        if "=>" not in trimmed:
            errors.append(LineError(i, trimmed, 'Missing "=>". Use: old => new'))
            continue
        left, right = trimmed.split("=>", 1)
        src = left.strip()
        dst = right.strip()
        if not src:
            errors.append(LineError(i, trimmed, "Empty source text"))
            continue
        key = src.lower()
        if key in seen:
            errors.append(LineError(i, trimmed, "Duplicate rule"))
            continue
        seen.add(key)
        items.append({"from": src, "to": dst})
    return items, errors


def is_valid_button_url(url: str) -> bool:
    if not url or not URL_OK.match(url):
        return False
    if url.lower().startswith("http"):
        try:
            parsed = urlparse(url)
            return bool(parsed.netloc)
        except Exception:
            return False
    return True


def parse_inline_buttons(raw: str) -> Tuple[List[List[Dict[str, str]]], List[LineError]]:
    rows: List[List[Dict[str, str]]] = []
    errors: List[LineError] = []
    for i, line in enumerate(raw.splitlines(), 1):
        trimmed = line.strip()
        if not trimmed:
            continue
        row: List[Dict[str, str]] = []
        row_ok = True
        for part in trimmed.split("||"):
            p = part.strip()
            if not p:
                continue
            if " - " not in p:
                errors.append(LineError(i, p, "Use: Button text - https://example.com"))
                row_ok = False
                continue
            text, url = p.split(" - ", 1)
            text, url = text.strip(), url.strip()
            if not text:
                errors.append(LineError(i, p, "Empty button label"))
                row_ok = False
                continue
            if not is_valid_button_url(url):
                errors.append(LineError(i, url, "Invalid URL"))
                row_ok = False
                continue
            row.append({"text": text, "url": url})
        if row_ok and row:
            rows.append(row)
    return rows, errors


def parse_delay(raw: str) -> Tuple[float | None, str | None]:
    try:
        n = float(raw.strip())
    except ValueError:
        return None, "Send a number, e.g. `1.5`"
    if n < 0:
        return None, "Delay cannot be negative."
    if n > 60:
        return None, "Delay cannot exceed 60 seconds."
    return n, None
