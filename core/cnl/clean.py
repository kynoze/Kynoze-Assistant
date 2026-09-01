"""Caption / filename cleaning — shared by CNL Auto Post and target remove-links."""
import re
import unicodedata


FILE_INFO_PATTERNS = [
    r"\baudio\b", r"\bsubtitle\b", r"\besub\b", r"\bsub\b",
    r"\baac\b", r"\bac3\b", r"\be-?ac3\b", r"\bddp?\b",
    r"\bdts\b", r"\batmos\b", r"\bflac\b", r"\bmp3\b",
    r"\bx264\b", r"\bx265\b", r"\bhevc\b", r"\bavc\b",
    r"\bweb[- ]?dl\b", r"\bwebrip\b", r"\bbluray\b",
    r"\bhdrip\b", r"\bremux\b", r"\b2160p\b", r"\b1080p\b",
    r"\b720p\b", r"\b480p\b", r"\bhindi\b", r"\benglish\b",
    r"\btamil\b", r"\btelugu\b", r"\bmalayalam\b",
    r"\bkannada\b", r"\bjapanese\b", r"\bkorean\b",
    r"\bchinese\b", r"\bdual audio\b", r"\bmulti\b",
    r"\bchapters?\b"
]

# Patterns that strongly indicate a media release / file name
MEDIA_NAME_PATTERNS = [
    r"\b(?:2160|1080|720|480)p\b",
    r"\b(?:x264|x265|hevc|avc|h\.?264|h\.?265)\b",
    r"\b(?:web[- ]?dl|webrip|bluray|hdrip|remux|bdrip|dvdrip|hdtv)\b",
    r"\b(?:dual[\s-]?audio|multi[\s-]?audio|hindi|english|tamil|telugu)\b",
    r"\b(?:esub|msub|subs?)\b",
    r"\.(?:mkv|mp4|avi|m4v|ts|m2ts)\b",
    r"\bS\d{1,2}\s*E\d{1,3}\b",
    r"\bEP?\d{1,3}\s*[-–]\s*\d{1,3}\b",
    r"\b(?:complete|season|episode)\b",
]


def remove_fancy_fonts(text):
    result = []
    for ch in text:
        cp = ord(ch)
        name = unicodedata.name(ch, "")

        if 0x1D400 <= cp <= 0x1D7FF:          # Mathematical Alphanumeric
            continue
        if 0xFF01 <= cp <= 0xFF5E:             # Fullwidth ASCII
            continue
        if 0x2460 <= cp <= 0x24FF:             # Enclosed Alphanumerics
            continue
        if 0x1F100 <= cp <= 0x1F1FF:           # Enclosed Alphanumeric Supplement
            continue
        if (
            "SQUARED LATIN" in name
            or "NEGATIVE SQUARED LATIN" in name
            or "CIRCLED LATIN" in name
            or "PARENTHESIZED LATIN" in name
            or "MATHEMATICAL" in name
            or "FULLWIDTH" in name
        ):
            continue
        result.append(ch)
    return "".join(result)


def is_file_info(text):
    text = text.lower()
    return any(re.search(pattern, text) for pattern in FILE_INFO_PATTERNS)


def is_media_file_name(text: str) -> bool:
    """Return True if the text looks like a movie / TV / release file name."""
    text_lower = text.lower()
    hits = sum(1 for p in MEDIA_NAME_PATTERNS if re.search(p, text_lower, re.I))
    # Need at least 2 strong indicators (or 1 + common extension)
    return hits >= 2 or (
        hits >= 1 and re.search(r"\.(mkv|mp4|avi|m4v|ts|m2ts)\b", text_lower)
    )


def remove_links_and_usernames(text: str) -> str:
    """Minimal cleaning: links + usernames + Telegram hidden links only."""
    # Normal links
    text = re.sub(
        r"https?://\S+|www\.\S+|t\.me/\S+",
        "",
        text,
        flags=re.I,
    )

    # Markdown / Telegram hidden links  [text](url)  or  [](url)
    text = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", text)

    # Zero-width / invisible characters often used for hidden links
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)

    # Usernames in brackets
    text = re.sub(r"\[\s*@[^]]+\]", "", text)
    text = re.sub(r"\(\s*@[^)]+\)", "", text)

    # Leading @username
    text = re.sub(r"^\s*@\S+\s*[-:|]?\s*", "", text)

    # Remaining bare @usernames
    text = " ".join(word for word in text.split() if not word.startswith("@"))

    return text.strip()


