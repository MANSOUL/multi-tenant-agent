"""会话层入口：SessionManager / DEFAULT_SESSION_ID。

会话创建/恢复/fork/归档 + 按会话复用图；元数据在
session/store.py（SQLite sessions 表）。
"""
try:
    from .session import DEFAULT_SESSION_ID, SessionManager
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from session.session import DEFAULT_SESSION_ID, SessionManager

__all__ = ["DEFAULT_SESSION_ID", "SessionManager"]
