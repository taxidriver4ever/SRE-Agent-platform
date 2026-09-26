"""Optional native Python instrumentation, initialized before framework imports."""
import os


def start() -> None:
    if os.getenv("SKYWALKING_AGENT_ENABLED", "false").lower() == "true":
        from skywalking import agent, config
        config.init(agent_name=os.getenv("SKYWALKING_SERVICE_NAME", "recommendation-service"),
                    agent_collector_backend_services=os.getenv("SKYWALKING_AGENT_COLLECTOR", "skywalking-oap:11800"))
        agent.start()


def correlation() -> tuple[str | None, str | None]:
    if os.getenv("SKYWALKING_AGENT_ENABLED", "false").lower() == "true":
        from skywalking.trace.context import get_context
        context = get_context()
        if context.active_span is not None:
            return str(context.segment.related_traces[0]), str(context.active_span.sid)
    return None, None
