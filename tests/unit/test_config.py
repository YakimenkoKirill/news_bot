from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app import config
from app.config import Settings, get_settings, load_settings
from app.domain.alerts import AlertKind

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/news",
    "TELEGRAM_BOT_TOKEN": "bot-token-secret",
    "TELEGRAM_CHANNEL_ID": "-1001234567890",
    "TELEGRAM_ADMIN_IDS": "111,222",
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": "telethon-hash-secret",
    "DEEPSEEK_API_KEY": "deepseek-key-secret",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in [*REQUIRED_ENV, "ENV", "APP__TIMEZONE", "LOG_LEVEL"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
def configs_dir(tmp_path: Path) -> Path:
    path = tmp_path / "configs"
    path.mkdir()
    base = yaml.safe_load((config.CONFIGS_DIR / "base.yaml").read_text(encoding="utf-8"))
    base["app"]["timezone"] = "Europe/Moscow"
    (path / "base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (path / "dev.yaml").write_text("app:\n  timezone: Asia/Tokyo\n", encoding="utf-8")
    (path / "prod.yaml").write_text("# пусто\n", encoding="utf-8")
    return path


def test_env_yaml_overrides_base(configs_dir: Path) -> None:
    assert load_settings(configs_dir, "dev").app.timezone == "Asia/Tokyo"
    assert load_settings(configs_dir, "prod").app.timezone == "Europe/Moscow"


def test_deep_merge_keeps_nested_keys() -> None:
    merged = config.deep_merge({"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"y": 3}})
    assert merged == {"a": {"x": 1, "y": 3}, "b": 1}


def test_env_from_environment(configs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    settings = load_settings(configs_dir)
    assert settings.env == "prod"
    assert settings.app.timezone == "Europe/Moscow"


def test_env_defaults_to_dev(configs_dir: Path) -> None:
    assert load_settings(configs_dir).env == "dev"


def test_environment_variable_beats_yaml(
    configs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP__TIMEZONE", "Europe/Berlin")
    assert load_settings(configs_dir, "dev").app.timezone == "Europe/Berlin"


def test_dotenv_is_read(configs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TELETHON_API_ID")
    (tmp_path / ".env").write_text("TELETHON_API_ID=777\n", encoding="utf-8")
    assert load_settings(configs_dir, "dev").telethon_api_id == 777


def test_parsed_values_and_defaults(configs_dir: Path) -> None:
    settings = load_settings(configs_dir, "dev")
    assert settings.telegram_admin_ids == [111, 222]
    assert settings.telegram_channel_id == -1001234567890
    assert settings.telethon_session_path == Path("data/telethon")
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.log_level == "INFO"


def test_admin_ids_json_list(configs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "[1, 2, 3]")
    assert load_settings(configs_dir, "dev").telegram_admin_ids == [1, 2, 3]


def test_get_settings_is_singleton(configs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "CONFIGS_DIR", configs_dir)
    first = get_settings()
    assert isinstance(first, Settings)
    assert get_settings() is first


def test_missing_required_variable(configs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValidationError, match="database_url"):
        load_settings(configs_dir, "dev")


def test_unknown_env(configs_dir: Path) -> None:
    with pytest.raises(ValueError, match="staging"):
        load_settings(configs_dir, "staging")


def test_unknown_env_from_environment(configs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "staging")
    with pytest.raises(ValueError, match="staging"):
        load_settings(configs_dir)


def test_extra_nested_yaml_key(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("app:\n  unknown: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown"):
        load_settings(configs_dir, "prod")


def test_extra_top_level_yaml_key(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("surprise:\n  a: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="surprise"):
        load_settings(configs_dir, "prod")


def test_invalid_timezone(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("app:\n  timezone: Mars/Olympus\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="Mars/Olympus"):
        load_settings(configs_dir, "prod")


def test_empty_admin_ids(configs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "")
    with pytest.raises(ValidationError, match="telegram_admin_ids"):
        load_settings(configs_dir, "dev")


def test_secrets_not_in_repr(configs_dir: Path) -> None:
    settings = load_settings(configs_dir, "dev")
    text = repr(settings) + str(settings)
    for secret in ("bot-token-secret", "telethon-hash-secret", "deepseek-key-secret"):
        assert secret not in text
    assert settings.telegram_bot_token.get_secret_value() == "bot-token-secret"


def test_repository_configs_are_valid() -> None:
    for env in config.ENVIRONMENTS:
        assert load_settings(config.CONFIGS_DIR, env).app.timezone


def test_alerts_silent_kinds_and_message_limit(configs_dir: Path) -> None:
    settings = load_settings(configs_dir)
    assert settings.alerts.silent_kinds == [AlertKind.AI_ITEM_FAILED]
    assert settings.alerts.max_message_chars == 2000


def test_alerts_unknown_silent_kind_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        'alerts:\n  silent_kinds: ["no_such_kind"]\n', encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="silent_kinds"):
        load_settings(configs_dir, "prod")


def test_alerts_message_limit_must_be_positive(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("alerts:\n  max_message_chars: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="max_message_chars"):
        load_settings(configs_dir, "prod")
