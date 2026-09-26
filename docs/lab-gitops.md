# Lab 独立 CI / GitOps 交付

本次增量在现有平台发布链路之外补齐六个故障服务。根目录 Compose、业务 API、故障代码、
Gateway、Frontend、DiagnosisTask / Checkpoint / Lease / Heartbeat / CAS 数据机制不变。
应用仓库 `deploy/gitops-template/` 是初始化模板，不是运行中的 desired state。

## 两条发布链路

```text
Application Git main
        |
   GitHub Actions
     /        \
Platform       Lab (per-service last desired SHA / PR merge base)
3 images       order / inventory / user / payment / notification / recommendation
     |         |
build -> runtime -> Trivy CRITICAL -> push same image -> digest artifact
     |         |
 GHCR immutable full source SHA + sha256 digest
     |         |
 Independent Deployment Git
 dev/values.yaml       dev/lab-values.yaml
 charts/sre-platform   charts/sre-lab
     |                     |
 Argo sre-dev          Argo sre-lab-dev
 namespace sre-dev     namespace sre-lab
```

平台保留原六项质量门禁、三个镜像、staging/prod 推广。平台 Application 继续叫 `sre-dev`，
避免重命名导致两个 Application 接管同一资源。Lab 单独 matrix，不依赖平台构建与发布结果，
只共享交付契约校验。Lab 当前自动交付 dev，不把实验故障服务推广到平台 prod。

PR 对变更 Lab 服务执行 Dockerfile 内测试、构建、运行检查、扫描，不登录或推送 GHCR，
不读取部署仓库 Secret。main 使用每个服务上次 GitOps desired image 的 tag 作为 diff 基线。
因此上次失败/被更新 main 跳过的服务会在下次重新被选中，不会被单纯 `before..after` 漏掉。
缺少首次发布基线或 SHA 对象时明确输出原因并选择相应服务；Git diff 错误直接失败。
修改 infra、Compose、交付脚本、GitOps 模板或 CI 配置会选择所有服务。
如果 GitOps 故意回滚了一个服务，下次 main push 会按当前源码重新交付；
需要长期冻结时先暂停 Lab 发布 Job，而不是期待回滚自动成为永久 pin。

每个镜像使用 `ghcr.io/<owner>/sre-lab-<service>:<40位源SHA>`，构建参数为 `VCS_REF` 和
`VCS_SOURCE`。现有 SHA 镜像复用后仍须通过运行检查和扫描；禁止覆盖其内容。
CRITICAL（包括未修复漏洞）阻止发布，不通过 ignore/continue-on-error 绕过。
Lab artifact 为 `lab-digest-*`，与平台 `digest-*` 隔离。两组 GitOps writer 使用独立并发组，
遇到部署仓库并发提交则 fetch、重新应用自己的 values 修改、普通 push，最多三次，不 force push。

## 服务契约与观测

| 服务 | 技术栈 / 端口 | 健康检查 | 依赖 / Trace |
|---|---|---|---|
| order-service | Java 21 / Spring Boot / 8080 | `/actuator/health/readiness`、`/actuator/health/liveness` | MySQL、inventory/user/payment/notification；Java OTel agent |
| inventory-service | Go / 8081 | `/health` | recommendation；已有 OTLP HTTP |
| user-service | Python 3.12 / FastAPI / 8082 | `/health` | MySQL；Python SkyWalking agent |
| payment-service | Node 22 / TypeScript / 8083 | `/health` | notification；保留 traceparent 传播与日志关联 |
| notification-service | Go / 8084 | `/health` | provider simulator；保留日志 trace ID |
| recommendation-service | Python 3.12 / FastAPI / 8085 | `/health` | 内存目录；Python SkyWalking agent |

order 指标为 `/actuator/prometheus`，其余 `/metrics`。Service 名、端口及 namespace 与现有
Prometheus 配置一致。Filebeat 继续采集 `*_sre-lab_*.log`，Logstash → Elasticsearch；
已有 OTel → SkyWalking、Python 原生 SkyWalking 链路不变。payment/notification 现有实现
不生成完整原生 span，这次没有新增 tracing SDK，不能把日志 trace ID 当成完整链路验证结果。
平台跨 namespace 访问继续用 `*.sre-lab.svc.cluster.local`；Chart 未写入 Pod IP。

Chart 复用原有资源请求/上限、Deployment selectors 和 Service selectors；增加 startupProbe、
readiness/liveness、无 API token 的 ServiceAccount、Secret 引用与版本元数据。
order 默认保留 NodePort 30080，其他 ClusterIP。平台读 `sre-lab` 的只读 RBAC 仍由平台 Chart 管理。

