"""权限管线回归测试（P3 修正，2026-11-27）。

P3 初版把 write_file / run_command 一律划为 ask（无审批通道 = 拒绝），
结果做网页（/files/）功能整个不可用。修正：工作区内的写文件放行
（执行时 safe_path 再拦一次），沙箱内执行命令放行（破坏半径与写
文件相同），越出工作区的写操作仍走 ask。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.permissions import check_permission  # noqa: E402


class TestWorkspaceWritePolicy(unittest.TestCase):
    def test_write_inside_workspace_allowed(self):
        for path in ("page.html", "sub/page.html", "a/b/c.txt"):
            with self.subTest(path=path):
                decision, _ = check_permission(
                    "write_file", "demo", {"path": path, "content": "x"})
                self.assertEqual(decision, "allow")

    def test_write_dotdot_asks(self):
        for path in ("../memory.db", "a/../../etc/passwd", "..\\memory.db"):
            with self.subTest(path=path):
                decision, reason = check_permission(
                    "write_file", "demo", {"path": path, "content": "x"})
                self.assertEqual(decision, "ask")
                self.assertIn("工作区之外", reason)

    def test_write_absolute_path_asks(self):
        decision, _ = check_permission(
            "write_file", "demo", {"path": "/etc/passwd", "content": "x"})
        self.assertEqual(decision, "ask")

    def test_write_missing_path_asks(self):
        decision, _ = check_permission(
            "write_file", "demo", {"content": "x"})
        self.assertEqual(decision, "ask")


class TestSandboxCommandPolicy(unittest.TestCase):
    def test_run_command_allowed_in_sandbox(self):
        decision, _ = check_permission(
            "run_command", "demo", {"command": "ls"})
        self.assertEqual(decision, "allow")


class TestDefaultDeny(unittest.TestCase):
    def test_unknown_tool_denied(self):
        decision, reason = check_permission("delete_everything", "demo", {})
        self.assertEqual(decision, "deny")
        self.assertIn("未知工具", reason)

    def test_read_tools_still_allowed(self):
        for tool in ("read_file", "list_workspace", "list_todos",
                     "list_reminders", "browse_page"):
            with self.subTest(tool=tool):
                decision, _ = check_permission(tool, "demo", {})
                self.assertEqual(decision, "allow")


if __name__ == "__main__":
    unittest.main()
