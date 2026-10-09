FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip && pip install .
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8001
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=8).status == 200 else 1)" || exit 1
CMD ["lp-indexer"]
