import re
from typing import Any, Dict, List, Optional



def _replace_whole_word(text: str, old: str, new: str) -> str:
    """Whole-word replace (CNL-compatible). Falls back to plain replace."""
    if not old:
        return text
    try:
        pattern = re.compile(rf"\b{re.escape(old)}\b", flags=re.IGNORECASE)
        return pattern.sub(new, text)
    except re.error:
        return text.replace(old, new)


def _apply_replacements_cnl_style(text, replacements: list):
    """Match CNL engine: from/old → to/new, optional regex, else whole-word."""
    if not text or not replacements:
        return text
    result = text
    for item in replacements:
        is_regex = False
        if isinstance(item, dict):
            old = item.get("from") or item.get("old") or ""
            new = item.get("to") if "to" in item else item.get("new", "")
            is_regex = bool(item.get("regex") or item.get("is_regex"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            old, new = item[0], item[1]
        else:
            continue
        if not old:
            continue
        try:
            if is_regex:
                result = re.sub(str(old), str(new), result, flags=re.IGNORECASE)
            else:
                result = _replace_whole_word(result, str(old), str(new))
        except re.error:
            result = _replace_whole_word(result, str(old), str(new))
    return result


def _extract_source_text(message) -> str:
    """Prefer HTML so bold/links/entities survive when Kurigram provides .html."""
    for attr in ("text", "caption"):
        val = getattr(message, attr, None)
        if val is None:
            continue
        html = getattr(val, "html", None)
        if html:
            return str(html)
        return str(val)
    for attr in ("text_html", "caption_html"):
        val = getattr(message, attr, None)
        if val:
            return str(val)
    return ""


def process_caption(message, settings: Dict[str, Any]) -> Optional[str]:
    original = _extract_source_text(message)
    return apply_caption_text(original, settings)


def apply_caption_text(original: str, settings: Dict[str, Any]) -> Optional[str]:
    caption = original or ""

    if settings.get("replace_enabled", False):
        replacements = settings.get("replacements", []) or []
        caption = _apply_replacements_cnl_style(caption, replacements)

    if settings.get("remove_links", False):
        # Same tested cleaner as CNL Auto Post (core/cnl/clean.py)
        from core.cnl.clean import clean_file_name
        caption = clean_file_name(caption) if caption else caption

    if settings.get("caption_enabled", False):
        template = settings.get("caption_template", "{caption}")
        caption = template.replace("{caption}", caption)

    if not caption or not str(caption).strip():
        return None
    return caption


def build_inline_keyboard(settings: Dict[str, Any]):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if not settings.get("inline_buttons_enabled", True):
        return None

    buttons_data = settings.get("inline_buttons", []) or []
    if not buttons_data:
        return None

    keyboard = []
    for row in buttons_data:
        btn_row = []
        for btn in row:
            text = btn.get("text")
            url = btn.get("url")
            if text and url:
                btn_row.append(InlineKeyboardButton(text=text, url=url))
        if btn_row:
            keyboard.append(btn_row)

    if not keyboard:
        return None
    return InlineKeyboardMarkup(keyboard)
