FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip && pip install .
RUN useradd --create-home --uid 10001 app
USER app
# healthy while the loop writes its heartbeat at least every 3 minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime('/tmp/lp_bot_heartbeat') < 180 else 1)" || exit 1
CMD ["lp-bot"]
