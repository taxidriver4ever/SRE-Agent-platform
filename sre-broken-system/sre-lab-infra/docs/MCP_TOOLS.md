# MCP Tools

Kubernetes 新增 Pod 级 `get_pod_image`、`get_pod_version`、`get_events`、`get_resource_limits`、`get_pod_metrics`，并保留 namespace/deployment/service/pod/status/restart/image 读取。

Observability 提供 `query_metrics`、`query_logs`、`query_trace`、`query_slow_queries`、`query_sql_digest`、`explain_sql`、`get_service_health`。

Git 支持 Catalog 白名单多仓库的 `get_repository`、`get_current_commit`、`get_commit`、`get_previous_commit`、`get_commit_diff`、`read_file`、`read_file_at_commit`、`search_code`、`list_changed_files`。所有代码读取都要求运行 SHA，不默认读取 HEAD。

Tool 层仅允许 get/list/top、SELECT/EXPLAIN SELECT、Git read/search/diff；含参数校验、超时、结果截断和结构化错误。
