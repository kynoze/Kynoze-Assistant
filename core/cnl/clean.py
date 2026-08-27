"""Caption / filename cleaning utilities for CNL."""
from __future__ import annotations
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
    r"\bchapters?\b",
]
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
        if 0x1D400 <= cp <= 0x1D7FF: continue
        if 0xFF01 <= cp <= 0xFF5E: continue
        if 0x2460 <= cp <= 0x24FF: continue
        if 0x1F100 <= cp <= 0x1F1FF: continue
        if any(x in name for x in ("SQUARED LATIN", "NEGATIVE SQUARED LATIN", "CIRCLED LATIN",
                                    "PARENTHESIZED LATIN", "MATHEMATICAL", "FULLWIDTH")):
            continue
        result.append(ch)
    return "".join(result)

def is_file_info(text):
    text = text.lower()
    return any(re.search(p, text) for p in FILE_INFO_PATTERNS)

def is_media_file_name(text: str) -> bool:
    text_lower = text.lower()
    hits = sum(1 for p in MEDIA_NAME_PATTERNS if re.search(p, text_lower, re.I))
    return hits >= 2 or (hits >= 1 and re.search(r"\.(mkv|mp4|avi|m4v|ts|m2ts)\b", text_lower))

def remove_links_and_usernames(text: str) -> str:
    text = re.sub(r"https?://\S+|www\.\S+|t\.me/\S+", "", text, flags=re.I)
    text = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    text = re.sub(r"\[\s*@[^]]+\]", "", text)
    text = re.sub(r"\(\s*@[^)]+\)", "", text)
    text = re.sub(r"^\s*@\S+\s*[-:|]?\s*", "", text)
    text = " ".join(w for w in text.split() if not w.startswith("@"))
    return re.sub(r"\s{2,}", " ", text).strip()

def clean_file_name(file_name: str) -> str:
    if not file_name:
        return ""
    name = remove_fancy_fonts(file_name)
    name = remove_links_and_usernames(name)
    name = re.sub(r"[\[\](){}]", " ", name)
    name = re.sub(r"[._\-]{2,}", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name
