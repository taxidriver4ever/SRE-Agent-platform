# user-service

Python 3.12 / FastAPI / SQLAlchemy 用户服务。模块包含 API、Service、Repository、ORM Model、Pydantic Schema、Config、CPU Workload、Observability 和测试。

业务 API：用户资料、会员权益、游标分页用户列表。故障重点为 CPU 密集计算、Event Loop 阻塞和数据库延迟。

## API 与故障模式

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/users/{user_id}` | 用户资料 |
| GET | `/users/{user_id}/membership` | 会员等级和折扣 |
| GET | `/users?after_id=...&limit=...` | 用户列表 |
| GET | `/health` | 健康、版本、Pod 和故障状态 |
| GET | `/metrics` | Prometheus 指标 |
| GET/POST | `/debug/fault?mode=...` | 查询或设置当前 Pod 故障 |

故障白名单为 `normal`、`cpu_saturation`、`event_loop_blocking`、`database_latency`。CPU 场景执行真实素数计算，便于从 Pod CPU、应用 gauge 与 HTTP P95 交叉验证。

## 配置与运行

主要变量为 `DATABASE_URL`、`SERVICE_VERSION`、`POD_NAME`、`OTEL_EXPORTER_OTLP_ENDPOINT`。默认数据库地址面向 Kubernetes；本地运行时应覆盖为宿主机的 Lab MySQL，并从未提交的本地环境注入凭据。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DATABASE_URL = "mysql+pymysql://<user>:<password>@127.0.0.1:13307/sre_lab"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8082
```

故障接口仅供本地隔离实验使用。完整场景由 [Infra](../sre-lab-infra/README.md) 负责注入和恢复。

返回 [实验工作区](../README.md)。
