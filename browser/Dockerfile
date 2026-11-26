FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt ./
# 同根 Dockerfile：阿里云服务器上用内网 PyPI 镜像，官方源太慢且不稳。
RUN pip install --no-cache-dir --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com -r requirements.txt
COPY browser.py ./browser.py
EXPOSE 8001
CMD ["python3", "browser.py"]
