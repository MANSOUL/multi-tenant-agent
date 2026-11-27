"""浏览器服务：Playwright + Chromium 无头浏览。

POST /browse  {url, screenshot, tenant}
  → 打开页面 → 返回 {title, text, screenshot_path}
  → 截图（如果要求）存到共享卷的该租户工作区：
     /data/<tenant>/workspace/browser-<时间戳>.png，
     返回给 agent 的是相对路径 browser-<时间戳>.png。

每租户的浏览器 profile 在 /data/<tenant>/browser-profile，
本版本为简单起见每次用全新的上下文（不持久化登录态）；
要持久化登录态时，把 launch_persistent_context 的 user_data_dir
指到该租户的 profile 目录即可（见 README 待讨论问题）。

P4 浏览器隔离：同一个服务、同一份代码，两种挂法——
- 共享容器（compose 的 browser 服务）：DATA_DIR=/data 挂宿主机
  ./data，截图落在宿主机 ./data/<租户>/workspace，不在租户卷里
  （P4 之前唯一的挂法，也是现在的 fallback）；
- 租户容器（mt-browser-<租户>，网关按需 docker run 起）：
  DATA_DIR=/data 挂租户卷 mt-data-<租户>，截图写
  /data/<租户>/workspace 时正好落在租户卷的 workspace 里，
  /files/ 能直接 serve。代码不用改，挂载点决定了截图落在哪。

启动：python3 browser.py ，端口 8001（仅容器内网）
环境变量：PORT / DATA_DIR
依赖：playwright==1.49.0（见 requirements.txt，镜像里已装好浏览器）
"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8001"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def browse(url: str, screenshot: bool, tenant: str) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            title = page.title()
            text = page.inner_text("body")[:4000]
            result = {"title": title, "text": text, "screenshot_path": ""}
            if screenshot:
                workspace = DATA_DIR / tenant / "workspace"
                workspace.mkdir(parents=True, exist_ok=True)
                name = f"browser-{int(time.time())}.png"
                page.screenshot(path=str(workspace / name), full_page=False)
                result["screenshot_path"] = name
            return result
        finally:
            browser.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/browse":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            payload = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0)) or 0))
            url = (payload.get("url") or "").strip()
            tenant = (payload.get("tenant") or "").strip()
            if not url.startswith(("http://", "https://")):
                return self._send(400, json.dumps(
                    {"error": "只支持 http:// 或 https:// 开头的地址"},
                    ensure_ascii=False))
            if not TENANT_RE.match(tenant):
                return self._send(400, json.dumps(
                    {"error": "tenant 不合法"}, ensure_ascii=False))
            result = browse(url, bool(payload.get("screenshot", False)), tenant)
            return self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 - 把失败原因返回给 agent
            return self._send(200, json.dumps(
                {"error": str(e)[:500]}, ensure_ascii=False))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"浏览器服务跑起来了：http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
