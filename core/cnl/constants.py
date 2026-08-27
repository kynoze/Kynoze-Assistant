from __future__ import annotations
RULE_LIMIT = 10
MAX_TARGETS_PER_SOURCE = 10
RULE_CACHE_TTL_SECONDS = 30.0
DAILY_FORWARD_LIMIT = 2000
DEFAULT_DB_NAME = "cnl_autopost"
DUPE_DB_NAME = "dupedb"
ALBUM_WAIT_SECONDS = 2.8
FORWARD_CONCURRENCY = 5
ALLOWED_CAPTION_POSITIONS = {"start", "end", "end_with_gap"}
ALLOWED_MEDIA_TYPES = {
    "all", "photo", "video", "document", "sticker", "animation",
    "audio", "voice", "text", "poll", "contact", "location", "venue",
}
ALLOWED_FORWARD_VIA = {"user_bot", "user_account"}
RULE_FORWARD_PROJECTION = {
    "_id": 1, "source_chat_id": 1, "target_chat_id": 1, "owner_id": 1,
    "enabled": 1, "add_caption": 1, "caption_position": 1, "custom_caption": 1,
    "remove_old_caption": 1, "replacements": 1, "block_words": 1,
    "whitelist_words": 1, "buttons": 1, "forward_tag": 1, "remove_links": 1,
    "allowed_types": 1, "delay": 1, "anti_dupe": 1, "forward_via": 1,
}
GLOBAL_COPY_FILTER_KEYS = {
    "block_words", "whitelist_words", "replacements", "add_caption",
    "caption_position", "custom_caption", "remove_old_caption", "remove_links",
    "buttons", "delay", "anti_dupe", "forward_tag", "allowed_types",
    "target_chat_id", "enabled",
}
NOT_CONFIGURED = (
    "⚠️ **CNL Database is not configured.**\n"
    "Please add your MongoDB URI to use CNL features."
)
