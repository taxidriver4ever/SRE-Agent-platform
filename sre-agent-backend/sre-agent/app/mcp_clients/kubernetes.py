"""对第三方 ``containers/kubernetes-mcp-server`` 的只读语义适配。

这里没有实现 Kubernetes API，也没有包装 kubectl。真正的集群访问由第三方
MCP Server 直接调用 Kubernetes API 完成；本类只把项目历史工作流中的语义名
（例如 ``list_pods``）映射到该 Server 的标准工具名（例如
``pods_list_in_namespace``），从而让工作流不依赖第三方参数命名细节。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastmcp import Client


# 固定版本可以避免 npx 每次解析 latest 后出现不兼容变更。升级时应先跑完整
# MCP 清单测试和真实 Kind 联调，再显式修改此默认值。
DEFAULT_KUBERNETES_MCP_VERSION = "0.0.65"


class KubernetesMCPAdapter:
    """连接第三方 Kubernetes MCP Server，并仅暴露审核过的只读语义。"""

    # Agent 使用稳定语义名，右侧是 containers/kubernetes-mcp-server 的工具名。
    _TOOL_MAP = {
        "list_namespaces": "namespaces_list",
        "list_pods": "pods_list_in_namespace",
        "get_pod": "pods_get",
        "get_pod_events": "events_list",
        "get_restart_count": "pods_get",
        "list_deployments": "resources_list",
        "get_deployment": "resources_get",
        "get_container_image": "resources_get",
    }

    def __init__(self, namespace: str) -> None:
        """构造惰性 MCP Client；应用健康检查不会触发 npx 下载或集群连接。"""
        self.namespace = namespace
        version = os.getenv("KUBERNETES_MCP_VERSION", DEFAULT_KUBERNETES_MCP_VERSION)
        command = os.getenv("KUBERNETES_MCP_COMMAND", "npx.cmd" if os.name == "nt" else "npx")
        # --read-only 会让第三方 Server 根本不注册 create/update/delete/exec 等写工具；
        # core 限定工具集，disable-multi-cluster 则阻止模型切换到其他 kube context。
        args = [
            "-y",
            f"kubernetes-mcp-server@{version}",
            "--read-only",
            "--toolsets",
            "core",
            "--disable-multi-cluster",
            "--list-output",
            "yaml",
        ]
        kubeconfig = os.getenv("KUBERNETES_MCP_KUBECONFIG")
        if kubeconfig:
            args.extend(["--kubeconfig", kubeconfig])
        # FastMCP 官方 Client 直接消费 MCP 配置。多 Server 配置会为工具名添加
        # ``kubernetes_`` 前缀，本类在首次连接后也兼容未加前缀的实现。
        child_env = {
            # 某些 Windows 用户目录由其他账户创建，默认 npm-cache 会直接 EPERM。
            # 使用系统临时目录既可写又不污染仓库；版本仍固定，因此缓存可复用。
            "NPM_CONFIG_CACHE": os.getenv(
                "SRE_NPM_CACHE", str(Path(tempfile.gettempdir()) / "sre-agent-npm-cache")
            ),
        }
        inherited_kubeconfig = kubeconfig or os.getenv("KUBECONFIG")
        if inherited_kubeconfig:
            child_env["KUBECONFIG"] = inherited_kubeconfig
        self._client = Client({
            "mcpServers": {
                "kubernetes": {"command": command, "args": args, "env": child_env}
            }
        })
        self._entered = False
        self._available_names: set[str] = set()

    @classmethod
    def semantic_names(cls) -> set[str]:
        """返回可供 Agent 使用的稳定只读工具名。"""
        return set(cls._TOOL_MAP)

    async def close(self) -> None:
        """关闭惰性创建的 stdio MCP 子进程，未启动时无需执行任何操作。"""
        if self._entered:
            await self._client.__aexit__(None, None, None)
            self._entered = False
            self._available_names.clear()

    async def call(self, semantic_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用第三方工具，并把 YAML/TextContent 归一化为稳定 JSON 结构。"""
        if semantic_name not in self._TOOL_MAP:
            raise ValueError(f"未审核的 Kubernetes MCP 语义: {semantic_name}")
        await self._ensure_connected()
        upstream_name = self._TOOL_MAP[semantic_name]
        # FastMCP 多 Server Client 默认命名空间化工具；保留原名回退便于未来切换
        # 为单一 StdioTransport 时不需要修改业务工作流。
        callable_name = (
            f"kubernetes_{upstream_name}"
            if f"kubernetes_{upstream_name}" in self._available_names
            else upstream_name
        )
        upstream_arguments = self._translate_arguments(semantic_name, arguments)
        result = await self._client.call_tool(callable_name, upstream_arguments)
        payload = self._decode_result(result)
        return self._shape_result(semantic_name, payload)

    async def _ensure_connected(self) -> None:
        """首次 K8s 查询时启动并缓存 stdio 会话，避免每个证据都重新拉起 npx。"""
        if self._entered:
            return
        await self._client.__aenter__()
        self._entered = True
        tools = await self._client.list_tools()
        self._available_names = {tool.name for tool in tools}

    def _translate_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """只传递第三方 Server 明确定义的参数，拒绝透传未知写入参数。"""
        if name == "list_namespaces":
            return {}
        if name == "list_pods":
            translated: dict[str, Any] = {"namespace": self.namespace}
            selector = arguments.get("label_selector")
            if selector:
                translated["labelSelector"] = str(selector)
            return translated
        if name in {"get_pod", "get_restart_count"}:
            return {"namespace": self.namespace, "name": str(arguments["name"])}
        if name == "get_pod_events":
            return {
                "namespace": self.namespace,
                "fieldSelector": f"involvedObject.name={arguments['name']}",
            }
        if name == "list_deployments":
            return {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "namespace": self.namespace,
            }
        if name in {"get_deployment", "get_container_image"}:
            return {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "namespace": self.namespace,
                "name": str(arguments["name"]),
            }
        raise ValueError(f"缺少 Kubernetes MCP 参数映射: {name}")

    @staticmethod
    def _decode_result(result: Any) -> Any:
        """优先读取结构化 data，否则解析第三方 Server 返回的 JSON/YAML 文本。"""
        value = result.data
        if value is None:
            text_parts = [
                block.text for block in result.content
                if getattr(block, "type", None) == "text" and getattr(block, "text", None)
            ]
            value = "\n".join(text_parts)
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            parsed = yaml.safe_load(text)
            return parsed if parsed is not None else {"text": text}

    @staticmethod
    def _shape_result(name: str, payload: Any) -> dict[str, Any]:
        """保持现有工作流可遍历的数据形状，同时完整保留第三方返回内容。"""
        # 该 Server 的 list-output=yaml 当前直接返回对象数组，而 Kubernetes API
        # 通常返回带 items 的 List；在 Client 边界统一，工作流无需绑定具体版本。
        if isinstance(payload, list):
            payload = {"items": payload}
        if name == "get_restart_count":
            statuses = (payload or {}).get("status", {}).get("containerStatuses", [])
            restart_count = sum(int(item.get("restartCount", 0)) for item in statuses)
            shaped: Any = {"restart_count": restart_count, "pod": payload}
        elif name == "get_container_image":
            metadata = (payload or {}).get("metadata", {})
            template = (payload or {}).get("spec", {}).get("template", {})
            template_metadata = template.get("metadata", {})
            shaped = {
                "annotations": {**metadata.get("annotations", {}), **template_metadata.get("annotations", {})},
                "containers": template.get("spec", {}).get("containers", []),
                "deployment": metadata.get("name"),
            }
        else:
            shaped = payload
        # ``source`` 明确记录证据来自第三方 MCP，而非项目内自写 kubectl 包装。
        return {"data": shaped, "source": "containers/kubernetes-mcp-server", "truncated": False}
