FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml .python-version ./
RUN pip install --no-cache-dir uv \
    && uv sync --no-dev --no-install-project

COPY app ./app
COPY contracts ./contracts
RUN uv sync --no-dev

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
