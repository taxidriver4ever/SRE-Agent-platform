FROM python:3.12-slim

# 固定测试运行时版本；Validation 请求不能在运行时安装依赖或传入任意命令。
RUN pip install --no-cache-dir pytest==8.3.5

USER root
WORKDIR /workspace
