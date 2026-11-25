FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Create non-root runtime user
RUN groupadd --system appuser && useradd --system --gid appuser appuser

COPY pyproject.toml requirements.txt config.py main.py ./
COPY src ./src
COPY sample_data ./sample_data

RUN pip install --no-cache-dir . && \
    mkdir -p /app/output && \
    chown -R appuser:appuser /app

USER appuser

ENTRYPOINT ["client-onboarding-bot"]
CMD ["--help"]
