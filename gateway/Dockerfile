FROM python:3.12-slim
WORKDIR /app
# 网关只用 Python 标准库，不需要 pip 装依赖
COPY gateway.py ./gateway.py
EXPOSE 8080
CMD ["python3", "gateway.py"]
