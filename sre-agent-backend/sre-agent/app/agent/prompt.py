"""Tool Agent 的 JSON-ReAct 协议提示词。

网关当前只接受普通文本消息，不能直接传递 OpenAI 风格的 ``tools`` 和
``tool_calls`` 字段。因此本模块把工具定义序列化进系统提示词，并要求模型用
固定 JSON 对象表达下一步动作。协议集中在这里，便于网关支持原生工具调用后
整体替换，而不影响工具注册和执行层。
"""

import json
from typing import Any


def build_system_prompt(tool_specs: list[dict[str, Any]]) -> str:
    """根据当前注册的工具生成系统提示词。

    Args:
        tool_specs: 工具注册中心导出的工具描述。每项包含工具名、用途和
            JSON Schema 格式的输入约束。

    Returns:
        可直接作为 ``system`` 消息发送给模型的完整提示词。

    ``ensure_ascii=False`` 保留中文工具描述，提高提示词可读性；缩进只影响
    发送文本的展示，不改变 JSON 语义。
    """
    tools_json = json.dumps(tool_specs, ensure_ascii=False, indent=2)
    return f"""你是一个可靠的 SRE Tool Agent。请分析用户请求并决定调用工具或直接回答。

可用工具：
{tools_json}

每次只能输出一个 JSON 对象，不要输出 Markdown、代码围栏或额外文字。
调用工具时：{{"type":"tool","tool":"工具名","tool_input":{{...}}}}
完成任务时：{{"type":"final","answer":"最终答复"}}
工具执行结果会以 user 消息返回。不要虚构工具结果；需要工具时必须先调用。"""
