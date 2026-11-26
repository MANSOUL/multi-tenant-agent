"""配置：所有环境变量和常量（只读，不含任何逻辑）。

启动：python3 agent/app.py ，网关在前面，见 docker-compose.yml
环境变量：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / MAX_TOOL_ROUNDS /
  CONTEXT_MAX_TURNS / CONTEXT_MAX_CHARS / SUMMARY_THRESHOLD_TURNS /
  PORT / DATA_DIR / TOKEN_SECRET（与网关的 GATEWAY_SECRET 相同）/
  BROWSER_URL / SANDBOX_MODE / SANDBOX_IMAGE / TRUST_TENANT_HEADER
依赖：见 agent/requirements.txt（langgraph / langchain-core / langchain-openai）
"""
import os
from pathlib import Path

PORT = int(os.environ.get("PORT", "8081"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "3"))
# 上下文管理默认值（每次调用 load_context 时重新读环境变量，
# 这样测试可以用小阈值验证，不用重启进程；生产默认值见 .env.example）
DEFAULT_CONTEXT_MAX_TURNS = 10
DEFAULT_CONTEXT_MAX_CHARS = 12000
DEFAULT_SUMMARY_THRESHOLD_TURNS = 20
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "") or os.environ.get(
    "GATEWAY_SECRET", ""
)
BROWSER_URL = os.environ.get("BROWSER_URL", "http://browser:8001").rstrip("/")
TRUST_TENANT_HEADER = os.environ.get("TRUST_TENANT_HEADER", "") == "1"

SYSTEM_PROMPT = """你是多租户 Agent 里的个人助手。
你可以直接调用工具，不需要调用工具时，直接用自然语言回答。
每次最多调用 3 轮工具，拿到工具结果后再回答用户。
回答用中文，简短直接。
如果用户说“记住……”“别忘了……”“我喜欢/我在……”这类话，调用 remember_fact
把要点记下来（key 用简短中文，如“城市”“喜好”）；如果用户问“你记得我……吗”，
先用 recall_fact 或 search_facts 查，查不到就老实说没记过，不要编造。
"""

TOOLS_DOC = ("可用工具：run_command(command: ls/pwd/date/df/free/uptime/whoami/git)、"
             "read_file(path)、write_file(path, content)、list_workspace()、"
             "search_file(keyword)、remember_fact(key, value)、recall_fact(key)、"
             "search_facts(keyword)、list_facts()、browse_page(url, screenshot)。"
             "文件只能读写本租户 workspace 目录内，记忆只属于本租户。")
