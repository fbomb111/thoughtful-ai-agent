FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Copy dependency files first (for layer caching)
COPY pyproject.toml poetry.lock ./

# Install dependencies (no virtualenv in container)
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --only main --no-root

# Copy application code (only what's needed at runtime)
COPY app.py chainlit.md ./
COPY agent/ ./agent/
COPY agent_config/ ./agent_config/
COPY .chainlit/config.toml ./.chainlit/config.toml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]
