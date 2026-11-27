"""数据层：持久化，实现 domain/ports.py 定义的仓储接口。

- sqlite_store.py：SQLite 实现（建表 + 全部 SQL）
- event_log.py：事件日志——chat_log 只追加，归档只标记，不删除

注意：包名是 store/ 而不是 ARCHITECTURE.md 写的 data/，原因见
store/sqlite_store.py 顶部注释（项目根目录的 data/ 是 gitignored 的
运行时目录，且容器里 /app/data 是租户数据卷的挂载点，代码放那里会被
卷遮住、也进不了 git）。
"""
