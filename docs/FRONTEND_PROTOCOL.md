# 前端事件协议（v1.0，2026-11-27，P1）

聊天前端（聊天页、登录页里的聊天框）和后端之间稳定的 JSON 协议。
以后换前端不用改后端；后端改实现时，这个文档里的字段和事件类型是底线。

## 1. 聊天响应

`POST /api/chat`（需登录，见网关 token）成功时：

```json
{
  "reply": "已设好提醒：喝水，时间 2026-11-27 19:20（今天，一次）",
  "tenant": "demo",
  "events": [
    {"type": "tool_call", "name": "add_reminder",
     "args": {"text": "喝水", "time_text": "晚上7点20分"}},
    {"type": "reminder_delivered", "notification_id": 3,
     "text": "喝水", "due_at": "2026-11-27T19:20:00+08:00"}
  ]
}
```

- `reply`（string）：给用户看的回复文本。和原来一样。
- `tenant`（string）：租户 id。和原来一样。
- `events`（array）：本轮产生的事件，**始终出现**；没有事件时是空数组 `[]`，
  不会省略这个字段。前端可以无条件 `d.events || []`。
- `usage`（object，v1.2 起）：本轮真实用量（`model` / `input_tokens` /
  `cached_tokens` / `output_tokens` / `reasoning_tokens` / `total_tokens` /
  `cost_usd`），没有真实用量时省略；`tenant_usage`（object，v1.2 起）：
  该租户的累计用量（`model_calls` / `tokens_total` / `cost_estimate`），
  和 `usage` 一起出现。

## 2. 事件类型

| type | 什么时候出现 | 字段 |
|---|---|---|
| `tool_call` | 本轮模型调了一次工具就记一条（多轮多工具就多条，按调用顺序） | `name`：工具名；`args`：调用参数对象（和工具定义的参数一致） |
| `reminder_delivered` | 本轮把一条到点提醒当面告诉了用户就记一条（可能多条） | `notification_id`：pending_notifications 行 id；`text`：提醒内容；`due_at`：应提醒时间（ISO 串） |
| `chat` | **占位，本版本不发**。以后流式/分段回复时用它推回复片段，字段到时再定 | — |

事件顺序：先是本轮送达的提醒（`reminder_delivered`，提醒在系统提示最前面、
模型先看到先说），再是本轮的工具调用（`tool_call`，按调用顺序）。
前端收到事件后只做展示（比如在提醒旁打个“已送达”标记、把工具调用折叠显示），
不要用事件内容反推业务状态——事实来源永远是 `reply` 文本和后端的库。

## 2.5 流式聊天（v1.1，2026-11-27，P5）

`POST /api/chat/stream`（需登录，和 /api/chat 同样的入参
`{"message": ..., "session_id": ...}`）：响应是 `text/event-stream`，
每行一个事件，格式 `data: {...}\\n\\n`。鉴权/入参校验失败时
**不开流**，直接按第 3 节的 JSON 错误返回；校验通过后立刻发第
一个事件，用户不用干等。

| type | 什么时候出现 | 字段 |
|---|---|---|
| `status` | 阶段变化：刚收到、记忆读完、开始调工具、工具跑完、整理回答 | `stage`：`received` / `thinking` / `tools` / `final`；`text`：给人看的中文 |
| `reasoning` | 模型在流式返回里带了思考内容时（推理模型才有；deepseek-chat 没有，不发） | `text`：思考片段 |
| `token` | 回答正文逐 token 增量 | `text`：增量文本，拼起来就是 `done.reply` |
| `tool_call` | 本轮模型调了一次工具（字段和 v1.0 的同名事件一致） | `name`；`args` |
| `tool_result` | 工具跑完（结果截到 200 字，不把大输出塞进流里） | `name`；`result` |
| `reminder_delivered` | 本轮把一条到点提醒当面告诉了用户（字段和 v1.0 一致） | `notification_id`；`text`；`due_at` |
| `done` | 整轮结束，流的最后一个事件 | `reply`：完整回复文本；`events`：本轮的 `tool_call` / `tool_result` / `reminder_delivered` 汇总（`status` / `token` / `reasoning` 不进汇总）；`session_id`；`usage`：有真实用量时带本轮用量（字段见下），没有时省略；`tenant_usage`：有 `usage` 时带租户累计 |
| `usage` | 整轮用量，`done` 之前发（每轮一次） | `model`：模型名；`input_tokens` / `cached_tokens`（输入里命中缓存的部分） / `output_tokens` / `reasoning_tokens`（输出里思考的部分，已含在 `output_tokens` 里） / `total_tokens`；`cost_usd`：按 DeepSeek 价目表算的本轮成本估算（美元，见 model/pricing.py）；`tenant_usage`：租户累计（`model_calls` / `tokens_total` / `cost_estimate`） |
| `error` | 出错，流的最后一个事件（再无 `done`） | `code`：第 3 节的错误码；`message`：中文说明 |

