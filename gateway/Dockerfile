FROM python:3.12-slim
WORKDIR /app
# 网关只用 Python 标准库，但要调 docker CLI 起/停租户容器，
# 所以要装 docker 客户端（只要 CLI，不在网关里跑 daemon）。
# 注意：Debian trixie 的 docker.io 包里没有 docker 客户端二进制，
# 客户端在单独的 docker-cli 包里（2026-11-26 部署时因此报 502）。
# 注意：网关容器还要挂载 /var/run/docker.sock 才能真正连上宿主机 Docker。
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

# P2：网关已拆成包 gateway/{main,edge,infra,control}（见 docs/ARCHITECTURE.md
# 第四节 P2），构建上下文是仓库根目录（见 docker-compose.yml gateway.build），
# 因为网关除了自己的代码，还要顶层 errors/（统一错误码）和
# gateway/web/login.html（登录页静态文件，后端只读文件返回）。
# 只拷单个 gateway.py 的旧写法会让新版网关在容器里 import 失败、
# 读不到登录页——不要改回单文件 COPY。
# 管理接口（127.0.0.1:8082，只限回环）不 EXPOSE、不发布端口，
# 只能 docker exec 进容器调。
COPY gateway/ ./gateway/
COPY errors/ ./errors/
EXPOSE 8080
CMD ["python3", "-m", "gateway.main"]
