"""由服务端注入、模型不可覆盖的项目和任务 Scope。"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from app.security.models import TaskSecurityScope


_scope: ContextVar[TaskSecurityScope | None] = ContextVar("task_security_scope", default=None)


def current_task_scope() -> TaskSecurityScope | None:
    return _scope.get()


@contextmanager
def task_security_scope(
    user_id: str,
    project_id: str,
    task_id: str,
    workspace: str | None = None,
) -> Iterator[TaskSecurityScope]:
    scope = TaskSecurityScope(user_id, project_id, task_id, workspace)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
