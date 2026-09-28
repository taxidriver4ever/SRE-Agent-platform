"""Optional native Python instrumentation, initialized before framework imports."""
import os


def start() -> None:
    if os.getenv("SKYWALKING_AGENT_ENABLED", "false").lower() == "true":
        from skywalking import agent, config
        config.init(agent_name=os.getenv("SKYWALKING_SERVICE_NAME", "recommendation-service"),
                    agent_collector_backend_services=os.getenv("SKYWALKING_AGENT_COLLECTOR", "skywalking-oap:11800"))
        agent.start()


def correlation() -> tuple[str | None, str | None]:
    """Use the active OTel context; the Collector exports it to SkyWalking."""
    from opentelemetry import trace
    span = trace.get_current_span().get_span_context()
    if span.is_valid:
        return format(span.trace_id,"032x"), format(span.span_id,"016x")
    return None, None
