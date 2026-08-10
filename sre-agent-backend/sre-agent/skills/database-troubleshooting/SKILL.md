---
name: database-troubleshooting
description: Diagnose slow SQL, connection pool exhaustion, locks, high rows examined, inefficient execution plans, missing indexes, and SQL digest hotspots. Use when traces or logs point from an application request into MySQL.
---

# Database Troubleshooting

## 什么时候触发

Trace 显示 JDBC/MySQL Span 较慢，日志出现连接获取超时，或数据库请求延迟与 HTTP P95 同时上升时触发。

## 目标

用慢查询、SQL Digest、EXPLAIN 和运行源码建立“查询形态 → 扫描/等待 → HTTP 症状”的因果链。

## 推荐诊断顺序

1. 查询最近 30 分钟 Slow Query Top N。
2. 对照 SQL Digest 的次数、总耗时和 rows examined。
3. 仅对目标 SELECT 执行 EXPLAIN。
4. 检查 type、key、rows、filtered 与 Extra。
5. 从运行 Git SHA 读取 Repository/DAO 查询代码。

## 关注指标

关注 query time、lock time、rows examined、Hikari active/pending、连接获取超时和 HTTP P95。

## 关注日志

关注 Slow Query、Hikari timeout、SQLTimeout、deadlock、lock wait timeout 与数据库连接重置。

## 推荐 Tools

使用 `query_slow_queries`、`query_sql_digest`、`explain_sql`、`query_trace`、`read_file_at_commit`、`search_code`。

## 证据要求

确定根因至少需要 Slow Query/EXPLAIN/Trace/Source 中两个独立来源；必须报告 rows examined 和执行计划关键字段。

## 禁止事项

只允许 SELECT 与 EXPLAIN SELECT。禁止 INSERT、UPDATE、DELETE、DDL、多语句、注释绕过和凭猜测建议索引。
