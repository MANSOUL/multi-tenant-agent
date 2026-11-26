## 2026-11-26 项目结构重构之后（新）

- `agent/app.py` 从约 700 行拆成 7 个模块 + 1 个薄入口（见 README“项目结构”）：
  `config.py`（配置）/`tenant.py`（租户隔离）/`memory.py`（记忆与上下文）/
  `tools.py`（10 个工具）/`graph.py`（LangGraph 图）/`server.py`（HTTP 服务）/
  `sandbox.py`（未动）/`app.py`（20 行薄入口）。行为未改：同一份逻辑原样搬移，
  `tenant.py` 是隔离逻辑的唯一来源，`tools`/`memory` 不再重复实现。
- `python3 -m py_compile agent/*.py gateway/gateway.py browser/browser.py`：全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过，5 个服务都在；根 `Dockerfile` 的
  `CMD ["python3", "agent/app.py"]` 不用改。
- Stub 模型端到端复测（`/tmp/test_refactor.py`，用假模型替掉 `ChatOpenAI`，
  不调真实接口；数据在 `/tmp/mt-refactor-data`，没碰项目 `data/`），**全部通过**：
  - (a) 多轮上下文：第 1 轮“记住，我在上海”→ 返回“记好了，你在上海。”；
    第 2 轮“我上次说我在哪个城市？”→ 系统提示里含第 1 轮原文，返回“你在上海。”；
    `facts` 表有（城市，上海），`chat_log` ≥ 4 行；
  - (c) 租户隔离：tenant-b 的 facts/chat_log 均为空，`recall_fact` 返回“没记过”，
    `search_facts("上海")` 返回“没找到”；
  - (d) 新工具：tenant-a 的 `list_workspace` 能看到 `notes/todo.md`，
    `search_file("牛奶")` 能找到、`search_file("不存在的词xyz")` 返回没找到；
    路径穿越 `../tenant-b/memory.db` 被挡；`rm` 被白名单拒绝；
    `df` 本地回退能跑；工具共 10 个。
- HTTP 入口复测（venv 启动，无真实 key）：`python3 agent/app.py` 和
  `python -m agent.app` 两种方式都能起；`/api/health` → `{"ok": true, ...}`；
  无身份 `/api/me` → 401；带 `X-Tenant-Id: demo`（`TRUST_TENANT_HEADER=1`）→
  `{"tenant": "demo"}`；`/api/chat` 无 key → “还没填 LLM_API_KEY”报错透传、服务不崩。
  测试后两个服务都已停掉，无残留进程。
- 无硬编码密钥：grep 检查过 py 文件。

## 未验证（重构后仍需在用户机器上跑）

- `docker compose up --build`、沙箱 docker 模式、browser 服务运行时、
  postgres / redis 启动：本机无 Docker / 没装 Playwright，**都没跑过**。
  重构只改了 agent 包内部分工，`Dockerfile`/`docker-compose.yml` 没变，
  但首次在有 Docker 的机器上跑时仍要按老步骤逐项验。
- 真实 LLM 端到端对话：stub 只验证了图流转和隔离，真实模型行为同前，仍未验证。

# 验证记录 · 2026-11-26

验证环境：Muse 工作区 VM（Ubuntu 24.04.5，Python 3.12.3）。**这台机器上没有 Docker**，
所以下面凡是标“未验证”的，都是因为缺 Docker / 缺 Playwright，不是代码写错。

## 2026-11-26 上下文/记忆/工具改进之后（新）

- `python3 -m py_compile agent/app.py agent/sandbox.py gateway/gateway.py browser/browser.py`：全部通过。
- 依赖不变（`agent/requirements.txt` 没改），全新 venv（`/tmp/mt-venv`）里
  `pip install -r agent/requirements.txt` 一次成功，`langgraph` 等导入通过。