## 接入现有部署仓库

全新部署仓库继续使用应用仓库的 `scripts/export_gitops.py`。已有仓库不要重新导出覆盖；
在独立部署仓库开分支，增量复制并评审以下内容：

* `charts/sre-lab/`
* `environments/dev/lab-values.yaml`
* `argocd/sre-lab-dev.yaml`：将 repoURL 改为你的部署仓库；targetRevision 与 GITOPS_BRANCH 保持一致。
* `.github/workflows/validate.yml` 中新增 `lab` Job，合并到已有验证流程。

保持 `environments/dev/values.yaml` 和已有平台 Application 不变。
确认 AppProject 已授权 `sre-lab` namespace 及 Deployment/Service/ServiceAccount；现有模板已包含。
应用仓库 Actions 使用原有 `GITOPS_REPOSITORY`、可选 `GITOPS_BRANCH`、限定范围的 `GITOPS_TOKEN`。
私有 GHCR 为 namespace 创建 read:packages pull Secret，并设置 `imagePullSecrets`。
Argo 私有仓库访问配置仍在集群侧完成。

首次 `lab-values.yaml` 明确为 `releaseReady: false`，六个 image 为空。
部署仓库检查将其报告为未初始化；Chart 本身拒绝渲染，防止同步虚假 SHA/digest。
首次 CI 必须发布全部六个镜像，updater 才能原子地填满并设 `releaseReady: true`。
后续可选择部分服务更新，其余服务镜像、配置、平台配置都保留。

```bash
# 在应用 checkout 中；digests-lab 只含本次 Lab source_sha/image/digest JSON。
python scripts/ci/update_gitops.py --component-group lab --repo /path/to/deploy \
  --environment dev --sha "$SOURCE_SHA" --registry ghcr.io/taxidriver4ever --digests /path/to/digests-lab
```

## 集群准备与首次接管（操作员执行，CI 不执行）

1. 备份、核对现有资源，检查旧 `order-service-canary` 为 0 副本；如有活跃旧 canary，先结束旧实验，
   避免它继续通过同一个 order Service 接收流量。Chart 默认不创建/接管旧 canary。
2. 复用已有 `sre-lab` MySQL PVC 和观测服务。新环境可运行
   `sre-broken-system/sre-lab-infra/scripts/deploy-lab.ps1 -InfrastructureOnly`；此命令安装/更新现有基础设施，
   会应用原有幂等 SQL 并重启观测工作负载，不适合在进行中的实验中执行。它不部署六个业务服务。
3. 在 `sre-lab` 创建 Secret `sre-lab-database`，键 `order-password` 与现有 `sre_app` 密码一致，
   `user-database-url` 为完整 SQLAlchemy URL（特殊字符必须 URL 编码）。DB_USERNAME/DB_URL 可在 order config
   中覆盖。Secret 不提交 Git，不在 CI 中生成；不要重置 MySQL root Secret/PVC。
4. 确认六个真实 digest 已发布，审核独立部署仓库 `lab-values.yaml`，执行 helm lint/template。
5. 在独立部署仓库执行 `kubectl --context kind-sre-lab apply -f argocd/sre-lab-dev.yaml`。
   这是操作员引导步骤，不是 Actions 的部署步骤。检查 Argo Sync/Healthy、六个 Deployment
   rollout、探针和观测数据；资源 selector 保持兼容。MySQL 与观测组件不被新 Application 接管/prune。

Secret 可用外部私密文件创建，例如（Bash/WSL，文件路径指向 Git 目录之外）：

```bash
kubectl --context kind-sre-lab -n sre-lab create secret generic sre-lab-database \
  --from-file=order-password=/private/lab/order-password \
  --from-file=user-database-url=/private/lab/user-database-url
```

文件内容不要尾随换行。Secret 变更后由操作员通过受审计的 desired state 变更触发滚动。

## 故障注入与 Argo 自愈

原有业务故障逻辑没有移除。HTTP 运行时 fault mode 不属于 Deployment desired state，
可在 Argo 自动同步开启时注入；Pod 重启后模式会丢失，这是现有运行时模型的行为。

```powershell
# 操作员针对各 Pod 调用现有接口，不写 Deployment。
.\sre-broken-system\sre-lab-infra\scripts\set-runtime-fault.ps1 -Service order-service -Mode slow_sql
.\sre-broken-system\sre-lab-infra\scripts\set-runtime-fault.ps1 -Service user-service -Mode cpu_saturation
.\sre-broken-system\sre-lab-infra\scripts\reset-lab.ps1 -RuntimeOnly
```

