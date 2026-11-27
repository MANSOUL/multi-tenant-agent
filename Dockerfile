FROM python:3.12-slim
WORKDIR /app
# agent-api 用 LangGraph 编排：先装依赖，再拷代码
COPY agent/requirements.txt ./agent/requirements.txt
# 服务器在阿里云上，PyPI 官方源又慢又经常返回空索引，
# 改用阿里云内网镜像（2026-11-26 部署时官方源两次找不到 pydantic-core==2.50.5）。
RUN pip install --no-cache-dir --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com -r agent/requirements.txt
COPY agent/ ./agent/
# P0 分层的新顶层包：必须一起拷进镜像，否则 agent/ 里的
# `from domain/... / store/... / model/... / context/...` 在容器里找不到。
# 注意不要把运行时目录 data/ 当代码拷——容器里 /app/data 是租户数据卷的挂载点。
COPY domain/ ./domain/
COPY store/ ./store/
COPY model/ ./model/
COPY context/ ./context/
# 让 `python3 agent/app.py` 脚本模式也能 import 顶层包（否则 sys.path
# 只有 agent/ 目录，`import domain` 会失败）。
ENV PYTHONPATH=/app
EXPOSE 8081
CMD ["python3", "agent/app.py"]
