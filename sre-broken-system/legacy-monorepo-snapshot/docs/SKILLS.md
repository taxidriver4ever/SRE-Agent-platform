# Skills

项目 Skills 位于 `sre-agent/skills/<name>/SKILL.md`，均通过 skill-creator 校验器。

- incident-triage：从模糊问题确定服务、症状、时间和环境。
- java-service-debugging：HikariCP、GC、OOM、线程池、SQLTimeout、下游超时。
- database-troubleshooting：Slow Query、Digest、EXPLAIN、Index、Pool、Lock。
- kubernetes-debugging：Pod、Event、restart、resource、Image 和 Git SHA。
- resource-exhaustion：CPU、memory、OOM、连接池和线程池耗尽。
- dependency-timeout：Trace 下游调用、timeout、retry amplification。
- deployment-regression：运行 SHA、GOOD..BAD diff 与源码映射。
- evidence-based-diagnosis：双证据门槛、因果链、置信度和报告。

每个 Skill 都包含触发条件、目标、诊断顺序、指标、日志、Tools、证据要求和禁止事项。
