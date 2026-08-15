"""安全运行固定可执行文件的共享异步进程函数。"""

import asyncio
import subprocess

from fastmcp.exceptions import ToolError


async def run_fixed_command(executable: str, arguments: list[str], *, timeout: float) -> str:
    """不经过 shell 执行 argv，并对超时、启动失败和非零退出做统一转换。

    Windows 上 Uvicorn 可能运行在不支持 asyncio 子进程的事件循环中。使用
    ``to_thread + subprocess.run(shell=False)`` 可保持 argv 安全边界，同时让
    Git Code State 和 MCP HTTP 请求在服务进程内稳定执行。
    """
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [executable, *arguments],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{executable} 只读查询超过 {timeout:.0f} 秒") from exc
    except OSError as exc:
        raise ToolError(f"无法启动 {executable}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise ToolError(f"{executable} 查询失败: {detail}")
    return completed.stdout.decode("utf-8", errors="replace")
