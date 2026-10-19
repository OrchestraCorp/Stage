FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.1 \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin stage

COPY pyproject.toml .python-version uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=stage:stage app ./app
COPY --chown=stage:stage contracts ./contracts
COPY --chown=stage:stage migrations ./migrations
COPY --chown=stage:stage alembic.ini ./alembic.ini
RUN uv sync --locked --no-dev \
    && chown -R stage:stage /app

EXPOSE 8080
USER stage
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
