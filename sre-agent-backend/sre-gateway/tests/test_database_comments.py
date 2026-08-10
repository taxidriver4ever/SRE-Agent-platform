"""验证所有业务 ORM 表及字段都具备可读的数据库 Comment。"""

from app.auth.model import GatewayToken
from app.gateway.model import GatewayUsageLog


def test_all_business_tables_and_columns_have_comments():
    """防止新增或修改 ORM 表时遗漏表说明或字段含义。"""
    tables = [GatewayToken.__table__, GatewayUsageLog.__table__]

    for table in tables:
        assert table.comment, f"表 {table.name} 缺少 comment"
        for column in table.columns:
            assert column.comment, f"字段 {table.name}.{column.name} 缺少 comment"
