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
- 演进规则（只加不改）：以后只加新字段、新事件类型，不改现有字段的含义、
  不删字段、不把可选字段改成必填。`chat` 事件类型已占位，流式回复时再启用。
  破坏性改动必须升大版本并先改本文档。
