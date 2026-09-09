FROM python:3.12-slim-bookworm

ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.source="https://github.com/taxidriver4ever/SRE-Agent-platform" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.title="sre-gateway"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements.txt

COPY app ./app
RUN groupadd --system --gid 10001 sre \
    && useradd --system --uid 10001 --gid sre --home-dir /home/sre --create-home sre \
    && chown -R sre:sre /app /home/sre

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
