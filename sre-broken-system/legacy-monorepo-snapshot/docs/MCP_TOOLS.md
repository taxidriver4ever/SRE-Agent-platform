# MCP Tools

## Kubernetes

`list_namespaces`、`list_deployments`、`list_pods`、`get_pod`、`get_pod_status`、`get_pod_events`、`get_deployment`、`get_container_image`、`get_resource_usage`、`get_restart_count`、`get_service`。

只构造固定 `kubectl get/list/top` argv；名称按 DNS 字符校验。无 delete/restart/scale/apply/patch。

## Observability

`query_metrics`、`query_logs`、`query_trace`、`query_slow_queries`、`query_sql_digest`、`explain_sql`、`get_service_health`。

支持 service、time range、level、keyword、limit；结果结构化、Top N、超时并截断。SQL 只允许单条 SELECT/EXPLAIN SELECT，且数据库账号为 `sre_reader`。

## Git

`get_repository`、`get_current_commit`、`get_commit`、`get_commit_diff`、`read_file`、`read_file_at_commit`、`search_code`、`list_changed_files`。

只允许 show/log/diff/grep/rev-parse 等读取操作；路径必须在仓库根内；代码读取优先使用 K8s 当前 Image 对应的完整 Git SHA。

## 可靠性

默认 Tool 超时 15 秒、输出 12000 字符、Agent 最大 12 步。每次调用保存 name、arguments、summary、timestamp、duration 和 error。
