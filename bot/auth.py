from __future__ import annotations

from typing import Any

from bot.config import Settings


def is_allowed(settings: Settings, user: Any | None, chat: Any | None) -> bool:
    if not settings.allowed_user_ids and not settings.allowed_chat_ids:
        return True

    if user and user.id in settings.allowed_user_ids:
        return True

    if chat and chat.id in settings.allowed_chat_ids:
        return True

    return False


def is_private_allowed(settings: Settings, user: Any | None) -> bool:
    if not settings.allowed_user_ids:
        return True
    return bool(user and user.id in settings.allowed_user_ids)


def is_group_allowed(settings: Settings, chat: Any | None) -> bool:
    if not settings.allowed_chat_ids:
        return True
    return bool(chat and chat.id in settings.allowed_chat_ids)
