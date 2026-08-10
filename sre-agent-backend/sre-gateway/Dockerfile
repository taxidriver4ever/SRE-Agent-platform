# 使用官方精简 Python 镜像，减少 Gateway 运行镜像体积。
FROM python:3.12-slim

# 禁止生成 .pyc，并让日志立即输出到 Docker 日志流。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先复制依赖清单，业务代码变化时仍可复用依赖安装层缓存。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Gateway 仅需要应用代码；SQLite 数据目录由 Compose volume 持久化。
COPY app ./app

EXPOSE 8000

# 生产容器关闭热重载，使用单进程 Uvicorn 提供 API。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