- Stub 模型端到端测试（`/tmp/test_context_v2.py`，用假模型替掉 `ChatOpenAI`，
  不调真实接口；测试时 `CONTEXT_MAX_TURNS=2`、`SUMMARY_THRESHOLD_TURNS=3`，
  数据在 `/tmp/mt-test2-data`，没碰项目 `data/`），**全部通过**：
  - (a) 多轮上下文：第 2 轮的系统提示里包含第 1 轮原文（“我在上海”），
    `chat_log` 里有 2 轮问答；
  - (b) 旧对话变摘要：6 轮时 `load_context` 把最旧 4 轮用模型总结进
    `summaries` 表（摘要含“在上海/喜欢喝美式”），`chat_log` 只剩最近 2 轮
    （4 行），历史文本里不再有第 1 轮原文；
  - (b2) 无模型回退：`llm=None` 时不写摘要、直接截断最旧轮，只剩最近 2 轮；
  - (b3) 字符预算：`max_chars=40` 时从最旧一轮开始截，只保留最后一轮，
    `estimate_tokens("abcd")==1`、`("abcdefgh")==2`（约 4 字符 ≈ 1 token）；
  - (c) 租户隔离：tenant-b 的 `search_facts("上海")` 返回“没找到”，
    `load_context("tenant-b")` 的 facts/history/summary 全为空；
  - (d) 新工具：tenant-a 的 `list_workspace` 能看到 `notes/todo.md`，
    `search_file("牛奶")` 能找到、`search_file("不存在的词xyz")` 返回没找到，
    tenant-b 的工作区是空的、搜不到 tenant-a 的文件；`read_file("../tenant-b/…")`
    仍被路径前缀检查挡掉。
  - 测试中还发现并修了一个真问题：`load_context` 原先把 `max_chars` 下限
    钳在 500，导致小预算测不出来，现改成下限 10（只防 0/负数）。
- 无硬编码密钥：grep 检查过 py 文件（`.env.example` 里只有占位符和演示账号密码）。

## 未验证（改进后仍需在用户机器上跑）

- `docker compose up --build`、沙箱 docker 模式、browser 服务运行时、
  postgres / redis 启动：本机无 Docker / 没装 Playwright，**都没跑过**。
- **真实 LLM 的摘要质量没跑过**：stub 只验证了“会调总结、会存摘要、会截断”，
  真实模型总结的摘要是否准确、是否超过 200 字、非 OpenAI 兼容接口的兼容性，
  填真实 key 后要先聊 20 轮以上（默认 `SUMMARY_THRESHOLD_TURNS=20`）再看
  `summaries` 表。
- `CONTEXT_MAX_CHARS` 的默认值 12000（约 3000 token）是按字符粗估的，
  不是精确 token 计费；模型上下文上限不同时要在 `.env` 里调这个值。

## 2026-11-26 改成 LangGraph 之后（上一版记录，保留）

- 依赖可装：`pip install langgraph langchain-core langchain-openai` 在全新 venv 里一次成功，
  解析出的版本是 **langgraph 1.2.24 / langchain-core 1.8.5 / langchain-openai 1.8.5 /
  openai 3.59.0 / pydantic 2.14.4**，已按此 pin 在 `agent/requirements.txt`；
  `from langgraph.graph import StateGraph`、`ToolNode`、`langchain_openai.ChatOpenAI` 导入全部通过。
- `python3 -m py_compile agent/app.py agent/sandbox.py gateway/gateway.py browser/browser.py`：全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过，5 个服务都在（compose 文件本身没改，
  只改了根 `Dockerfile` 加装 requirements）。
- 图结构实测：`build_graph("tenant-a")` 编译通过，节点齐全
  `recall_memory / llm / tools / final_llm / save_memory`，7 个工具齐全。
- 租户隔离复测（和上一版同一标准）：tenant-a 用 `remember_fact` 记住“喜好=喜欢喝美式”后，
  tenant-a 能查到，tenant-b 查同一 key 返回“没记过这件事”，`list_facts` 也看不到；
  路径穿越 `../tenant-b/memory.db` 仍被前缀检查挡掉；`rm` 仍不在白名单。
- stub 模型端到端（用假模型替掉 `ChatOpenAI`，不调真实接口）：
  `chat("tenant-a", "记住，我在上海")` 完整走了
  recall→llm→tools（remember_fact）→llm→save_memory，返回“记好了，你在上海。”；
  `facts` 表里有（城市，上海），`chat_log` 表里有一问一答。说明图循环、工具执行、
  存记忆三个节点都通了。
- agent-api + gateway 联调（本地 venv 的 `python` 启动两个进程，无 LLM key）：
  - `GET /api/health` → `{"ok": true, "model": "gpt-4o-mini", "engine": "langgraph", "sandbox_mode": "local"}`。
  - `GET /api/me` 无身份 → 401；经网关不带 token → 401；错误密码登录 → 401；正确登录 → 发 token，带 token 经网关 → `{"tenant": "demo"}`。
  - 经网关调 `/api/chat` → 缺 key 的报错正确透传（“还没填 LLM_API_KEY”）。
  - **只认 HMAC token 的路径复测过**：agent 不开 `TRUST_TENANT_HEADER` 时，
    直接拿 `X-Tenant-Id: demo` 调 `/api/me` 和 `/api/chat`（无 token）→ 都是 401，
    伪造租户头进不去，和上一版行为一致。
