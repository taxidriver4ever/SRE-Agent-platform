"""管理员手动验证 Docker Sandbox 的真实资源与网络隔离入口。"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sandbox import DockerSandboxManager


async def verify(image: str) -> None:
    manager = DockerSandboxManager(
        ".sandbox-tasks",
        image=image,
        cpus=0.25,
        memory_mb=128,
        pids_limit=32,
        timeout_seconds=30,
    )
    async with manager.task_workspace("sandbox-smoke"):
        output = await manager.run("sandbox-smoke", ["mysql", "--version"])
    print(output.strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="mysql:8.4")
    arguments = parser.parse_args()
    asyncio.run(verify(arguments.image))
