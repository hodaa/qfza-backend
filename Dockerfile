FROM python:3.12-slim AS builder

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Copy application and migration sources
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Run as an unprivileged user; /app only needs read access.
RUN groupadd --system --gid 10001 jumpto \
    && useradd --system --create-home --shell /usr/sbin/nologin --uid 10001 --gid 10001 jumpto

USER jumpto

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
