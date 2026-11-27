"""GET /files/ 文件访问（integration，真 HTTP + 假网关上游）。

用例来源：VERIFY.md 第一节逐条列出的 29 项。VERIFY.md 逐条列出 27 项，
另有 2 项（css 的 Content-Type、非白名单后缀 403）是按 _serve_file 的
白名单逻辑补的，见 tests/README.md。
agent server 本地起在 127.0.0.1 随机端口，TENANT_ID=demo，数据用临时
目录，每次运行先清空，不碰项目 data/；网关部分不用真 Docker，
用 fake urlopen + monkeypatch ensure_tenant_container。
"""
import http.client
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest import mock

from tests.helpers import TEST_SECRET, fresh_tenant_data, make_token
import agent.server as server_mod
import agent.tenant as tenant_mod
import gateway.gateway as gateway_mod

HTML = b"<!DOCTYPE html><html><body>calc</body></html>"
CSS = b"body { color: red; }"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class TestFilesEndpoint(unittest.TestCase):
    """agent /files/：真起一个 HTTP 服务测（VERIFY.md：agent 侧 17 项 + 补 2 项）。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = fresh_tenant_data()
        cls._ctx.__enter__()
        workspace, db_path, _ = tenant_mod.tenant_dirs("demo")
        (workspace / "calculator.html").write_bytes(HTML)
        (workspace / "sub").mkdir(exist_ok=True)
        (workspace / "sub" / "style.css").write_bytes(CSS)
        (workspace / "pic.png").write_bytes(PNG)
        (workspace / "data.db").write_bytes(b"fake-db-bytes")
        (workspace / ".env").write_bytes(b"SECRET=x")
        (workspace / "run.exe").write_bytes(b"MZ fake exe")
        os.symlink(db_path, workspace / "link.db")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.demo_token = make_token("demo")
        cls.other_token = make_token("tenant-a")
        cls.fake_token = make_token("demo", secret="wrong-secret")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._ctx.__exit__(None, None, None)

    def _get(self, path, token="USE_DEMO"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if token == "USE_DEMO":
            token = self.demo_token
        if token:
            headers["X-Tenant-Token"] = token
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        ctype = resp.getheader("Content-Type")
        conn.close()
        return resp.status, ctype, body

    def test_html_200_and_content(self):
        status, ctype, body = self._get("/files/calculator.html")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertEqual(body, HTML)

    def test_subdir_css_200(self):
        status, _, body = self._get("/files/sub/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(body, CSS)

    def test_css_content_type(self):
        # 补的 1 项：白名单映射里 css 是 text/css，不是 application/octet-stream。
        _, ctype, _ = self._get("/files/sub/style.css")
        self.assertEqual(ctype, "text/css; charset=utf-8")

    def test_png_200_bytes_identical(self):
        status, ctype, body = self._get("/files/pic.png")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, PNG)

    def test_query_string_still_200(self):
        status, _, body = self._get("/files/calculator.html?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, HTML)

    def test_parent_traversal_403(self):
        status, _, _ = self._get("/files/../memory.db")
        self.assertEqual(status, 403)

    def test_encoded_traversal_403(self):
        status, _, _ = self._get("/files/%2e%2e%2fmemory.db")
        self.assertEqual(status, 403)

    def test_subdir_traversal_403(self):
        status, _, _ = self._get("/files/sub/../../memory.db")
        self.assertEqual(status, 403)

    def test_absolute_path_403(self):
        status, _, _ = self._get("/files//etc/passwd")
        self.assertEqual(status, 403)

    def test_encoded_absolute_path_403(self):
        status, _, _ = self._get("/files/%2Fetc%2Fpasswd")
        self.assertEqual(status, 403)

    def test_dotenv_403(self):
        status, _, _ = self._get("/files/.env")
        self.assertEqual(status, 403)

    def test_symlink_escape_403(self):
        # workspace 里的符号链接指到 memory.db：解析后在 workspace 外，挡掉。
        status, _, _ = self._get("/files/link.db")
        self.assertEqual(status, 403)

    def test_missing_404(self):
        status, _, _ = self._get("/files/nope.html")
        self.assertEqual(status, 404)

    def test_directory_404(self):
        # 目录不列表，直接 404。
        status, _, _ = self._get("/files/sub/")
        self.assertEqual(status, 404)

    def test_no_token_401(self):
        status, _, _ = self._get("/files/calculator.html", token=None)
        self.assertEqual(status, 401)

    def test_other_tenant_token_401(self):
        # tenant-a 的 token：单租户容器只认 demo。
        status, _, _ = self._get("/files/calculator.html",
                                 token=self.other_token)
        self.assertEqual(status, 401)

    def test_fake_token_401(self):
        status, _, _ = self._get("/files/calculator.html",
                                 token=self.fake_token)
        self.assertEqual(status, 401)

    def test_workspace_db_403(self):
        status, _, _ = self._get("/files/data.db")
        self.assertEqual(status, 403)

    def test_non_whitelisted_suffix_403(self):
        # 补的 1 项：不在白名单的后缀（如 .exe）一律 403。
        status, _, _ = self._get("/files/run.exe")
        self.assertEqual(status, 403)


class _FakeResp:
    def __init__(self, status=200, body=b"", ctype="text/html; charset=utf-8"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_handler(path="/files/calculator.html", headers=None):
    h = object.__new__(gateway_mod.Handler)
    h.path = path
    h.headers = dict(headers or {})
    h.command = "GET"
    h.rfile = BytesIO(b"")
    h.sent = []
    h._send = lambda code, body, ctype="text/html; charset=utf-8", extra=None: \
        h.sent.append((code, body, ctype))
    return h


class TestGatewayFiles(unittest.TestCase):
    """网关 /files/：token_from、_proxy 透传、do_GET 路由、linkify（10 项）。"""

    def _run_proxy(self, path="/files/calculator.html", token=None,
                   fake_resp=None):
        token = make_token("demo") if token is None else token
        handler = _fake_handler(
            path, {"Authorization": f"Bearer {token}"})
        captured = {}

        def fake_urlopen(req, timeout=90):
            captured["url"] = req.full_url
            # 注意：urllib 把 header 名存成 capitalize 后的形式
            #（X-tenant-token），而 get_header 是按原样查键，
            # 所以这里直接读 header_items()，不用 get_header。
            sent_headers = dict(req.header_items())
            captured["tenant_token"] = sent_headers.get("X-tenant-token")
            captured["tenant_id"] = sent_headers.get("X-tenant-id")
            return fake_resp or _FakeResp(200, HTML)

        with (mock.patch.object(gateway_mod, "ensure_tenant_container",
                                lambda tenant: None),
              mock.patch("urllib.request.urlopen", fake_urlopen)):
            handler._proxy()
        return handler, captured

    def test_token_from_cookie(self):
        token = make_token("demo")
        h = _fake_handler(headers={"Cookie": f"mt_token={token}"})
        self.assertEqual(gateway_mod.token_from(h), token)

    def test_token_from_authorization(self):
        token = make_token("demo")
        h = _fake_handler(headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(gateway_mod.token_from(h), token)

    def test_proxy_forwards_to_tenant_container(self):
        _, captured = self._run_proxy()
        self.assertEqual(
            captured["url"],
            "http://mt-tenant-demo:8081/files/calculator.html")

    def test_proxy_forwards_tenant_token_header(self):
        token = make_token("demo")
        _, captured = self._run_proxy(token=token)
        self.assertEqual(captured["tenant_token"], token)
        self.assertEqual(captured["tenant_id"], "demo")

    def test_proxy_passthrough_html_content_type(self):
        handler, _ = self._run_proxy(
            fake_resp=_FakeResp(200, HTML, "text/html; charset=utf-8"))
        self.assertEqual(handler.sent[0][0], 200)
        self.assertEqual(handler.sent[0][1], HTML)
        self.assertEqual(handler.sent[0][2], "text/html; charset=utf-8")

    def test_proxy_passthrough_png_content_type(self):
        # 图片不许被套上 "; charset=utf-8"。
        handler, _ = self._run_proxy(
            fake_resp=_FakeResp(200, PNG, "image/png"))
        self.assertEqual(handler.sent[0][2], "image/png")
        self.assertEqual(handler.sent[0][1], PNG)

    def test_do_get_files_routes_to_proxy(self):
        handler = _fake_handler("/files/calculator.html")
        calls = []
        handler._proxy = lambda: calls.append("proxy")
        handler.do_GET()
        self.assertEqual(calls, ["proxy"])

    def test_do_get_api_routes_to_proxy(self):
        handler = _fake_handler("/api/chat")
        calls = []
        handler._proxy = lambda: calls.append("proxy")
        handler.do_GET()
        self.assertEqual(calls, ["proxy"])

    def test_agent_page_linkifies_files_links(self):
        self.assertIn("linkify", server_mod.PAGE)
        self.assertIn("[/]files[/]", server_mod.PAGE)

    def test_gateway_page_linkifies_files_links(self):
        self.assertIn("linkify", gateway_mod.LOGIN_PAGE)
        self.assertIn("[/]files[/]", gateway_mod.LOGIN_PAGE)
