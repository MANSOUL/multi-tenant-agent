FROM python:3.12-slim
WORKDIR /app
# agent-api 用 LangGraph 编排：先装依赖，再拷代码
COPY agent/requirements.txt ./agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt
COPY agent/ ./agent/
EXPOSE 8081
CMD ["python3", "agent/app.py"]
