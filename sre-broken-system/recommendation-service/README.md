# recommendation-service

Python 3.12 / FastAPI 推荐服务，包含 API、Ranking Service、Catalog Repository、Domain Model、Config、Observability 和测试。

业务 API：按商品推荐、按用户推荐。故障重点为缓存失效、大数据扫描和 O(n²) 排序算法。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/recommendations/products/{product_id}` | 相似商品推荐 |
| GET | `/recommendations/users/{user_id}?limit=10` | 用户推荐 |
| GET | `/health` | 健康、版本、Pod 和故障状态 |
| GET | `/metrics` | HTTP 与缓存指标 |
| GET/POST | `/debug/fault?mode=...` | 查询或设置当前 Pod 故障 |

故障白名单：`normal`、`cache_miss`、`large_scan`、`quadratic_ranking`。`quadratic_ranking` 使用真实 O(n²) 两两比较，为 SRE-006 的重试放大提供高 CPU 下游。

## 运行与测试

配置变量为 `SERVICE_VERSION` 和 `POD_NAME`，默认监听 8085。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8085
```

数据来自合成商品目录，不依赖外部数据库。完整的跨服务 Trace 和场景恢复由 [Infra](../sre-lab-infra/README.md) 管理。

返回 [实验工作区](../README.md)。
