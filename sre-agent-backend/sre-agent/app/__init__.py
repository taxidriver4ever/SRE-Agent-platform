"""SRE Agent 服务顶层包。

具体能力按 core、llm、tools、agent、api 分层，顶层包不主动导入应用实例，
避免仅导入某个子模块时提前创建 HTTP 客户端。
"""
