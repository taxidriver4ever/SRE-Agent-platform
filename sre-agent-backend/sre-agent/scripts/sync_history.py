"""Rebuild/search projection maintenance: python -m scripts.sync_history."""
import argparse
import asyncio

from app.core.config import get_settings
from app.core.database import ApplicationDatabase
from app.history.schema import initialize_history_schema
from app.history.repository import HistoryRepository
from app.history.elasticsearch import ElasticsearchHistoryIndex
from app.history.worker import HistorySyncWorker


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="Requeue MySQL snapshots without deleting originals")
    parser.add_argument("--batches", type=int, default=1, help="Bound maintenance run (1-10000)")
    args = parser.parse_args()
    settings = get_settings()
    db = ApplicationDatabase(settings.application_mysql_host, settings.application_mysql_port,
        settings.application_mysql_user, settings.application_mysql_password, settings.application_mysql_database)
    initialize_history_schema(db)
    source = HistoryRepository(db)
    if args.rebuild:
        print({"requeued": source.rebuild()})
    index = ElasticsearchHistoryIndex(settings.elasticsearch_url, settings.elasticsearch_history_index,
        username=settings.elasticsearch_username, password=settings.elasticsearch_password,
        timeout=settings.history_timeout_seconds)
    worker = HistorySyncWorker(source, index, batch_size=settings.history_sync_batch_size, logstash_url=settings.logstash_url)
    for _ in range(max(1, min(args.batches, 10000))):
        result = await worker.run_once()
        print(result)
        if not any(result.values()):
            break


if __name__ == "__main__":
    asyncio.run(main())
