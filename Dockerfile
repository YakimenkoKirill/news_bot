# syntax=docker/dockerfile:1

# --- builder: wheel-пакеты зависимостей (слой кэшируется, пока не меняется pyproject.toml)
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; open('requirements.txt', 'w').write('\n'.join(deps))" \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# --- runtime: только зависимости и код приложения, запуск не от root
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENV=prod

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

WORKDIR /app
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN chmod 0755 ./docker/entrypoint.sh \
    && mkdir -p /app/data /app/backups \
    && chown -R 10001:10001 /app/data /app/backups

USER 10001

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["python", "-m", "app"]
