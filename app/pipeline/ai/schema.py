"""Схема ответа модели и его разбор."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

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

_ModelT = TypeVar("_ModelT", bound=BaseModel)

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


class ScreenResult(BaseModel):
    """Проверенный ответ отбора: подходит ли новость каналу и насколько интересна.

    Лишние ключи запрещены: ответ отбора короткий, и всё, что в нём сверх
    relevant и score, — признак того, что модель поняла задачу неверно.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    relevant: bool
    score: float = Field(ge=0, le=1)


class SummaryResult(BaseModel):
    """Проверенный пересказ новости.

    Длины title и summary и число хэштегов проверяются по профилю канала,
    переданному в контексте валидации под ключом «profile».
    """

    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)

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
    def _check_lengths(self, info: ValidationInfo) -> SummaryResult:
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


class AiResult(SummaryResult):
    """Ответ промпта v3: оценка и пересказ в одном ответе (ai.two_stage = false)."""

    relevant: bool
    score: float = Field(ge=0, le=1)


def _profile(info: ValidationInfo) -> ChannelProfile | None:
    context = info.context or {}
    profile = context.get(PROFILE_CONTEXT_KEY)
    return profile if isinstance(profile, ChannelProfile) else None


def strip_code_fence(content: str) -> str:
    """Снимает обёртку ```json ... ```, если модель её добавила."""
    stripped = content.strip()
    match = _FENCE.match(stripped)
    return match.group(1) if match else stripped


def _load_object(content: str) -> dict[str, Any]:
    try:
        data = json.loads(strip_code_fence(content))
    except json.JSONDecodeError as exc:
        raise AiResponseError(f"ответ модели не JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise AiResponseError("ответ модели не JSON-объект")
    return data


def _validate(model: type[_ModelT], data: dict[str, Any], context: dict[str, Any]) -> _ModelT:
    try:
        return model.model_validate(data, context=context)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'ответ'}: {error['msg']}"
            for error in exc.errors()
        )
        raise AiResponseError(f"ответ модели не прошёл проверку: {problems}") from exc


def parse_screen_response(content: str) -> ScreenResult:
    """Разбирает и проверяет ответ отбора.

    Args:
        content: Текст ответа модели.

    Returns:
        Оценка темы и важности новости.

    Raises:
        AiResponseError: Невалидный JSON, нет ключа, лишние ключи или неверные значения.
    """
    return _validate(ScreenResult, _load_object(content), {})


def parse_summary_response(content: str, profile: ChannelProfile) -> SummaryResult:
    """Разбирает и проверяет ответ с пересказом.

    Args:
        content: Текст ответа модели.
        profile: Профиль канала с ограничениями длины.

    Returns:
        Проверенный пересказ.

    Raises:
        AiResponseError: Невалидный JSON, нет ключа, неверные значения или длины.
    """
    return _validate(SummaryResult, _load_object(content), {PROFILE_CONTEXT_KEY: profile})


def parse_ai_response(content: str, profile: ChannelProfile) -> AiResult:
    """Разбирает ответ промпта v3: оценка и пересказ вместе (ai.two_stage = false).

    Args:
        content: Текст ответа модели.
        profile: Профиль канала с ограничениями длины.

    Returns:
        Проверенный результат.

    Raises:
        AiResponseError: Невалидный JSON, нет ключа, неверные значения или длины.
    """
    return _validate(AiResult, _load_object(content), {PROFILE_CONTEXT_KEY: profile})