其余模式以各服务 `/debug/fault` 及代码白名单为准，非法模式返回错误。
Python 服务原本有两个 uvicorn worker，fault state 是进程内对象，一次请求不能保证命中所有 worker；
需通过持续请求观测实际影响，不能把一次 POST 视为全 Pod 故障已覆盖。这次未改变 worker/API 语义。
慢 SQL、timeout、5xx、连接异常、CPU/内存等现有模式和源码回归仍可使用。

镜像/副本/probe/资源回归必须在独立 GitOps 仓库改 `lab-values.yaml` 后 commit/push。
旧 `deploy-lab`、`reset-lab`、`run-scenario` 会在检测到 GitOps 管理的 Deployment 时拒绝直接覆盖，
提示使用 GitOps 或 HTTP 接口；旧手动 Kind 环境行为继续保留。
不要关闭所有 selfHeal，也不要用 Actions `kubectl set image` 绕过审计。

SRE-009 混合版本可在 `orderCanary` 配置 `enabled: true`、`replicas: 1`、另一份已扫描发布的
order image repository/tag/digest；稳定 order 保持三副本，Service selector 不含 track，真实分流。
canary 与稳定镜像可来自不同的 monorepo 源 SHA；禁止把旧嵌套仓库的 good/bad SHA 冒充新 GHCR tag。
结束实验通过 Git revert 恢复 desired state；回滚后检查 Argo Healthy 和观测指标。不要仅点临时回滚
然后任由自动同步又恢复到当前 Git HEAD。

## Deployment regression 证据链

```text
P99 / 错误率 / 资源异常
 -> Prometheus + Elasticsearch + SkyWalking 证据
 -> Kubernetes Deployment / Pod / Events
 -> image repository:sourceSHA@sha256:digest
 -> sre.agent/git-sha / sre-agent/source-sha
 -> 白名单 GitHub monorepo URL + 精确 source-path
 -> get_commit(sourceSHA)
 -> get_commit_diff(previous deployed SHA, sourceSHA)
 -> 源码变化与运行证据共同验证 deployment regression
```

Deployment 和 Pod 都有版本 label、source SHA、digest、repository URL、source path 注解。
updater 保存之前的已发布 SHA；canary 使用当前稳定版作为比较基线。
Agent 修复了 SHA@digest / digest-only 镜像的解析，并按选中 Pod（含少数版本 canary）绑定源码元数据。
首次发布没有 previous SHA 时比较父提交；远程获取保留父提交，并显式获取较早部署基线。
Agent 仍受 HTTPS 主机白名单与 Catalog 服务白名单约束，不从任意 annotation 克隆任意地址。
若源码仓库私有，需在 Agent 运行环境提供授权只读 Git 访问；访问失败必须保留为证据缺失，不能推断成功。
只有版本变化不足以确认根因，仍由原有证据验证流程决定结论。

## 验证方法

```bash
python -m pytest scripts/ci/tests -q
python scripts/ci/validate_delivery.py
actionlint .github/workflows/ci.yml deploy/gitops-template/.github/workflows/validate.yml
python scripts/ci/lab_runtime_check.py --service order-service --image "$IMAGE_REF" --sha "$SOURCE_SHA"
```

`validate_delivery.py` 使用明确的合成 SHA/digest 验证渲染契约，不能作为镜像已经发布的证明。
运行检查启动临时容器，order/user 使用临时 MySQL 和已有合成 schema，验证 OCI revision、健康、metrics、fault API，
最后清理容器/网络，不连接业务数据库。它不等同完整生产流量或观测端到端验收。
真实 GHCR push、GitOps push、Argo 同步和集群端到端证据收集必须在接入后单独验收。

