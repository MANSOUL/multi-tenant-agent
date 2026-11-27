"""安全横切入口：密钥管理（security/secrets.py）+ 输入防线
（security/guardrails.py）。租户隔离、路径安全仍在 agent/tenant.py。
"""
try:
    from .guardrails import check_injection, guardrail_note
    from .secrets import assert_key_isolation
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from security.guardrails import check_injection, guardrail_note
    from security.secrets import assert_key_isolation

__all__ = ["assert_key_isolation", "check_injection", "guardrail_note"]
