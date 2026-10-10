"""Подбор порога SimHash по размеченному набору новостей.

Формат входа — JSONL, по одной новости в строке:
    {"id": "1", "title": "...", "text": "...", "cluster": "a"}
Новости с одинаковым cluster человек считает дублями.

Запуск:
    .venv/bin/python scripts/eval_dedup.py data/dedup_labeled.jsonl
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config import CONFIGS_DIR, DedupConfig
from app.pipeline.dedup import compute_simhash, hamming

DEFAULT_MAX_THRESHOLD = 10
DEFAULT_MAX_MISSED = 0.02
REQUIRED_FIELDS = ("id", "title", "text", "cluster")


class DatasetError(ValueError):
    """Ошибка в размеченном наборе."""


@dataclass(frozen=True)
class DedupRecord:
    """Размеченная новость."""

    id: str
    title: str
    text: str
    cluster: str


@dataclass(frozen=True)
class ThresholdStats:
    """Качество порога на парах новостей.

    Attributes:
        threshold: Максимальное расстояние Хэмминга для склейки.
        missed_rate: Доля пар-дублей, которые порог не склеил.
        false_merge_rate: Доля пар разных новостей, которые порог склеил.
    """

    threshold: int
    missed_rate: float
    false_merge_rate: float


def parse_records(lines: Iterable[str]) -> list[DedupRecord]:
    """Разбирает JSONL; пустые строки пропускаются.

    Raises:
        DatasetError: Нет записей, строка не JSON-объект или нет обязательного поля
            (в сообщении — номер строки).
    """
    records: list[DedupRecord] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"строка {number}: некорректный JSON ({exc.msg})") from exc
        if not isinstance(data, dict):
            raise DatasetError(f"строка {number}: ожидался JSON-объект")
        missing = [name for name in REQUIRED_FIELDS if data.get(name) in (None, "")]
        if missing:
            raise DatasetError(f"строка {number}: нет полей {', '.join(missing)}")
        records.append(DedupRecord(**{name: str(data[name]) for name in REQUIRED_FIELDS}))
    if not records:
        raise DatasetError("в наборе нет записей")
    return records


def evaluate(
    records: Sequence[DedupRecord],
    thresholds: Iterable[int],
    simhash: Callable[[DedupRecord], int],
) -> list[ThresholdStats]:
    """Считает долю пропущенных дублей и ложных склеек для каждого порога.

    Args:
        records: Размеченные новости.
        thresholds: Проверяемые пороги расстояния Хэмминга.
        simhash: Функция SimHash для записи.

    Returns:
        Статистика по каждому порогу; при отсутствии пар какого-то вида доля равна 0.
    """
    hashes = [simhash(record) for record in records]
    same: list[int] = []
    different: list[int] = []
    for (i, a), (j, b) in itertools.combinations(enumerate(records), 2):
        distance = hamming(hashes[i], hashes[j])
        (same if a.cluster == b.cluster else different).append(distance)

    stats: list[ThresholdStats] = []
    for threshold in thresholds:
        missed = sum(1 for d in same if d > threshold)
        merged = sum(1 for d in different if d <= threshold)
        stats.append(
            ThresholdStats(
                threshold=threshold,
                missed_rate=missed / len(same) if same else 0.0,
                false_merge_rate=merged / len(different) if different else 0.0,
            )
        )
    return stats


def recommend(stats: Sequence[ThresholdStats], max_missed: float) -> ThresholdStats | None:
    """Минимальный порог, при котором доля пропущенных дублей не больше max_missed."""
    ordered = sorted(stats, key=lambda s: s.threshold)
    return next((s for s in ordered if s.missed_rate <= max_missed), None)


def format_report(stats: Sequence[ThresholdStats], max_missed: float) -> str:
    """Таблица порогов и рекомендация."""
    lines = [
        "порог | пропущено дублей | ложные склейки",
        "----- | ---------------- | --------------",
    ]
    for s in stats:
        lines.append(f"{s.threshold:>5} | {s.missed_rate:>15.1%} | {s.false_merge_rate:>13.1%}")
    best = recommend(stats, max_missed)
    if best is None:
        lines.append(f"Ни один порог не даёт пропуск <= {max_missed:.0%}: нужен больший порог.")
    else:
        lines.append(
            f"Рекомендуемый dedup.simhash_max_distance: {best.threshold} "
            f"(пропуск {best.missed_rate:.1%}, ложные склейки {best.false_merge_rate:.1%})"
        )
    return "\n".join(lines)


def load_dedup_config(configs_dir: Path = CONFIGS_DIR) -> DedupConfig:
    """Секция dedup из configs/base.yaml (без чтения окружения и секретов)."""
    data = yaml.safe_load((configs_dir / "base.yaml").read_text(encoding="utf-8"))
    return DedupConfig(**data["dedup"])


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа скрипта; возвращает код выхода."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("path", type=Path, help="JSONL с размеченными новостями")
    parser.add_argument("--max-threshold", type=int, default=DEFAULT_MAX_THRESHOLD)
    parser.add_argument("--max-missed", type=float, default=DEFAULT_MAX_MISSED)
    args = parser.parse_args(argv)

    chars = load_dedup_config().first_paragraph_chars
    try:
        records = parse_records(args.path.read_text(encoding="utf-8").splitlines())
    except (DatasetError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    stats = evaluate(
        records,
        range(args.max_threshold + 1),
        lambda record: compute_simhash(record.title, record.text, chars),
    )
    print(f"Записей: {len(records)}, кластеров: {len({r.cluster for r in records})}")
    print(format_report(stats, args.max_missed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
