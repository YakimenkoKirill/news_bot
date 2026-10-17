from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import InlineKeyboardMarkup, Update

from app.bot import BotDeps, create_dispatcher, texts
from app.bot.handlers import settings as settings_handlers
from app.bot.handlers.settings import SettingsDeps, format_value
from app.bot.keyboards import ST_EDIT, ST_LIST, ST_PRESET, ST_RESET, SettingsAction
from app.config import Settings, load_settings
from app.runtime_config import KEYS_BY_NAME, RuntimeConfig, RuntimeSnapshot
from tests.bot_helpers import callback_update, make_bot, message_update
from tests.config_helpers import bot_config, configs_copy

ADMIN = 111
CALLBACK_DATA_LIMIT = 64


class FakeRuntime:
    """RuntimeConfig на словаре: валидация настоящая, БД не нужна."""

    def __init__(self, settings: Settings) -> None:
        self._base = RuntimeSnapshot(
            channel=settings.channel, ai=settings.ai, publisher=settings.publisher
        )
        self._overrides: dict[str, Any] = {}
        self.sets: list[tuple[str, Any, int]] = []
        self.resets: list[tuple[str, int]] = []
        self._real = RuntimeConfig(self._scope, self._repo, settings)

    def _scope(self) -> Any:
        raise AssertionError("БД в этом тесте не нужна")

    def _repo(self, session: Any) -> Any:
        raise AssertionError("БД в этом тесте не нужна")

    @property
    def base(self) -> RuntimeSnapshot:
        return self._base

    async def snapshot(self) -> RuntimeSnapshot:
        return self._real._apply(self._overrides)

    async def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    async def set(self, key: str, raw_value: Any, by: int) -> None:
        from app.runtime_config import validate_value

        spec = KEYS_BY_NAME.get(key)
        if spec is None:
            raise ValueError(f"неизвестный параметр {key!r}")
        section = getattr(self._base, spec.section)
        self._overrides[key] = validate_value(spec, raw_value, section)
        self.sets.append((key, raw_value, by))

    async def reset(self, key: str, by: int) -> None:
        self._overrides.pop(key, None)
        self.resets.append((key, by))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    return load_settings(configs_copy(tmp_path, monkeypatch), "dev")


@pytest.fixture
def runtime(settings: Settings) -> FakeRuntime:
    return FakeRuntime(settings)


def _buttons(sent: SendMessage) -> list[str]:
    """Подписи кнопок под сообщением."""
    markup = sent.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.text for row in markup.inline_keyboard for button in row]


class Chat:
    """Диалог с ботом: один dispatcher, поэтому состояние FSM живёт между шагами."""

    def __init__(self, runtime: FakeRuntime) -> None:
        class NoAdmins:
            async def is_admin(self, telegram_id: int) -> bool:
                return False

        deps = BotDeps(
            settings=Settings.model_construct(telegram_admin_ids=[ADMIN], bot=bot_config()),
            admin_repo=NoAdmins(),
            routers=[settings_handlers.create_router()],
            workflow_data={"settings_deps": SettingsDeps(runtime=runtime, cache_s=30)},
        )
        self.dispatcher = create_dispatcher(deps)
        self.bot, self.session = make_bot()

    async def feed(self, update: Update) -> None:
        """Обрабатывает апдейт; запросы предыдущего шага забываются."""
        self.session.requests.clear()
        await self.dispatcher.feed_update(self.bot, update)

    async def send(self, text: str) -> None:
        """Сообщение от администратора."""
        await self.feed(message_update(ADMIN, text))

    async def click(self, action: str, key: str = "", value: str = "") -> None:
        """Нажатие кнопки /settings."""
        await self.feed(
            callback_update(ADMIN, SettingsAction(action=action, key=key, value=value).pack())
        )

    def sent(self) -> list[SendMessage]:
        """Отправленные сообщения последнего шага."""
        return [r for r in self.session.requests if isinstance(r, SendMessage)]

    def alerts(self) -> list[AnswerCallbackQuery]:
        """Ответы на callback последнего шага."""
        return [r for r in self.session.requests if isinstance(r, AnswerCallbackQuery)]


async def test_command_lists_all_parameters(runtime: FakeRuntime, settings: Settings) -> None:
    chat = Chat(runtime)
    await chat.send("/settings")
    [sent] = chat.sent()

    assert texts.SETTINGS_TITLE in sent.text
    assert "не позже 30 с" in sent.text
    for label in texts.SETTINGS_LABELS.values():
        assert label in sent.text
    assert settings.channel.topic in sent.text
    assert str(settings.ai.min_score) in sent.text
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    buttons = _buttons(sent)
    assert buttons == list(texts.SETTINGS_LABELS.values())
    assert "изменено" not in sent.text


async def test_changed_parameter_marked(runtime: FakeRuntime, settings: Settings) -> None:
    await runtime.set("ai.min_score", "0.9", ADMIN)
    chat = Chat(runtime)
    await chat.send("/settings")
    [sent] = chat.sent()
    assert "0.9" in sent.text
    assert f"изменено (в конфиге {settings.ai.min_score})" in sent.text


async def test_edit_button_asks_value_with_hint(runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_EDIT, "quiet")
    [sent] = chat.sent()
    assert texts.SETTINGS_LABELS["quiet"] in sent.text
    assert KEYS_BY_NAME["publisher.quiet_hours"].hint in sent.text
    assert "/cancel" in sent.text
    buttons = _buttons(sent)
    assert "Выключить" in buttons
    assert texts.SETTINGS_RESET_BUTTON in buttons


