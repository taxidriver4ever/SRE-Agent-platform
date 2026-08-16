# Scenarios

使用 `.\scripts\run-scenario.ps1 -Scenario SRE-00X` 开启；使用 `.\scripts\reset-lab.ps1` 恢复 GOOD 镜像、canonical probes/replicas 和所有 Pod 故障模式。

1. SRE-001：真实前导通配 Slow SQL。
2. SRE-002：Hikari 小池与慢连接占用。
3. SRE-003：inventory reservation 下游超时。
4. SRE-004：Python 真实 CPU 密集计算。
5. SRE-005：Node Buffer 泄漏至 OOMKilled。
6. SRE-006：Go BAD 版本无退避重试放大。
7. SRE-007：order 全量 GOOD→BAD 回归。
8. SRE-008：只对一个 order Pod 注入 CPU 退化。
9. SRE-009：两个 GOOD stable + 一个 BAD canary 混合版本。
10. SRE-010：错误 liveness path 导致重启。

精确机制和证据类型由 `../scenarios/catalog.yaml` 机器可读定义。
