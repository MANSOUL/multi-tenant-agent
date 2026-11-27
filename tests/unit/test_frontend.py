"""前端改版（2026-11-27）：新聊天界面 + Markdown + 刷新免登录。

验收（只读页面源码和服务端源码，不启动浏览器）：
- 两页都有无依赖的 renderMarkdown：标题/粗体/代码块/列表/表格/
  引用/链接/裸链接，且先转义 HTML（XSS 防护在代码块正则之前）。
- 两页都是新界面：气泡（msg me / msg ai）、深色模式、流式聊天、
  思考过程、用量行。
- 登录页：刷新后靠 cookie + GET /api/me 恢复（不再读/存 JS token，
  不用 Bearer 头）；退出清 cookie；登录表单还在。
- agent 页：没有登录表单，靠 cookie 认身份，401 提示走网关登录。
- 服务端：网关 Set-Cookie 带 Max-Age=43200（12 小时）；
  agent 的 get_tenant 也认 mt_token cookie。
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAT = (ROOT / "agent" / "web" / "chat.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "gateway" / "web" / "login.html").read_text(encoding="utf-8")


class TestMarkdownRenderer(unittest.TestCase):
    def test_both_pages_have_renderer(self):
        for page in (CHAT, LOGIN):
            self.assertIn("function renderMarkdown", page)
            self.assertIn("function linkify", page)
            self.assertIn("[/]files[/]", page)
            # 先转义再渲染：esc 定义在 renderMarkdown 之前使用
            self.assertLess(page.index("function esc"),
                            page.index("function renderMarkdown"))
            self.assertIn("```", page)
            self.assertIn("<table>", page)
            self.assertIn("<blockquote>", page)
            self.assertIn("<strong>", page)

    def test_no_external_markdown_dependency(self):
        for page in (CHAT, LOGIN):
            self.assertNotIn("marked", page)
            self.assertNotIn("cdn.jsdelivr", page)
            self.assertNotIn("unpkg.com", page)


class TestNewChatUi(unittest.TestCase):
    def test_bubbles_and_dark_mode(self):
        for page in (CHAT, LOGIN):
            self.assertIn("msg me", page)
            self.assertIn("msg ai", page)
            self.assertIn("prefers-color-scheme: dark", page)
            self.assertIn("思考过程", page)
            self.assertIn("/api/chat/stream", page)
            self.assertIn("function usageText", page)
            self.assertIn("function showUsage", page)
            self.assertIn("Token：输入", page)
            self.assertIn('id="q"', page)


class TestSessionRestore(unittest.TestCase):
    def test_login_page_restores_from_cookie(self):
        # 刷新后：问一句 /api/me，认得出就进聊天；JS 不读不存 token。
        self.assertIn("fetch('/api/me')", LOGIN)
        self.assertIn("function restore", LOGIN)
        self.assertIn("DOMContentLoaded", LOGIN)
        self.assertIn("function logout", LOGIN)
        self.assertIn("max-age=0", LOGIN)
        self.assertNotIn("Bearer", LOGIN)
        self.assertNotIn("localStorage", LOGIN)
        # 登录表单还在
        self.assertIn('id="u"', LOGIN)
        self.assertIn('id="p"', LOGIN)

    def test_agent_page_has_no_login_form(self):
        self.assertNotIn('id="u"', CHAT)
        self.assertNotIn("/api/login", CHAT)
        self.assertNotIn("Bearer", CHAT)
        self.assertIn("401", CHAT)


class TestServerSideCookie(unittest.TestCase):
    def test_gateway_cookie_has_max_age(self):
        main = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
        self.assertIn("Max-Age=43200", main)

    def test_agent_accepts_cookie(self):
        tenant = (ROOT / "agent" / "tenant.py").read_text(encoding="utf-8")
        self.assertIn('part.startswith("mt_token=")', tenant)
        self.assertIn("verify_token", tenant)


if __name__ == "__main__":
    unittest.main()