- 沙箱本地回退复测：`df` 能跑且带“本地回退：未隔离，仅开发用”标注。
- 无硬编码密钥：grep 检查过 py 文件（`.env.example` 里只有占位符和演示账号密码）。

## 未验证（改成 LangGraph 后仍需在用户机器上跑）

- `docker compose config` / `docker compose up --build`：本机无 Docker，**没跑过**。
  注意根 `Dockerfile` 现在多了 `pip install -r agent/requirements.txt` 一步，
  首次跑时确认这步能在镜像里成功（需要联网拉 PyPI）。
- 沙箱 docker 模式、browser 服务运行时、postgres / redis 启动：同上一版，均未验证。
- **LangGraph + 真实 LLM 的端到端对话：没跑过。** stub 模型只验证了图的流转，
  真实模型调工具时可能出现：模型不按工具格式返回、连续调工具超 3 轮（会走 final_llm
  强制回答）、`ChatOpenAI` 对非 OpenAI 兼容接口的兼容性问题。填真实 key 后先试一句
  “记住，我在上海”再试一句“帮我看看 df”，确认工具调用和记忆都通。
- LangGraph 的 `recursion_limit` 设的是 30，3 轮工具循环（每轮 llm+tools 约 2 步）
  加 recall/save 节点远到不了上限，只是防死循环的安全网。

## 以下是上一版（手写循环）的验证记录，保留作历史


## 已验证（本地实测通过）

- `python3 -m py_compile agent/app.py agent/sandbox.py gateway/gateway.py browser/browser.py`：全部通过。
- `docker-compose.yml` 用 PyYAML 解析通过，5 个服务都在：`gateway / agent-api / browser / postgres / redis`。
- agent-api 单独启动（`SANDBOX_MODE=local`，无 LLM key）：
  - `GET /api/health` → `{"ok": true, "model": "gpt-4o-mini", "sandbox_mode": "local"}`。
  - `GET /api/me` 无身份 → 401；带 `X-Tenant-Id: demo`（`TRUST_TENANT_HEADER=1` 时）→ `{"tenant": "demo"}`。
  - `POST /api/chat` 无 key → 返回“还没填 LLM_API_KEY”，服务不崩。
- gateway + agent-api 联调（本地 `python3` 启动两个进程）：
  - 登录页 `GET /` → 200；错误密码登录 → 401“用户名或密码不对”；正确登录 → 发 token。
  - 不带 token 访问 `/api/me` → 401；带 token → `{"tenant": "demo"}`。
  - 经网关调 `/api/chat` → 缺 key 的报错正确透传。
  - **只认 HMAC token 的路径也测过**：agent 不开 `TRUST_TENANT_HEADER` 时，
    经网关带 token 能进；直接拿 `X-Tenant-Id` 调 agent-api（无 token）→ 401。
    也就是说 compose 里（gateway 和 agent 共用 `GATEWAY_SECRET`）的鉴权链是通的。
- 沙箱（本地回退模式）：`df` 能跑，返回 `[本地回退：未隔离，仅开发用]` 标注；
  `rm` 不在白名单 → 被拒，返回可用命令列表。
- 路径穿越检查：`workspace/../tenant-b/secret.txt` 解析后不在本租户工作区内，被前缀检查挡掉。
- 无硬编码密钥：grep 检查过，`.env.example` 里只有占位符和演示账号密码（首次使用务必改掉）。

## 未验证（需要用户的机器 / Docker）

- `docker compose config` / `docker compose up --build`：本机无 Docker，**没跑过**。
  首次在有 Docker 的机器上跑时，先跑 `docker compose config` 检查，再 `up`。
- 沙箱 docker 模式（`docker run --rm --read-only --network none …`）：本机无 Docker，
  只验证了本地回退模式。docker 模式的代码路径在 `agent/sandbox.py`，等有 Docker 后再实测。
- browser 服务运行时：本机没装 Playwright，只做了 `py_compile`。
  首次跑时要在容器里确认 `mcr.microsoft.com/playwright/python:v1.49.0-jammy`
  镜像能拉到、`pip install -r requirements.txt` 能装上，再调一次 `/browse`。
- LLM 真实对话：没填真实 key，没跑过端到端聊天。填 key 后按 README 的步骤试一句即可。
- postgres / redis：compose 里有这两个服务，但当前版本 agent 默认用每租户 SQLite，
  不依赖它俩；它俩的启动本身也没在这台机器上验证过。

## pi-agent-starter 是否动过

没动。`~/workspace/pi-agent-starter/` 只有原有文件（多了一个 2026-11-26 上午的
`MULTI_TENANT_VM_PLAN.md`，那是之前那轮对话里生成的计划文件，本次任务没有改它）。
