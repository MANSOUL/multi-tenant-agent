"""领域层：纯规则，不依赖任何外部东西。

这里只放不碰数据库、不碰网络、不碰模型的纯函数和常量：
- reminder.py：提醒时间解析、状态机、去重规则
- memory.py：上下文预算、摘要策略常量
- ports.py：领域层定义的接口（仓储、模型客户端），实现在 store/ 和 model/

硬规则：本包不许碰 sqlite3、http、langchain、docker，
以及 os.environ 之外的任何 infra（见 grep 检查）。
"""
