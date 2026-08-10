"""安全运行固定可执行文件的共享异步进程函数。"""

import asyncio

from fastmcp.exceptions import ToolError


async def run_fixed_command(executable: str, arguments: list[str], *, timeout: float) -> str:
    """不经过 shell 执行 argv，并对超时、启动失败和非零退出做统一转换。"""
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        if "process" in locals():
            process.kill()
            await process.wait()
        raise ToolError(f"{executable} 只读查询超过 {timeout:.0f} 秒") from exc
    except OSError as exc:
        raise ToolError(f"无法启动 {executable}: {exc}") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise ToolError(f"{executable} 查询失败: {detail}")
    return stdout.decode("utf-8", errors="replace")
