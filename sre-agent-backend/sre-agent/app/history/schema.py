from pathlib import Path
from app.core.database import ApplicationDatabase


def initialize_history_schema(database: ApplicationDatabase) -> None:
    database.initialize_schema_file(Path(__file__).parent / "sql" / "schema.sql")
