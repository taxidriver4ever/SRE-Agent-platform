"""Official MCP tool adapter with the existing FastMCP result contract.

The official converter uses ClientSession. FastMCP exposes that public session,
so no new transport or MCP-to-Tool conversion is necessary. Its public call_tool
also preserves typed/unwrapped .data and ToolError semantics used by Evidence.
"""

from uuid import uuid4

from fastmcp import Client
from langchain_mcp_adapters.tools import load_mcp_tools
from langsmith import tracing_context
from mcp.types import CallToolResult
from app.agent.observability import call_config


async def specifications(client: Client) -> list[dict]:
    tools = await load_mcp_tools(client.session, handle_tool_errors=False)
    return [{"name": tool.name, "description": tool.description,
             "input_schema": tool.args_schema} for tool in tools]


async def call_tool(client: Client, name: str, arguments: dict):
    """Execute once through an official LangChain Tool, retain the raw domain value.

    Result capture is invocation-local, including concurrent requests and users.
    The interceptor keeps FastMCP's timeout/error/typed-result handling. It does not
    bypass policy: the owning ToolClient authorizes before calling this function.
    """
    results = []

    async def preserve_fastmcp_result(request, handler):
        result = await client.call_tool(request.name, request.args)
        results.append(result)
        return CallToolResult(
            content=result.content, structuredContent=result.structured_content,
            isError=result.is_error, _meta=result.meta,
        )

    tools = await load_mcp_tools(
        client.session, tool_interceptors=[preserve_fastmcp_result], handle_tool_errors=False,
    )
    tool = next((tool for tool in tools if tool.name == name), None)
    if tool is None:
        # Preserve the server's unknown-tool error, including its original type.
        return await client.call_tool(name, arguments)
    with tracing_context(enabled=False):
        await tool.ainvoke(
            {"type": "tool_call", "name": name, "args": arguments, "id": uuid4().hex},
            config=call_config(),
        )
    return results[0]
