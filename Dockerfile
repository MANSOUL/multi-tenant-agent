FROM python:3.12-slim
WORKDIR /app
# agent-api 用 LangGraph 编排：先装依赖，再拷代码
COPY agent/requirements.txt ./agent/requirements.txt
# 服务器在阿里云上，PyPI 官方源又慢又经常返回空索引，
# 改用阿里云内网镜像（2026-11-26 部署时官方源两次找不到 pydantic-core==2.50.5）。
RUN pip install --no-cache-dir --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com -r agent/requirements.txt
COPY agent/ ./agent/
EXPOSE 8081
CMD ["python3", "agent/app.py"]
