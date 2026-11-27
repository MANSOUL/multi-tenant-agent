"""HTTP 服务（传输层）：只剩路由分发 + 请求校验入口。

路由：/（聊天页）、/api/health、/api/chat、/api/chat/stream（SSE，见
_handle_chat_stream）、/api/me、/api/metrics、
/files/<路径>。聊天页是 agent/web/chat.html 静态文件，后端只读文件
返回，不再内嵌 HTML 字符串；/files/ 的实现在 agent/file_server.py；
错误响应统一走 errors/（{"error": {"code": ..., "message": ...}}）；
健康检查的实现在 observability/health.py，指标在
observability/metrics.py（/api/metrics 只读 Prometheus 文本）。

只负责 HTTP 层：鉴权（调 tenant.get_tenant）、把消息交给
app/chat_service.chat_with_events()、返回 JSON。业务逻辑（图、工具、
记忆、沙箱）都在别的模块。会话：请求里可带 session_id（不带时
用每租户默认会话 "default"），返回里带 session_id；归档会话
只读，再发消息时按模型失败同款 500 结构返回（ChatService 抛
RuntimeError，见 do_POST 的异常分支）。
提醒调度器（2026-11-26）：启动时起一个守护线程，每 30 秒扫一次本租户
（单租户容器）或 DATA_DIR 下所有租户（老的多租户进程模式）的 reminders，
到点的写进 pending_notifications 表；下次用户来聊天时，recall_memory
会把未送达的提醒拼在系统提示最前面当面告诉用户。注意：这个 agent 只有
网页聊天框，没有向用户手机推送的通道，所以“到点提醒”实际是“到点后下次
聊天时提醒”，不要在任何地方假装能推送（见 README 能力边界）。
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from .config import DATA_DIR, MODEL, PORT, SINGLE_TENANT_MODE, TENANT_ID
    from .file_server import serve_file
    from .memory import check_due_reminders
    from .tenant import get_tenant
    from app.chat_service import chat_stream_with_events, chat_with_events
    from errors.codes import (
        ApiError, bad_request, model_failure, not_found,
        tenant_isolation_violation, unauthenticated,
    )
    from observability import health as health_mod
    from observability import logging as obs_logging
    from observability import metrics as obs_metrics
except ImportError:  # 以脚本方式运行 python3 agent/app.py 时
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from config import DATA_DIR, MODEL, PORT, SINGLE_TENANT_MODE, TENANT_ID
    from file_server import serve_file
    from memory import check_due_reminders
    from tenant import get_tenant
    from app.chat_service import chat_stream_with_events, chat_with_events
    from errors.codes import (
        ApiError, bad_request, model_failure, not_found,
        tenant_isolation_violation, unauthenticated,
    )
    from observability import health as health_mod
    from observability import logging as obs_logging
    from observability import metrics as obs_metrics


def format_sse_event(event: dict) -> bytes:
    """一个 SSE 事件 dict → wire 格式（data: {...}\\n\\n）。

    前端按空行切事件、按 "data: " 取 JSON；事件里不出现裸换行
    （json.dumps 会转义），所以一行一个事件足够。
    """
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            ).encode("utf-8")


def _load_chat_page() -> str:
    """读聊天页静态文件（agent/web/chat.html）；后端只读文件返回。"""
    return (Path(__file__).resolve().parent / "web" / "chat.html").read_text(
        encoding="utf-8")


CHAT_HTML = _load_chat_page()


def _reminder_tenants():
    """调度器要扫的租户：单租户容器只扫自己；否则扫 DATA_DIR 下所有租户目录。"""
    if TENANT_ID:
        return [TENANT_ID]
    try:
        return [p.name for p in DATA_DIR.iterdir()
                if p.is_dir() and (p / "memory.db").exists()]
    except OSError:
        return []


def _reminder_loop(interval_seconds: int = 30):
    """调度器后台线程：每 interval 秒扫一次，异常自己吞掉，绝不把服务拖垮。"""
    while True:
        try:
            for tenant in _reminder_tenants():
                check_due_reminders(tenant)
        except Exception:
            pass
        time.sleep(interval_seconds)


def start_reminder_scheduler(interval_seconds: int = 30) -> threading.Thread:
    """启动提醒调度器守护线程，返回线程对象（测试可调小 interval）。"""
    t = threading.Thread(target=_reminder_loop, args=(interval_seconds,),
                         daemon=True, name="reminder-scheduler")
    t.start()
    return t


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静一点，别刷屏（日志走 observability）
        pass

    # -- 请求上下文：每请求一个 id，每条日志带租户 id -------------------
    def _begin_request(self):
        self._request_id = (self.headers.get("X-Request-Id", "") or
                            obs_logging.new_request_id())
        self._request_started = time.monotonic()
        self._tenant = ""
        obs_metrics.inc(obs_metrics.REQUESTS)

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        if code >= 400:
            obs_metrics.inc(obs_metrics.FAILURES)
        obs_logging.emit(
            "http_response", level="error" if code >= 500 else "info",
            request_id=getattr(self, "_request_id", ""),
            tenant=getattr(self, "_tenant", ""),
            method=self.command, path=urlparse(self.path).path,
            status=code,
            duration_ms=round(
                (time.monotonic() - getattr(
                    self, "_request_started", time.monotonic())) * 1000, 1))

    def _send_sse_start(self):
        """发 SSE 响应头（不带 Content-Length，靠关连接收尾）。

        头一发出去网关/浏览器就能读到第一个事件，所以这里立刻
        flush；之后每个事件也都 flush（见 _send_sse_event）。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()

    def _send_sse_event(self, event: dict):
        """写一个 SSE 事件并立刻 flush（不等攒满缓冲）。"""
        self.wfile.write(format_sse_event(event))
        self.wfile.flush()

    def _send_error(self, err: ApiError):
        """按统一错误结构发 JSON 错误响应（状态码由 ApiError 自带）。"""
        self._send(err.status, json.dumps(err.body(), ensure_ascii=False),
                   "application/json")

    def _need_tenant(self):
        tenant = get_tenant(self)
        if not tenant:
            self._send_error(unauthenticated(
                "未登录：请通过网关登录后再访问"))
            return None
        self._tenant = tenant
        return tenant

    def do_GET(self):
        self._begin_request()
        # 查询串（?token= 之类）只用来透传，路由只看路径部分。
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, CHAT_HTML)
        elif path == "/api/health":
            self._send(200, json.dumps(health_mod.health_payload(
                model=MODEL, tenant=TENANT_ID or None,
                single_tenant=SINGLE_TENANT_MODE,
                sandbox_mode=os.environ.get("SANDBOX_MODE", "docker"))),
                "application/json")
        elif path == "/api/metrics":
            self._send(200, obs_metrics.render_prometheus(),
                       "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/api/me":
            tenant = self._need_tenant()
            if tenant:
                self._send(200, json.dumps({"tenant": tenant},
                                           ensure_ascii=False),
                           "application/json")
        elif path.startswith("/files/"):
            tenant = self._need_tenant()
            if tenant:
                serve_file(self, tenant, path[len("/files/"):])
        else:
            self._send_error(not_found())

    def do_POST(self):
        self._begin_request()
        if self.path not in ("/api/chat", "/api/chat/stream"):
            return self._send_error(not_found())
        tenant = self._need_tenant()
        if not tenant:
            return
        try:
            payload = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0))
        except ValueError:
            return self._send_error(bad_request("消息不是合法的 JSON"))
        if not isinstance(payload, dict):
            return self._send_error(bad_request("消息不是合法的 JSON"))
        text = (payload.get("message") or "").strip()
        if not text:
            return self._send_error(bad_request("消息是空的"))
        session_id = payload.get("session_id") or None
        if self.path == "/api/chat/stream":
            return self._handle_chat_stream(tenant, text, session_id)
        try:
            result = chat_with_events(tenant, text, session_id)
            payload = {"reply": result["reply"], "tenant": tenant,
                       "events": result.get("events", []),
                       "session_id": result.get("session_id", "default")}
            if result.get("usage") is not None:
                payload["usage"] = result["usage"]
                payload["tenant_usage"] = result.get("tenant_usage")
            self._send(200, json.dumps(payload, ensure_ascii=False),
                       "application/json")
        except Exception as e:  # noqa: BLE001 - 模型失败按 500 统一错误结构返回
            if "只服务租户" in str(e):
                return self._send_error(
                    tenant_isolation_violation(str(e)))
            return self._send_error(model_failure(f"模型调用失败：{e}"))

    def _handle_chat_stream(self, tenant, text, session_id):
        """POST /api/chat/stream：SSE 流式聊天（协议 v1.1）。

        鉴权和入参校验和 /api/chat 一模一样（失败时先按普通 JSON
        错误返回，不开流）；校验通过后立刻发 SSE 头，之后每个事件
        现产现发。生成器本身不抛异常，但迭代时万一冒出异常也补一个
        error 事件再收尾，不让连接半路断在没交代的状态。
        """
        self._send_sse_start()
        try:
            for event in chat_stream_with_events(tenant, text, session_id):
                self._send_sse_event(event)
        except Exception as e:  # noqa: BLE001 - 流中断也要给前端一个交代
            self._send_sse_event({
                "type": "error", "code": "model_error",
                "message": f"模型调用失败：{e}"})
        # 请求计数在 _begin_request 里已经记过一次，这里只补一条
        # 完成日志（_send() 的日志在流式路径里不会走）。
        obs_logging.emit(
            "http_response", level="info",
            request_id=getattr(self, "_request_id", ""),
            tenant=getattr(self, "_tenant", ""),
            method=self.command, path="/api/chat/stream",
            status=200,
            duration_ms=round(
                (time.monotonic() - getattr(
                    self, "_request_started", time.monotonic())) * 1000, 1))


def main():
    obs_logging.setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start_reminder_scheduler()
    print(f"多租户 Agent API 跑起来了：http://localhost:{PORT}  模型={MODEL} 引擎=langgraph")
    print("提醒调度器已启动（每 30 秒扫一次，到点提醒下次聊天时当面送达）")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
