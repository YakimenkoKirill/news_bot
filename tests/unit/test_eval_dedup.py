from pathlib import Path

import pytest

from scripts.eval_dedup import (
    DatasetError,
    DedupRecord,
    ThresholdStats,
    evaluate,
    format_report,
    main,
    parse_records,
    recommend,
)

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "dedup" / "sample.jsonl"


def _rec(record_id: str, cluster: str) -> DedupRecord:
    return DedupRecord(id=record_id, title="t", text="x", cluster=cluster)


def test_known_answer_on_synthetic_hashes() -> None:
    records = [_rec("a1", "a"), _rec("a2", "a"), _rec("b1", "b"), _rec("c1", "c")]
    hashes = {"a1": 0b0000, "a2": 0b0011, "b1": 0b0111, "c1": 0b1111_0000_0000}
    stats = evaluate(records, range(5), lambda r: hashes[r.id])
    # Пары-дубли: (a1, a2) с расстоянием 2.
    # Разные: a1-b1=3, a1-c1=4, a2-b1=1, a2-c1=6, b1-c1=7.
    assert stats == [
        ThresholdStats(0, missed_rate=1.0, false_merge_rate=0.0),
        ThresholdStats(1, missed_rate=1.0, false_merge_rate=0.2),
        ThresholdStats(2, missed_rate=0.0, false_merge_rate=0.2),
        ThresholdStats(3, missed_rate=0.0, false_merge_rate=0.4),
        ThresholdStats(4, missed_rate=0.0, false_merge_rate=0.6),
    ]
    best = recommend(stats, 0.02)
    assert best is not None
    assert best.threshold == 2


def test_no_threshold_reaches_target() -> None:
    records = [_rec("a1", "a"), _rec("a2", "a")]
    stats = evaluate(records, range(3), lambda r: 0 if r.id == "a1" else 0b11111)
    assert recommend(stats, 0.02) is None
    assert "Ни один порог" in format_report(stats, 0.02)


def test_parse_sample_fixture() -> None:
    records = parse_records(SAMPLE.read_text(encoding="utf-8").splitlines())
    assert len(records) == 7
    assert {r.cluster for r in records} == {"rate", "coach", "metro", "speech"}


@pytest.mark.parametrize("content", ["", "\n\n  \n"])
def test_empty_file(content: str) -> None:
    with pytest.raises(DatasetError, match="нет записей"):
        parse_records(content.splitlines())


def test_record_without_cluster_reports_line() -> None:
    lines = [
        '{"id": "1", "title": "a", "text": "b", "cluster": "x"}',
        "",
        '{"id": "2", "title": "a", "text": "b"}',
    ]
    with pytest.raises(DatasetError, match="строка 3: нет полей cluster"):
        parse_records(lines)


@pytest.mark.parametrize(
    ("line", "message"),
    [("{not json", "некорректный JSON"), ("[1, 2]", "ожидался JSON-объект")],
)
def test_broken_line(line: str, message: str) -> None:
    with pytest.raises(DatasetError, match=f"строка 1: {message}"):
        parse_records([line])


def test_main_prints_table_and_recommendation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(SAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "Записей: 7, кластеров: 4" in out
    assert "порог | пропущено дублей | ложные склейки" in out
    assert out.count("\n") >= 13
    assert "dedup.simhash_max_distance" in out or "Ни один порог" in out


def test_main_reports_dataset_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main([str(empty)]) == 1
    assert "нет записей" in capsys.readouterr().err
