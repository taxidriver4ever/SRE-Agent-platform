"""Start instrumentation before FastAPI/httpx/PyMySQL imports."""

import os


def main() -> None:
    if os.getenv("SKYWALKING_AGENT_ENABLED", "false").lower() == "true":
        from skywalking import agent, config
        config.init(agent_name=os.getenv("SKYWALKING_SERVICE_NAME", "sre-agent"),
                    agent_collector_backend_services=os.getenv("SKYWALKING_AGENT_COLLECTOR", "127.0.0.1:11800"))
        agent.start()
    from app.core.telemetry import configure_logging
    configure_logging()
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, workers=1, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
