# Service Catalog

权威机器可读文件为 `../service-catalog.yaml`。每项包含 serviceName、中文/英文 aliases、language、framework、repository、port、dependencies、database、owner、description、source_path、GOOD/BAD SHA。

Agent 先用最长别名匹配服务，再用 repository 标识选择 Git 白名单仓库。K8s 注解只保存标识与运行 SHA，不接受任意文件系统路径。

| Service | Language | Port | Replicas | Dependencies |
|---|---|---:|---:|---|
| order-service | Java 21 | 8080 | 3 | inventory, user, payment, notification |
| inventory-service | Go 1.23 | 8081 | 2 | recommendation |
| user-service | Python/FastAPI | 8082 | 2 | MySQL |
| payment-service | Node.js/TypeScript | 8083 | 2 | notification |
| notification-service | Go 1.23 | 8084 | 2 | external-provider simulator |
| recommendation-service | Python/FastAPI | 8085 | 2 | product catalog |
