"""把服务与远程 Git URL 写入 Deployment 和 Pod Template 注解。

脚本是显式的运维配置动作，不会由 Agent 自动执行。Agent 本身保持只读；只有
平台管理员在确认映射文件后手动运行本脚本，Kubernetes 才会更新绑定。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def load_bindings(path: Path) -> dict[str, str]:
    """读取并严格校验 service→HTTPS URL，避免 token、query 或本机 URL 入集群。"""
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bindings = payload.get("repositories")
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("绑定文件必须包含非空 repositories 映射")
    validated: dict[str, str] = {}
    for service, value in bindings.items():
        url = str(value).strip()
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"{service}: 只允许 HTTPS Git URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(f"{service}: URL 不能包含凭据、query 或 fragment")
        validated[str(service)] = url.rstrip("/")
    return validated


def kubectl(*arguments: str) -> None:
    """用 argv 调用 kubectl；从不经过 shell，也不拼接用户输入。"""
    subprocess.run(["kubectl", *arguments], check=True)


def bind_service(namespace: str, service: str, repository_url: str) -> None:
    """同时更新 Deployment 元数据和 Pod Template，使当前/未来 Pod 都带绑定。"""
    kubectl(
        "annotate", "deployment", service, "-n", namespace,
        f"sre.agent/repository={service}",
        f"sre.agent/repository-url={repository_url}",
        "--overwrite",
    )
    # patch 内容由 json.dumps 生成，URL 始终是 JSON 字符串而非命令片段。
    patch = {
        "spec": {"template": {"metadata": {"annotations": {
            "sre.agent/repository": service,
            "sre.agent/repository-url": repository_url,
        }}}}
    }
    kubectl(
        "patch", "deployment", service, "-n", namespace,
        "--type=merge", "-p", json.dumps(patch, ensure_ascii=False),
    )


def main() -> None:
    """解析命令行并逐个绑定；任意 Deployment 失败时返回非零退出码。"""
    parser = argparse.ArgumentParser(description="Bind Kubernetes workloads to remote Git repositories")
    parser.add_argument("mapping", type=Path, help="repository-bindings.yaml 路径")
    parser.add_argument("--namespace", default="sre-lab", help="目标 Namespace，默认 sre-lab")
    args = parser.parse_args()
    for service, repository_url in load_bindings(args.mapping).items():
        bind_service(args.namespace, service, repository_url)
        print(f"bound {service} -> {repository_url}")


if __name__ == "__main__":
    main()
