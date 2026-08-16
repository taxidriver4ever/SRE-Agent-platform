# Install dependencies and run pure ranking tests before producing the runtime image.
FROM python:3.12-slim
ARG VCS_REF=unknown
ARG VCS_SOURCE=local/recommendation-service
LABEL org.opencontainers.image.revision=$VCS_REF org.opencontainers.image.source=$VCS_SOURCE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
ENV PYTHONPATH=/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
RUN pytest -q
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8085
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8085","--workers","2"]