本次扫描发现两个旧 Go 1.23 构建产物包含 `CVE-2025-68121`，因此仅把两个 Go Dockerfile
的编译器升级到固定 `golang:1.26.8-alpine`，保留业务代码和故障逻辑；重建后的镜像通过了
CRITICAL 复扫。漏洞范围见 [Go 官方漏洞记录](https://pkg.go.dev/vuln/GO-2026-4337)，
版本说明见 [Go Release History](https://go.dev/doc/devel/release#go1.26.0)。

## 本次修改文件范围

* CI：`.github/workflows/ci.yml`。
* 交付脚本：`scripts/ci/update_gitops.py`、`lab_changes.py`、`lab_runtime_check.py`、
  `validate_delivery.py`、`validate_lab.py`；测试 `scripts/ci/tests/test_lab_delivery.py`。
* GitOps 模板：`charts/sre-lab/Chart.yaml`、`values.yaml`、`templates/workloads.yaml`、
  `templates/services.yaml`、`environments/dev/lab-values.yaml`、`argocd/sre-lab-dev.yaml`、
  `.github/workflows/validate.yml`、`README.md`（均位于 `deploy/gitops-template/`）。
* Agent：`app/workflow/runtime_extractor.py`、`models.py`、`planning/decision_rules.py`、
  `planning/planner.py`、`app/repositories/registry.py`、`app/mcp_servers/git/tools.py`、
  `tests/test_runtime_provenance.py`（均位于 `sre-agent-backend/sre-agent/`）。
* 镜像：`sre-broken-system/inventory-service/Dockerfile`、`notification-service/Dockerfile`。
  订单安全依赖调整位于 `sre-broken-system/order-service/Dockerfile` 与 `pom.xml`。
* 实验操作：`sre-broken-system/sre-lab-infra/scripts/` 下的 `deploy-lab.ps1`、`reset-lab.ps1`、
  `run-scenario.ps1`、新增 `gitops-guard.ps1`、`set-runtime-fault.ps1`。
* 文档：根 `README.md`、`docs/gitops-delivery.md`、本文。

此前未提交的 Node 24 Actions / Ubuntu runner / CI 契约修复继续保留，包括
`sre-agent-backend/sre-agent/tests/test_cicd_config.py`；没有回退这些修改。

订单镜像初次扫描还发现 OTel Java agent 和内嵌 Tomcat 的 CRITICAL 漏洞，因此将 Java agent
更新到 `2.27.0`，Tomcat 在同一 `10.1` 分支更新到 `10.1.59`，Spring Boot/API 基线保持不变。
Tomcat 的 `10.1.58` 候选版本未通过发布投票，采用官方实际发布的 `10.1.59`，见
[Tomcat 安全公告](https://tomcat.apache.org/security-10)。Java agent 修复依据见
[OTel 发布记录](https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/tag/v2.27.0)。

## 本地验证结果（2026-09-27）

| 状态 | 验证 | 结果 |
|---|---|---|
| PASS | Actions YAML | actionlint 1.7.12 校验应用 CI 与 GitOps validate 工作流 |
| PASS | 交付 Python 测试 | 26 passed，覆盖分组、部分更新、幂等、错误制品、path filter 与失败发布补偿 |
| PASS | Agent 全量回归 | 271 passed；独立临时 MySQL 8.4 和 Elasticsearch 8.17.3，343.37 秒 |
| PASS | 最终定向回归 | 24 passed，含新增的 previous SHA / 远程 diff 获取测试、Planner 和 CI 契约 |
| PASS | Helm 3.17.3 | 平台 dev/staging/prod 与 Lab lint/template；六服务、可选 canary、非法镜像拒绝、Secret/DNS/probe/selector 验证 |
| PASS | 新部署仓库导出 | 平台 SHA 初始化、Lab 保持明确未初始化、Lab Argo repoURL 正确替换 |
| PASS | PowerShell | 语法检查及 GitOps ownership guard 的模拟成功/拒绝/查询失败验证 |
| PASS | 六个 Docker build | Dockerfile 内的各语言测试与打包完成，安全更新后重建通过 |
| PASS | 六个镜像运行检查 | OCI revision、health、metrics、fault API；order/user 使用临时 MySQL |
| PASS | 六个镜像安全门禁 | Trivy 0.69.3，CRITICAL、exit-code=1、未忽略未修复漏洞；最终各镜像均 0 CRITICAL |
| FAIL → 已修复 | 首次镜像扫描 | 两个 Go 镜像各 1 项、订单镜像 7 项 CRITICAL；上述依赖更新后复扫通过 |
| NOT RUN | GHCR / GitOps 远程写入 | 未推送镜像、未提交或推送应用/部署仓库 |
| NOT RUN | Argo / 集群端到端 | 未注册或同步 Application，未在现有集群执行故障实验或完整 Prometheus/ELK/SkyWalking 验收 |

最终扫描服务：order、inventory、user、payment、notification、recommendation 均为 0 CRITICAL；
这不表示不存在其他严重级别的漏洞。扫描结果随漏洞库变化，CI 每次仍须重新扫描。
本地镜像使用 `sre-ci/` 前缀，源 SHA 标签仅用于工作区验证身份测试；未提交的依赖改动也参与了构建，
这些本地镜像不是可发布的提交制品。正式发布由 GitHub checkout 的 `GITHUB_SHA` 重新构建并记录 GHCR digest。
测试容器与临时扫描缓存清理后，以上结果作为本次验证记录保留；不将本地合成 digest 写入真实 desired state。