@pytest.mark.parametrize("code", ["score", "decay"])
async def test_presets_offered(code: str, runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_EDIT, code)
    [sent] = chat.sent()
    buttons = _buttons(sent)
    for _value, label in texts.SETTINGS_PRESETS[code]:
        assert label in buttons


async def test_preset_applies_value(runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_PRESET, "score", "0.7")
    assert runtime.sets == [("ai.min_score", "0.7", ADMIN)]
    assert (await runtime.snapshot()).ai.min_score == 0.7
    sent = chat.sent()
    assert texts.SETTINGS_SAVED.format(label=texts.SETTINGS_LABELS["score"], value="0.7") in [
        m.text for m in sent
    ]
    assert texts.SETTINGS_TITLE in sent[-1].text, "после изменения показан обновлённый список"


async def test_quiet_hours_preset_disables(runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_PRESET, "quiet", "")
    assert (await runtime.snapshot()).publisher.quiet_hours == []
    assert texts.SETTINGS_EMPTY_VALUE in chat.sent()[0].text


@pytest.mark.parametrize(
    ("code", "value", "check"),
    [
        ("topic", "спорт и финансы", lambda s: s.channel.topic == "спорт и финансы"),
        ("score", "0,75", lambda s: s.ai.min_score == 0.75),
        ("interval", "45", lambda s: s.publisher.min_interval_min == 45),
        (
            "quiet",
            "23:30-07:30",
            lambda s: s.publisher.quiet_hours == ["23:30", "07:30"],
        ),
        ("limit", "2", lambda s: s.publisher.max_queued_per_source == 2),
        ("decay", "0.1", lambda s: s.publisher.score_decay_per_h == 0.1),
    ],
)
async def test_typed_value_saved(code: str, value: str, check: Any, runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_EDIT, code)
    await chat.send(value)
    assert check(await runtime.snapshot())
    assert texts.SETTINGS_TITLE in chat.sent()[-1].text


@pytest.mark.parametrize(
    ("code", "value"),
    [
        ("topic", "ок"),
        ("score", "высокий"),
        ("score", "1.5"),
        ("interval", "0"),
        ("quiet", "25:00-07:00"),
        ("quiet", "23:00"),
        ("limit", "-1"),
        ("decay", "очень"),
    ],
)
async def test_bad_value_asks_again(code: str, value: str, runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_EDIT, code)
    await chat.send(value)

    [sent] = chat.sent()
    assert sent.text.startswith("❌ Не сохранил")
    assert "/cancel" in sent.text
    assert await runtime.overrides() == {}, "значение не записано"


async def test_state_kept_after_error(runtime: FakeRuntime) -> None:
    """После ошибки бот всё ещё ждёт значение того же параметра."""
    chat = Chat(runtime)
    await chat.click(ST_EDIT, "score")
    await chat.send("много")
    await chat.send("0.65")
    assert (await runtime.snapshot()).ai.min_score == 0.65
    assert texts.SETTINGS_TITLE in chat.sent()[-1].text


async def test_reset_returns_config_value(runtime: FakeRuntime, settings: Settings) -> None:
    await runtime.set("publisher.min_interval_min", "90", ADMIN)
    chat = Chat(runtime)
    await chat.click(ST_RESET, "interval")

    assert runtime.resets == [("publisher.min_interval_min", ADMIN)]
    snapshot = await runtime.snapshot()
    assert snapshot.publisher.min_interval_min == settings.publisher.min_interval_min
    assert "снова как в конфиге" in chat.sent()[0].text


async def test_back_to_list(runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_LIST)
    [sent] = chat.sent()
    assert texts.SETTINGS_TITLE in sent.text


async def test_unknown_key_reports_error(runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_EDIT, "nope")
    [alert] = chat.alerts()
    assert alert.text == texts.SETTINGS_UNKNOWN
    assert chat.sent() == []


async def test_cancel_stops_waiting_for_value(runtime: FakeRuntime) -> None:
    """После /cancel присланное число уже не меняет параметр."""
    chat = Chat(runtime)
    await chat.click(ST_EDIT, "score")
    await chat.send("/cancel")
    assert [m.text for m in chat.sent()] == [texts.ADD_CANCELLED]
    await chat.send("0.95")
    assert await runtime.overrides() == {}
    assert chat.sent() == [], "ввод больше не ждём"


async def test_value_not_awaited_after_save(runtime: FakeRuntime) -> None:
    chat = Chat(runtime)
    await chat.click(ST_EDIT, "score")
    await chat.send("0.65")
    await chat.send("0.95")
    assert (await runtime.snapshot()).ai.min_score == 0.65
    assert chat.sent() == []


def test_callback_data_fits_telegram_limit() -> None:
    for action in (ST_EDIT, ST_PRESET, ST_RESET, ST_LIST):
        for code in texts.SETTINGS_CODES:
            for value, _label in texts.SETTINGS_PRESETS.get(code, (("0.02", "0.02"),)):
                packed = SettingsAction(action=action, key=code, value=value).pack()
                assert len(packed.encode()) <= CALLBACK_DATA_LIMIT, packed


def test_codes_cover_all_runtime_keys() -> None:
    assert set(texts.SETTINGS_CODES.values()) == set(KEYS_BY_NAME)
    assert set(texts.SETTINGS_LABELS) == set(texts.SETTINGS_CODES)


def test_format_value_readable() -> None:
    assert format_value(["23:00", "07:00"]) == "23:00–07:00"
    assert format_value([]) == texts.SETTINGS_EMPTY_VALUE
    assert format_value(0.6) == "0.6"
