"""Ollama Adapter 的模型模板兼容行为测试。"""

from app.gateway.provider.ollama_adapter import _without_thinking


def test_without_thinking_keeps_plain_content():
    """不含思考标签的普通模型回答不应被改变。"""
    assert _without_thinking("  正常回答  ") == "正常回答"


def test_without_thinking_removes_qwen_reasoning_prefix():
    """Qwen3 混入 content 的思考前缀应在返回客户端前移除。"""
    content = "内部推理过程\n</think>\n{\"type\":\"final\",\"answer\":\"完成\"}"
    assert _without_thinking(content) == '{"type":"final","answer":"完成"}'
