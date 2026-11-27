"""测试包启动时先定好环境变量（必须在 agent / gateway 模块 import 之前）。

这里的值全是测试专用的假值：TENANT_ID=demo、TOKEN_SECRET 和
GATEWAY_SECRET 用同一个测试密钥（网关签发的 token 才能被 agent 认出来）、
LLM_API_KEY 是假 key（e2e 测试会把 ChatDeepSeek 换成剧本假模型，绝不调真实接口）。
DATA_DIR 强制指向一个临时目录，任何测试都碰不到项目真正的 data/。
"""
import os
import tempfile

os.environ["TENANT_ID"] = "demo"
os.environ["TOKEN_SECRET"] = "test-secret-do-not-use"
os.environ["GATEWAY_SECRET"] = "test-secret-do-not-use"
os.environ["LLM_API_KEY"] = "test-dummy-key"
os.environ["REMINDER_TZ"] = "Asia/Shanghai"
# 强制覆盖：哪怕外面传了 DATA_DIR，测试也不许碰项目 data/。
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="mt-tests-default-")
