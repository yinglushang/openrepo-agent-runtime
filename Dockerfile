FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts
COPY config ./config
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /app/data /app/workspace \
    && chown -R agent:agent /app

USER agent
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["python", "-m", "app.main", "--host", "0.0.0.0", "--port", "8000"]
