"""数据层：持久化，实现 domain/ports.py 定义的仓储接口。

- sqlite_store.py：SQLite 实现（建表 + 全部 SQL）
- postgres_store.py：Postgres 实现（P4，同一接口，%s 方言）
- event_log.py：事件日志——chat_log 只追加，归档只标记，不删除

STORE_BACKEND 切换（P4）：create_store(conn) 按环境变量选实现，
sqlite（默认）→ SQLiteStore，postgres → PostgresStore；连接和
后端必须匹配（postgres 后端配了个 SQLite 连接直接 ValueError，
不静默跑错方言）。tenant_db() 目前还是只开 SQLite（见
tenant_db() 已按 STORE_BACKEND 切后端（见 agent/tenant.py），
所以生产默认行为不变；服务器上验证 Postgres 时再跑真库。

注意：包名是 store/ 而不是 ARCHITECTURE.md 写的 data/，原因见
store/sqlite_store.py 顶部注释（项目根目录的 data/ 是 gitignored 的
运行时目录，且容器里 /app/data 是租户数据卷的挂载点，代码放那里会被
卷遮住、也进不了 git）。
"""
import os


def backend_name() -> str:
    """当前存储后端：STORE_BACKEND 环境变量，默认 sqlite。"""
    return os.environ.get("STORE_BACKEND", "sqlite").strip().lower()


def create_store(conn):
    """按 STORE_BACKEND 给这个连接建仓储（实现 domain/ports.py 的
    ReminderStore / MemoryStore 接口）。

    sqlite → SQLiteStore；postgres → PostgresStore；其它值直接
    ValueError。postgres 后端拿到 SQLite 连接也直接 ValueError——
    占位符方言不同（? 对 %s），跑起来只会错得更难查，不如在入口
    就拦掉。
    """
    backend = backend_name()
    if backend == "postgres":
        import sqlite3

        if isinstance(conn, sqlite3.Connection):
            raise ValueError(
                "STORE_BACKEND=postgres，但拿到的是 SQLite 连接："
                "先把 tenant_db() 切到 store/postgres_store.connect()，"
                "再开 postgres 后端")
        from .postgres_store import PostgresStore

        return PostgresStore(conn)
    if backend == "sqlite":
        from .sqlite_store import SQLiteStore

        return SQLiteStore(conn)
    raise ValueError(
        f"STORE_BACKEND 不支持：{backend!r}（只支持 sqlite / postgres）")
