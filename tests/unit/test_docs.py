import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD_CANDIDATES = [ROOT / "CLAUDE.md", ROOT / ".claude" / "CLAUDE.md"]
MIN_INVARIANTS = 11


def _claude_md() -> str:
    for path in CLAUDE_MD_CANDIDATES:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise AssertionError("CLAUDE.md не найден")


def _section(text: str, title: str) -> str:
    match = re.search(rf"^## {title}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"нет раздела «{title}»"
    return match.group(1)


def _makefile_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z0-9_.-]+):", text, re.MULTILINE))


def test_readme_exists() -> None:
    assert (ROOT / "README.md").is_file()


def test_invariants_section_has_enough_items() -> None:
    section = _section(_claude_md(), "Инварианты")
    items = re.findall(r"^\d+\. ", section, re.MULTILINE)
    assert len(items) >= MIN_INVARIANTS


def test_autopilot_section_present() -> None:
    _section(_claude_md(), "Автопилот: выполнение шагов из prompts/ по очереди")


def test_make_commands_declared_in_makefile() -> None:
    commands = set(re.findall(r"`make ([A-Za-z0-9_.-]+)", _claude_md()))
    assert commands
    assert commands <= _makefile_targets()
