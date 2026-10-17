import random

import pytest

from app.config import SIMHASH_BAND_BITS, SIMHASH_BANDS, SIMHASH_MAX_DISTANCE
from app.pipeline.dedup import (
    compute_simhash,
    from_db,
    hamming,
    shingles,
    simhash_bands,
    to_db,
)
from tests.config_helpers import base_section

CHARS = base_section("dedup")["first_paragraph_chars"]

TITLE = "Центробанк сохранил ключевую ставку на уровне шестнадцати процентов"
TEXT = (
    "Совет директоров Банка России на заседании в пятницу принял решение сохранить "
    "ключевую ставку. Регулятор отметил, что инфляция замедляется, однако остаётся выше "
    "целевого уровня, и не исключил дальнейших изменений в следующем году."
)
PARAPHRASE_TITLE = "ЦБ сохранил ключевую ставку на уровне шестнадцати процентов"
PARAPHRASE_TEXT = TEXT.replace("в пятницу", "в эту пятницу")
OTHER_TITLE = "Футбольный клуб подписал контракт с новым главным тренером"
OTHER_TEXT = (
    "Руководство клуба объявило о назначении нового наставника команды. Контракт рассчитан "
    "на три сезона, а первая тренировка под его руководством пройдёт уже на следующей неделе."
)


def _sim(title: str, text: str) -> int:
    return compute_simhash(title, text, CHARS)


def test_identical_texts_distance_zero() -> None:
    assert hamming(_sim(TITLE, TEXT), _sim(TITLE, TEXT)) == 0


def test_normalization_ignores_case_and_punctuation() -> None:
    variant = TEXT.upper().replace(",", "").replace(".", "!")
    assert hamming(_sim(TITLE, TEXT), _sim(TITLE.lower(), variant)) == 0


def test_paraphrase_is_close_and_different_news_far() -> None:
    base = _sim(TITLE, TEXT)
    close = hamming(base, _sim(PARAPHRASE_TITLE, PARAPHRASE_TEXT))
    far = hamming(base, _sim(OTHER_TITLE, OTHER_TEXT))
    assert 0 < close < far
    assert close <= 12
    assert far >= 20


def test_only_first_chars_of_text_used() -> None:
    long_text = f"{TEXT} {TEXT}"
    assert len(long_text) > CHARS
    tail_changed = long_text[:CHARS] + " совершенно другой хвост статьи про погоду и спорт"
    assert hamming(_sim(TITLE, long_text), _sim(TITLE, tail_changed)) == 0


def test_short_text_uses_single_words() -> None:
    assert shingles("Два слова") == ["два", "слова"]
    assert shingles("один два три четыре") == ["один два три", "два три четыре"]
    assert shingles("") == []
    value = compute_simhash("Пожар", "", CHARS)
    assert 0 <= value < 2**64
    assert value != compute_simhash("Наводнение", "", CHARS)


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1, 2**63, 2**64 - 1])
def test_to_db_from_db_roundtrip(value: int) -> None:
    stored = to_db(value)
    assert -(2**63) <= stored <= 2**63 - 1
    assert from_db(stored) == value


def test_db_boundaries() -> None:
    assert to_db(0) == 0
    assert to_db(2**64 - 1) == -1
    assert to_db(2**63) == -(2**63)
    for bad in (-1, 2**64):
        with pytest.raises(ValueError, match="64"):
            to_db(bad)


def test_hamming_works_with_signed_values() -> None:
    a, b = 2**64 - 1, 0
    assert hamming(a, b) == 64
    assert hamming(to_db(a), to_db(b)) == 64
    assert hamming(to_db(a), a) == 0


def test_simhash_bands_splits_into_four_parts() -> None:
    value = 0xAAAA_BBBB_CCCC_DDDD
    assert simhash_bands(value) == (0xAAAA, 0xBBBB, 0xCCCC, 0xDDDD)
    assert simhash_bands(0) == (0, 0, 0, 0)
    assert simhash_bands(2**64 - 1) == (0xFFFF,) * SIMHASH_BANDS


def test_simhash_bands_same_for_signed_and_unsigned() -> None:
    """В БД simhash лежит знаковым BIGINT — части должны совпадать."""
    for value in (2**64 - 1, 2**63, 0xFFFF_0000_1234_5678, 0xABCD_1234_5678_9ABC):
        assert simhash_bands(value) == simhash_bands(to_db(value))


def test_simhash_bands_are_16_bit() -> None:
    bands = simhash_bands(0xABCD_1234_5678_9ABC)
    assert len(bands) == SIMHASH_BANDS
    assert all(0 <= band < 2**SIMHASH_BAND_BITS for band in bands)


def test_close_values_share_a_band() -> None:
    """Расстояние <= 3 — хотя бы одна часть совпадает (на этом держится поиск)."""
    rng = random.Random(20261017)
    for _ in range(2000):
        value = rng.getrandbits(64)
        distance = rng.randint(0, SIMHASH_MAX_DISTANCE)
        bits = rng.sample(range(64), distance)
        other = value
        for bit in bits:
            other ^= 1 << bit
        assert hamming(value, other) == distance
        shared = [
            mine == theirs
            for mine, theirs in zip(simhash_bands(value), simhash_bands(other), strict=True)
        ]
        assert any(shared), f"нет общей части при расстоянии {distance}"


def test_distant_values_may_share_no_band() -> None:
    """Расстояние 4 по одному биту в каждой части — общих частей нет."""
    value = 0xABCD_1234_5678_9ABC
    other = value ^ (1 << 1) ^ (1 << 20) ^ (1 << 40) ^ (1 << 60)
    assert hamming(value, other) == SIMHASH_MAX_DISTANCE + 1
    assert all(
        mine != theirs
        for mine, theirs in zip(simhash_bands(value), simhash_bands(other), strict=True)
    )
