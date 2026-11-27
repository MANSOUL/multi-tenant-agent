"""会话（会话层）：创建/恢复/fork/归档 + 按会话复用图。

行为兼容：/api/chat 不带 session id 时自动用每租户默认会话
（"default"），返回里带 session_id；带 session_id 时恢复该会话；
fork 复制历史摘要 + 事实引用（不复制全文——事实是租户级的 facts
表，新会话天然引用同一份；全文在租户级 chat_log 里，不按会话切，
见模块说明）；归档后该会话只读，再发消息直接拒绝。

图复用：同一会话的连续请求复用同一张编译好的图实例（P3 之前
chat() 每次现建图）；一把锁保护缓存，线程安全。缓存 key 里带
模型客户端类的 id——测试里假模型换类时旧图不会被误复用。
"""
import secrets
import threading

try:
    from agent.tenant import tenant_db
    from store import create_store
    from . import store as session_store
except ImportError:  # 以脚本方式跑时项目根目录在 path 上
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.tenant import tenant_db
    from store import create_store
    from session import store as session_store

DEFAULT_SESSION_ID = "default"


def new_session_id() -> str:
    """生成会话 id：sess- + 8 位随机十六进制。"""
    return "sess-" + secrets.token_hex(4)


class SessionManager:
    """会话管理器：元数据走 session/store.py（转调 store/ 仓储，
    后端按 STORE_BACKEND 切），
    图实例缓存在内存里（进程内，丢了就重建——图本身无状态，记忆
    在库里，重建不丢东西）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._graphs: dict = {}

    # -- 会话元数据 ------------------------------------------------------
    def ensure_session(self, tenant: str, session_id: str | None = None
                       ) -> dict:
        """恢复会话：id 为空时用默认会话；没有就新建。返回会话 dict。"""
        sid = session_id or DEFAULT_SESSION_ID
        with tenant_db(tenant) as conn:
            store = create_store(conn)
            session = session_store.get_session(store, sid)
            if session is None:
                session = session_store.create_session(store, sid, tenant)
        return session

    def get_session(self, tenant: str, session_id: str) -> dict | None:
        """只读会话，没有返回 None（不新建）。"""
        with tenant_db(tenant) as conn:
            return session_store.get_session(
                create_store(conn), session_id)

    def create_session(self, tenant: str) -> dict:
        """新建一个会话（随机 id），返回会话 dict。"""
        with tenant_db(tenant) as conn:
            return session_store.create_session(
                create_store(conn), new_session_id(), tenant)

    def fork_session(self, tenant: str, session_id: str) -> dict:
        """fork 会话：新 id，复制源会话的历史摘要；事实是租户级的，
        新会话引用同一份 facts 表，不复制全文。源会话不存在抛 KeyError。"""
        with tenant_db(tenant) as conn:
            store = create_store(conn)
            src = session_store.get_session(store, session_id)
            if src is None:
                raise KeyError(session_id)
            if src["tenant"] != tenant:
                raise KeyError(session_id)
            return session_store.create_session(
                store, new_session_id(), tenant, summary=src["summary"] or "")

    def archive_session(self, tenant: str, session_id: str) -> bool:
        """归档会话：状态翻成 archived，之后只读。返回是否真的改了一条。
        归档不删图缓存里的图实例（图无状态，重建也一样），只挡新请求。"""
        with tenant_db(tenant) as conn:
            return session_store.archive_session(
                create_store(conn), session_id)

    def is_archived(self, tenant: str, session_id: str) -> bool:
        """该会话是不是已归档（只读）。"""
        session = self.get_session(tenant, session_id)
        return bool(session and session["status"]
                    == session_store.SESSION_ARCHIVED)

    # -- 图复用 ----------------------------------------------------------
    def get_graph(self, tenant: str, session_id: str, builder,
                  salt=None):
        """取（或建）该会话的图：同一 (租户, 会话, 模型类) 复用同一张
        图实例。builder 是 build_graph(tenant) 这样的可调用对象，
        salt 区分不同的模型客户端类（测试换假模型时旧图不复用）。"""
        key = (tenant, session_id or DEFAULT_SESSION_ID, salt)
        with self._lock:
            graph = self._graphs.get(key)
            if graph is None:
                graph = builder(tenant)
                self._graphs[key] = graph
            return graph

    def drop_graph(self, tenant: str, session_id: str | None = None) -> None:
        """丢掉图缓存（测试、或会话归档后想强制重建时调）。"""
        with self._lock:
            for key in [k for k in self._graphs
                        if k[0] == tenant
                        and (session_id is None
                             or k[1] == session_id)]:
                del self._graphs[key]
