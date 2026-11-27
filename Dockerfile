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
# P1 新增的横切包：errors/（统一错误码）+ observability/（日志/健康/指标），
# agent/server.py、agent/graph.py、agent/file_server.py 都 import 它们。
# agent/web/chat.html 随 COPY agent/ 一起进镜像，不用单独拷。
COPY errors/ ./errors/
COPY observability/ ./observability/
# P3/P4 新增的顶层包：app/（编排）tools/（工具层）loop/（循环控制）
# session/（会话）security/（安全横切）infra/（图接线 + checkpointer
# + 浏览器路由）。P3 时漏拷了它们——agent/graph.py、agent/server.py、
# agent/memory.py 都 import 这些包，不拷进镜像容器里会 ImportError；
# P4 的 infra/checkpoints.py、infra/browser_service.py 也在这里，
# 不拷的话 checkpointer 和租户浏览器路由在容器里不可用。
COPY app/ ./app/
COPY tools/ ./tools/
COPY loop/ ./loop/
COPY session/ ./session/
COPY security/ ./security/
COPY infra/ ./infra/
# 让 `python3 agent/app.py` 脚本模式也能 import 顶层包（否则 sys.path
# 只有 agent/ 目录，`import domain` 会失败）。
ENV PYTHONPATH=/app
EXPOSE 8081
CMD ["python3", "agent/app.py"]
