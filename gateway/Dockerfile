FROM python:3.12-slim
WORKDIR /app
# 网关只用 Python 标准库，但要调 docker CLI 起/停租户容器，
# 所以要装 docker 客户端（只要 CLI，不在网关里跑 daemon）。
# 注意：网关容器还要挂载 /var/run/docker.sock 才能真正连上宿主机 Docker。
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*
COPY gateway.py ./gateway.py
EXPOSE 8080
CMD ["python3", "gateway.py"]
