FROM python:3.12-slim
ARG VCS_REF=unknown
ARG VCS_SOURCE=local/user-service
LABEL org.opencontainers.image.revision=$VCS_REF org.opencontainers.image.source=$VCS_SOURCE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
ENV PYTHONPATH=/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
# 在镜像构建阶段运行不依赖外部数据库的领域测试，防止坏版本进入集群。
RUN pytest -q
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8082
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082", "--workers", "2"]