def clean_file_name(file_name):
    """Clean and format the file name.

    - If it looks like a media release → full cleaning.
    - Otherwise → only remove links, usernames and Telegram hidden links.
    """
    file_name = str(file_name)
    file_name = remove_fancy_fonts(file_name)

    # ---------- Simple path (not a media file name) ----------
    if not is_media_file_name(file_name):
        return remove_links_and_usernames(file_name)

    # ---------- Full media-file cleaning ----------
    # Keep filename + file-info blocks, drop promotional blocks
    parts = re.split(r"\r?\n\s*\r?\n", file_name.strip())
    cleaned_parts = [parts[0]]
    for part in parts[1:]:
        if is_file_info(part):
            cleaned_parts.append(part)
        else:
            break
    file_name = "\n\n".join(cleaned_parts)

    # Links
    file_name = re.sub(
        r"https?://\S+|www\.\S+|t\.me/\S+", "", file_name, flags=re.I
    )
    # Markdown / Telegram hidden links
    file_name = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", file_name)
    # Zero-width chars
    file_name = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", file_name)

    # Usernames in brackets
    file_name = re.sub(r"\[\s*@[^]]+\]", "", file_name)
    file_name = re.sub(r"\(\s*@[^)]+\)", "", file_name)

    # (2025) → 2025
    #file_name = re.sub(r"\(((?:19|20)\d{2})\)", r"\1", file_name)

    # Leading @username
    file_name = re.sub(r"^\s*@\S+\s*[-:|]?\s*", "", file_name)

    # ---------------- Protect patterns ----------------
    # Audio channels 5.1 / 2.0 / 7.1
    file_name = re.sub(r"(?<=\d)\.(?=\d)", "<DOT>", file_name)

    # Episode ranges
    file_name = re.sub(
        r"(?i)\b(?:"
        r"s\d+\s*e(?:p)?\d+"
        r"|s\d+\s*e(?:p)?\d+\s*-\s*e?(?:p)?\d+"
        r"|e(?:p(?:isode)?)?\s*\d+"
        r"|\d+"
        r")\s*-\s*\d+\b",
        lambda m: m.group(0).replace("-", "<DASH>"),
        file_name,
    )

    def protect_language_block(match):
        text = match.group(0)
        text = text.replace("-", "<DASH>")
        text = text.replace("+", "<PLUS>")
        return text

    LANG_WORDS = (
        r"Hindi|English|Tamil|Telugu|Malayalam|Kannada|Japanese|Korean|Chinese|"
        r"French|German|Spanish|Italian|Russian|Arabic|Punjabi|Bengali|Gujarati|"
        r"Marathi|Urdu|Odia|Line|Thai|Indonesian|"
        r"Hin|Eng|Tam|Tel|Mal|Jap|Kor|Thai"
    )

    file_name = re.sub(
        rf"\[[^\]]*(?:{LANG_WORDS})[^\]]*\]",
        protect_language_block,
        file_name,
        flags=re.I,
    )
    file_name = re.sub(
        rf"\([^\)]*(?:{LANG_WORDS})[^\)]*\)",
        protect_language_block,
        file_name,
        flags=re.I,
    )

    # Short pairs HE-AAC, WEB-DL, HD-TC …
    file_name = re.sub(
        r"\b(?!@)([A-Za-z]{1,5})-([A-Za-z]{1,5})\b",
        lambda m: f"{m.group(1)}<DASH>{m.group(2)}",
        file_name,
    )

    # ---------------- Replace separators ----------------
    file_name = re.sub(r"[_.+-]", " ", file_name)

    # ---------------- Restore protected patterns ----------------
    file_name = (
        file_name.replace("<DOT>", ".")
        .replace("<DASH>", "-")
        .replace("<PLUS>", "+")
    )

    # Remaining @user tokens
    file_name = " ".join(
        word for word in file_name.split() if not word.startswith("@")
    )

    return file_name.strip()