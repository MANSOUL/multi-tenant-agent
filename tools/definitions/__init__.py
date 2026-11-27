"""工具定义包：每类工具一个文件（todo/reminder/memory/file/browser/sandbox）。

各模块只暴露 specs(tenant) -> list，拼 LangChain 工具的事归
tools/registry.py；业务逻辑在 app/*_service.py。
"""
