"""本地开发入口。

执行 ``python main.py`` 会以热重载方式启动应用；部署环境建议直接使用
``uvicorn app.main:app`` 并由进程管理器控制 worker 和重启策略。
"""

import uvicorn


if __name__ == "__main__":
    # 字符串导入路径是 Uvicorn reload 模式的要求；Agent 使用 8001，避免与
    # 默认运行在 8000 的 sre-gateway 端口冲突。
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=True)
