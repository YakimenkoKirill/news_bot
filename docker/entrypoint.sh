#!/bin/bash
# Точка входа контейнера: миграции БД (идемпотентны), затем приложение или переданная команда.
# Ошибка миграции завершает контейнер с ненулевым кодом (set -e).
set -euo pipefail

alembic upgrade head

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec python -m app
