"""Показывает следующий шаг разработки и отмечает выполненные.

Использование:
    python prompts/next_step.py           # показать следующий шаг
    python prompts/next_step.py --done    # отметить текущий шаг выполненным
    python prompts/next_step.py --status  # прогресс
    python prompts/next_step.py --list    # все шаги с отметками
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
STEP_RE = re.compile(r"^(\d{3})_P\d{3}_.+\.md$")
COMMIT_RE = re.compile(r"^\*\*Коммит:\*\* `(.+)`$", re.MULTILINE)


def list_steps() -> list[Path]:
    return sorted(p for p in PROMPTS_DIR.iterdir() if STEP_RE.match(p.name))


def progress_file() -> Path:
    """Файл прогресса лежит в .git, чтобы не пачкать рабочее дерево."""
    git_dir = Path(git("rev-parse", "--absolute-git-dir"))
    return git_dir / "next_step_progress.json"


def load_done() -> set[str]:
    path = progress_file()
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("done", []))


def save_done(done: set[str]) -> None:
    progress_file().write_text(
        json.dumps({"done": sorted(done)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def step_number(path: Path) -> str:
    return path.name[:3]


def expected_commit(path: Path) -> str:
    match = COMMIT_RE.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"В файле {path.name} не найдена строка «Коммит».")
    return match.group(1)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} завершился с ошибкой: {result.stderr.strip()}")
    return result.stdout.strip()


def current_step(steps: list[Path], done: set[str]) -> Path | None:
    return next((p for p in steps if step_number(p) not in done), None)


def show(step: Path | None, total: int, done_count: int) -> None:
    if step is None:
        print(f"Все шаги выполнены ({done_count} из {total}).")
        return
    header = step.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
    print(header)
    print(f"Файл: prompts/{step.name}")
    print(f"В Claude Code напишите: «Выполни @prompts/{step.name}»")
    print(f"Выполнено: {done_count} из {total}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--done", action="store_true", help="отметить текущий шаг выполненным")
    group.add_argument("--status", action="store_true", help="показать прогресс")
    group.add_argument("--list", action="store_true", help="показать все шаги")
    parser.add_argument("--force", action="store_true", help="с --done: не сверять коммит")
    args = parser.parse_args()

    steps = list_steps()
    if not steps:
        print("Файлы шагов не найдены.", file=sys.stderr)
        return 1
    done = load_done()

    if args.list:
        for p in steps:
            mark = "x" if step_number(p) in done else " "
            print(f"[{mark}] {p.name}")
        return 0

    if args.status:
        print(f"Выполнено: {len(done)} из {len(steps)}")
        return 0

    step = current_step(steps, done)
    if args.done:
        if step is None:
            print("Все шаги уже выполнены.")
            return 0
        if not args.force:
            if git("status", "--porcelain"):
                print("Есть незакоммиченные изменения — сначала закоммитьте шаг.", file=sys.stderr)
                return 1
            want = expected_commit(step)
            got = git("log", "-1", "--pretty=%s")
            if got != want:
                print(
                    f"Последний коммит «{got}» не совпадает с коммитом шага «{want}».\n"
                    "Если всё верно, повторите с --force.",
                    file=sys.stderr,
                )
                return 1
        done.add(step_number(step))
        save_done(done)
        print(f"Шаг {step_number(step)} отмечен выполненным.\n")
        step = current_step(steps, done)

    show(step, len(steps), len(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
