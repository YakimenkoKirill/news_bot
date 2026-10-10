"""Схема ответа модели и его разбор."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.config import ChannelProfile
from app.pipeline.ai.client import AiResponseError

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_NON_WORD = re.compile(r"\W+")
_UNDERSCORES = re.compile(r"_+")
PROFILE_CONTEXT_KEY = "profile"


def normalize_hashtags(raw: list[Any], limit: int) -> list[str]:
    """Приводит хэштеги к виду #слово.

    Пробелы и прочие символы, недопустимые в хэштеге Telegram, заменяются на «_»,
    пустые и повторы (без учёта регистра) удаляются, лишние сверх limit отбрасываются.
    """
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw:
        body = _UNDERSCORES.sub("_", _NON_WORD.sub("_", str(item).strip().lstrip("#"))).strip("_")
        if not body or body.lower() in seen:
            continue
        seen.add(body.lower())
        tags.append(f"#{body}")
    return tags[:limit]


class AiResult(BaseModel):
    """Проверенный ответ модели.

    Длины title и summary и число хэштегов проверяются по профилю канала,
    переданному в контексте валидации под ключом «profile».
    """

    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)

    relevant: bool
    score: float = Field(ge=0, le=1)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    hashtags: list[str]
    category: str

    @field_validator("hashtags", mode="before")
    @classmethod
    def _normalize_hashtags(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, list):
            return value
        profile = _profile(info)
        return normalize_hashtags(value, profile.hashtags_max if profile else len(value))

    @model_validator(mode="after")
    def _check_lengths(self, info: ValidationInfo) -> AiResult:
        profile = _profile(info)
        if profile is None:
            return self
        if len(self.title) > profile.max_title_chars:
            raise ValueError(
                f"title длиннее {profile.max_title_chars} символов ({len(self.title)})"
            )
        if len(self.summary) > profile.max_summary_chars:
            raise ValueError(
                f"summary длиннее {profile.max_summary_chars} символов ({len(self.summary)})"
            )
        return self


def _profile(info: ValidationInfo) -> ChannelProfile | None:
    context = info.context or {}
    profile = context.get(PROFILE_CONTEXT_KEY)
    return profile if isinstance(profile, ChannelProfile) else None


def strip_code_fence(content: str) -> str:
    """Снимает обёртку ```json ... ```, если модель её добавила."""
    stripped = content.strip()
    match = _FENCE.match(stripped)
    return match.group(1) if match else stripped


def parse_ai_response(content: str, profile: ChannelProfile) -> AiResult:
    """Разбирает и проверяет ответ модели.

    Args:
        content: Текст ответа модели.
        profile: Профиль канала с ограничениями длины.

    Returns:
        Проверенный результат.

    Raises:
        AiResponseError: Невалидный JSON, нет ключа, неверные значения или длины.
    """
    try:
        data = json.loads(strip_code_fence(content))
    except json.JSONDecodeError as exc:
        raise AiResponseError(f"ответ модели не JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise AiResponseError("ответ модели не JSON-объект")
    try:
        return AiResult.model_validate(data, context={PROFILE_CONTEXT_KEY: profile})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'ответ'}: {error['msg']}"
            for error in exc.errors()
        )
        raise AiResponseError(f"ответ модели не прошёл проверку: {problems}") from exc
