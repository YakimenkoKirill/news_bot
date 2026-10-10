import pytest

from app.pipeline.ai.factcheck import normalize_number, unsupported_facts

SOURCE_RU = (
    "Министр финансов Антон Силуанов заявил, что бюджет России в 2026 году получит "
    "1 000 млрд рублей дополнительных доходов. Рост составит 1,5% к прошлому году. "
    "Об этом он рассказал журналистам в Москве после встречи с представителями ЕС."
)
SOURCE_EN = (
    "NASA said on Friday that the Artemis mission will launch 25 days later than planned. "
    "Administrator Bill Nelson told reporters in Washington that 3.5 billion dollars were spent."
)


def test_faithful_russian_summary() -> None:
    summary = (
        "Силуанов сообщил, что в 2026 году бюджет получит дополнительно 1000 млрд рублей. "
        "По словам министра, рост составит 1.5 % — об этом он сказал в Москве на встрече с ЕС."
    )
    assert unsupported_facts(SOURCE_RU, summary) == []


def test_name_cases_supported() -> None:
    summary = "Журналисты расспросили Силуанова о бюджете, который обсуждали в Москве с Россией."
    assert unsupported_facts(SOURCE_RU, summary) == []


def test_invented_numbers_and_names_listed() -> None:
    summary = (
        "Министр Силуанов и премьер Мишустин пообещали 2 000 млрд рублей и рост на 7% "
        "уже в 2027 году, сообщает Reuters."
    )
    assert unsupported_facts(SOURCE_RU, summary) == [
        "2 000",
        "7%",
        "2027",
        "Мишустин",
        "Reuters",
    ]


def test_english_text() -> None:
    faithful = "Agency official Nelson said in Washington the Artemis launch slips by 25 days."
    assert unsupported_facts(SOURCE_EN, faithful) == []
    invented = "Officials at NASA and ESA said the Artemis launch cost 4 billion dollars."
    assert unsupported_facts(SOURCE_EN, invented) == ["4", "ESA"]


def test_abbreviation_checked_whole() -> None:
    assert unsupported_facts("Решение принял ЕС.", "Об этом сообщил ЕС.") == []
    assert unsupported_facts("Решение принял ЕСПЧ.", "Об этом сообщил ЕС.") == ["ЕС"]
    assert unsupported_facts("Решение принял ЕС.", "Об этом сообщил ЕСПЧ.") == ["ЕСПЧ"]


def test_sentence_start_words_not_checked() -> None:
    summary = "Бюджет вырос. «Доходы» выросли! Расходы тоже?\nНалоги снизят."
    assert unsupported_facts("бюджет доходы расходы налоги", summary) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 000", "1000"),
        ("1 000 000", "1000000"),
        ("1,5", "1.5"),
        ("1.50", "1.5"),
        ("25%", "25"),
        ("25 %", "25"),
        ("2026", "2026"),
    ],
)
def test_number_forms(raw: str, expected: str) -> None:
    assert normalize_number(raw) == expected


def test_number_forms_match_between_texts() -> None:
    source = "Собрали 1000 подписей и 2,5 тонны."
    assert unsupported_facts(source, "Собрали 1 000 подписей и 2.5 тонны.") == []


@pytest.mark.parametrize("summary", ["", "   \n"])
def test_empty_summary(summary: str) -> None:
    assert unsupported_facts(SOURCE_RU, summary) == []


def test_duplicates_reported_once() -> None:
    assert unsupported_facts("Текст.", "Сумма 5 и снова 5, а также Иванов и Иванов.") == [
        "5",
        "Иванов",
    ]