注意：`token` 增量是模型原样吐出来的——工具轮的简短铺垫
（比如“好的，我来记一下”）也会出现在 token 流里，`done.reply`
就是全部 token 拼起来的样子；非流式的 `/api/chat` 仍按老规则
只取最后一条有文字的 AI 回复，两者极少数情况下措辞会有出入。
前端收到 `done` / `error` 就收尾；中途断连时按 `done` 没到
处理（可提示用户重试，或回退调一次 `/api/chat`）。

## 3. 错误响应（和聊天响应同一套结构）

所有 HTTP 错误响应统一是：

```json
{"error": {"code": "unauthenticated", "message": "未登录：请通过网关登录后再访问"}}
```

- `code`（string）：机器可读的错误码，见下表；`message`（string）：给人看的中文说明。
- HTTP 状态码语义不变：401 未登录、403 禁止/租户隔离违规、404 不存在、
  400 请求不合法、500 模型失败/内部错误、502 上游（租户容器）不可用。
- 前端读错误时取 `d.error.message`，不要直接拿 `d.error` 当字符串拼进页面
  （旧版 `d.error` 是字符串，新版是对象；聊天页/登录页的 JS 已按新结构改）。

| code | 状态码 | 含义 | 可重试 |
|---|---|---|---|
| `unauthenticated` | 401 | 没登录 / token 无效或过期 | 否 |
| `forbidden` | 403 | 路径越界、后缀不在白名单等禁止访问 | 否 |
| `tenant_isolation` | 403 | 跨租户访问（容器只服务自己的租户） | 否 |
| `not_found` | 404 | 路由 / 文件不存在 | 否 |
| `bad_request` | 400 | 消息为空、JSON 不合法等 | 否 |
| `model_error` | 500 | 模型调用失败 | 是（瞬时失败可重试；key 没配则重试也没用） |
| `upstream_unavailable` | 502 | 租户容器起不来 / 连不上 | 是 |
| `internal_error` | 500 | 其它服务器内部错误 | 否 |

错误分类的完整定义见 `errors/codes.py`（可重试 / 不可重试 / 租户隔离违规 /
模型失败四类）；线上只传 `code` + `message`，分类和 `retryable` 只在服务端用。

## 4. 其它端点（本协议不改它们，只列出来对齐）

- `GET /`、`/index.html`：聊天页静态 HTML（`agent/web/chat.html`）。
- `GET /api/health`：`{"ok": true, "model": ..., "engine": "langgraph",
  "tenant": ..., "single_tenant": ..., "sandbox_mode": ...}`（内容和原来一致）。
- `GET /api/metrics`：只读，Prometheus 文本格式，三个计数
  （`mt_requests_total` / `mt_request_failures_total` / `mt_model_calls_total`）。
- `GET /api/me`：`{"tenant": ...}`。
- `GET /files/<路径>`：本租户 workspace 里的静态文件；不存在/越界时按第 3 节
  的统一错误结构返回（以前是 plain text 的 `not found` / `forbidden`）。

## 5. 版本说明

- v1.0（2026-11-27，P1）：定下 `reply` / `tenant` / `events` 和两种事件
  （`tool_call`、`reminder_delivered`），`events` 始终出现、可为空数组；
  错误响应统一为 `{"error": {"code", "message"}}`。
- v1.1（2026-11-27，P5）：加 `POST /api/chat/stream`（SSE）和第 2.5 节的
  8 种流事件（`status` / `reasoning` / `token` / `tool_call` /
  `tool_result` / `reminder_delivered` / `done` / `error`）；v1.0 的
  `/api/chat` 和事件含义一字不改。`chat` 占位类型被 `token` 替代，
  不再启用。
- v1.2（2026-11-27，P6）：加 `usage` 流事件和 `done` / `/api/chat`
  响应里的 `usage` + `tenant_usage`（真实用量，见第 2.5 节）；模型
  换成 `deepseek-reasoner`（ChatDeepSeek，推理文本进 `reasoning`
  事件）。用量没有时（假模型、老客户端）字段省略，前端按无用量处理。
- 演进规则（只加不改）：以后只加新字段、新事件类型，不改现有字段的含义、
  不删字段、不把可选字段改成必填。`chat` 事件类型已占位，流式回复时再启用。
  破坏性改动必须升大版本并先改本文档。
