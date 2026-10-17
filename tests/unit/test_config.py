from pathlib import Path

import pytest
import structlog.testing
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


def test_alerts_retention_and_purge_interval(configs_dir: Path) -> None:
    settings = load_settings(configs_dir)
    assert settings.alerts.retention_days == 30
    assert settings.scheduler.purge_every_h == 24


def test_retention_days_must_be_positive(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("alerts:\n  retention_days: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="retention_days"):
        load_settings(configs_dir, "prod")


def test_purge_interval_must_be_positive(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("scheduler:\n  purge_every_h: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="purge_every_h"):
        load_settings(configs_dir, "prod")


def test_bot_error_preview_chars(configs_dir: Path) -> None:
    assert load_settings(configs_dir).bot.error_preview_chars == 80


def test_daily_report_keys(configs_dir: Path) -> None:
    settings = load_settings(configs_dir)
    assert settings.daily_report.enabled is True
    assert settings.daily_report.send_at == "00:05"
    assert settings.daily_report.send_time.hour == 0
    assert settings.daily_report.send_time.minute == 5
    assert settings.daily_report.report_day == "previous"
    assert settings.scheduler.daily_report_every_min == 5


def test_report_day_current_allowed(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        'daily_report:\n  report_day: "current"\n  send_at: "01:00"\n', encoding="utf-8"
    )
    assert load_settings(configs_dir, "prod").daily_report.report_day == "current"


def test_invalid_report_day_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        'daily_report:\n  report_day: "вчера"\n', encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="report_day"):
        load_settings(configs_dir, "prod")


def test_default_report_day_without_warning(configs_dir: Path) -> None:
    """previous в тихие часы — предупреждений нет."""
    with structlog.testing.capture_logs() as logs:
        load_settings(configs_dir)
    assert [entry for entry in logs if entry["event"] == "config.inconsistent"] == []


def test_current_report_day_outside_quiet_hours_warns(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        'daily_report:\n  report_day: "current"\n  send_at: "23:30"\n', encoding="utf-8"
    )
    with structlog.testing.capture_logs() as logs:
        settings = load_settings(configs_dir, "prod")
    warnings = [entry for entry in logs if entry["event"] == "config.inconsistent"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert "до конца публикаций" in warnings[0]["message"]
    assert config.config_warnings(settings)


def test_current_report_day_inside_quiet_hours_without_warning(configs_dir: Path) -> None:
    """send_at внутри тихих часов 00:00–07:00: публикаций за сутки уже не будет."""
    (configs_dir / "prod.yaml").write_text(
        'daily_report:\n  report_day: "current"\n  send_at: "00:30"\n', encoding="utf-8"
    )
    assert config.config_warnings(load_settings(configs_dir, "prod")) == []


def test_disabled_report_does_not_warn(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        'daily_report:\n  enabled: false\n  report_day: "current"\n  send_at: "23:30"\n',
        encoding="utf-8",
    )
    assert config.config_warnings(load_settings(configs_dir, "prod")) == []


def test_without_quiet_hours_current_report_day_warns(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        'publisher:\n  quiet_hours: []\ndaily_report:\n  report_day: "current"\n',
        encoding="utf-8",
    )
    assert config.config_warnings(load_settings(configs_dir, "prod")) != []


@pytest.mark.parametrize("bad", ["25:00", "23-30", "вечером", ""])
def test_invalid_send_at_rejected(configs_dir: Path, bad: str) -> None:
    (configs_dir / "prod.yaml").write_text(f'daily_report:\n  send_at: "{bad}"\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="send_at"):
        load_settings(configs_dir, "prod")


def test_ui_section(configs_dir: Path) -> None:
    ui = load_settings(configs_dir).ui
    assert ui.single_screen is True
    assert ui.delete_user_messages is True
    assert ui.delete_batch_size == 100


def test_ui_batch_size_must_be_positive(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("ui:\n  delete_batch_size: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="delete_batch_size"):
        load_settings(configs_dir, "prod")


def test_ui_unknown_key_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("ui:\n  surprise: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="surprise"):
        load_settings(configs_dir, "prod")


def test_cluster_bonus_keys(configs_dir: Path) -> None:
    publisher = load_settings(configs_dir).publisher
    assert publisher.cluster_bonus == 0.05
    assert publisher.cluster_bonus_max == 3


def test_zero_cluster_bonus_allowed(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        "publisher:\n  cluster_bonus: 0\n  cluster_bonus_max: 0\n", encoding="utf-8"
    )
    publisher = load_settings(configs_dir, "prod").publisher
    assert (publisher.cluster_bonus, publisher.cluster_bonus_max) == (0, 0)


@pytest.mark.parametrize("key", ["cluster_bonus", "cluster_bonus_max"])
def test_negative_cluster_bonus_rejected(configs_dir: Path, key: str) -> None:
    (configs_dir / "prod.yaml").write_text(f"publisher:\n  {key}: -1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match=key):
        load_settings(configs_dir, "prod")


def test_retention_section(configs_dir: Path) -> None:
    settings = load_settings(configs_dir)
    assert settings.retention.text_days == 30
    assert settings.retention.batch_size == 1000
    assert settings.retention.max_run_s == 60
    assert settings.scheduler.retention_every_h == 24


def test_retention_text_days_must_cover_dedup_window(configs_dir: Path) -> None:
    """Окно дублей 48 часов: очищать тексты раньше третьих суток нельзя."""
    (configs_dir / "prod.yaml").write_text("retention:\n  text_days: 2\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="text_days"):
        load_settings(configs_dir, "prod")

    (configs_dir / "prod.yaml").write_text("retention:\n  text_days: 3\n", encoding="utf-8")
    assert load_settings(configs_dir, "prod").retention.text_days == 3


def test_retention_window_checked_against_configured_dedup(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        "dedup:\n  window_h: 240\nretention:\n  text_days: 10\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="text_days"):
        load_settings(configs_dir, "prod")

    (configs_dir / "prod.yaml").write_text(
        "dedup:\n  window_h: 240\nretention:\n  text_days: 11\n", encoding="utf-8"
    )
    assert load_settings(configs_dir, "prod").retention.text_days == 11


@pytest.mark.parametrize(
    ("key", "value"),
    [("text_days", 0), ("batch_size", 0), ("max_run_s", 0)],
)
def test_non_positive_retention_values_rejected(configs_dir: Path, key: str, value: int) -> None:
    (configs_dir / "prod.yaml").write_text(f"retention:\n  {key}: {value}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match=key):
        load_settings(configs_dir, "prod")


def test_retention_unknown_key_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("retention:\n  surprise: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="surprise"):
        load_settings(configs_dir, "prod")


def test_simhash_max_distance(configs_dir: Path) -> None:
    assert load_settings(configs_dir).dedup.simhash_max_distance == 3


@pytest.mark.parametrize("value", [4, 10, 64])
def test_too_large_simhash_distance_rejected(configs_dir: Path, value: int) -> None:
    """Поиск по частям SimHash корректен только до расстояния 3."""
    (configs_dir / "prod.yaml").write_text(
        f"dedup:\n  simhash_max_distance: {value}\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="simhash_max_distance"):
        load_settings(configs_dir, "prod")


@pytest.mark.parametrize("value", [0, 1, 3])
def test_allowed_simhash_distances(configs_dir: Path, value: int) -> None:
    (configs_dir / "prod.yaml").write_text(
        f"dedup:\n  simhash_max_distance: {value}\n", encoding="utf-8"
    )
    assert load_settings(configs_dir, "prod").dedup.simhash_max_distance == value


def test_negative_simhash_distance_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("dedup:\n  simhash_max_distance: -1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="simhash_max_distance"):
        load_settings(configs_dir, "prod")


def test_ai_max_output_tokens(configs_dir: Path) -> None:
    assert load_settings(configs_dir).ai.max_output_tokens == 800


def test_zero_max_output_tokens_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("ai:\n  max_output_tokens: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="max_output_tokens"):
        load_settings(configs_dir, "prod")


def test_stuck_after_min_and_recover_interval(configs_dir: Path) -> None:
    settings = load_settings(configs_dir)
    assert settings.publisher.stuck_after_min == 15
    assert settings.scheduler.recover_every_min == 10


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_stuck_after_min_rejected(configs_dir: Path, value: int) -> None:
    (configs_dir / "prod.yaml").write_text(
        f"publisher:\n  stuck_after_min: {value}\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="stuck_after_min"):
        load_settings(configs_dir, "prod")


def test_non_positive_recover_interval_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text("scheduler:\n  recover_every_min: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="recover_every_min"):
        load_settings(configs_dir, "prod")


def test_max_queued_per_source(configs_dir: Path) -> None:
    assert load_settings(configs_dir).publisher.max_queued_per_source == 4


def test_zero_queue_limit_allowed(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        "publisher:\n  max_queued_per_source: 0\n", encoding="utf-8"
    )
    assert load_settings(configs_dir, "prod").publisher.max_queued_per_source == 0


def test_negative_queue_limit_rejected(configs_dir: Path) -> None:
    (configs_dir / "prod.yaml").write_text(
        "publisher:\n  max_queued_per_source: -1\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="max_queued_per_source"):
        load_settings(configs_dir, "prod")
