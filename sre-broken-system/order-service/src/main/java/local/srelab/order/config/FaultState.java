package local.srelab.order.config;

import java.util.Set;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.stereotype.Component;

/**
 * 进程级故障开关。每个 Pod 独立保存状态，因此场景脚本可以只命中一个 Pod，
 * 真实复现 SRE-008，而不会把异常错误地扩散到整个 Deployment。
 */
@Component
public class FaultState {
    private static final Set<String> ALLOWED = Set.of(
            "normal", "slow_sql", "pool_exhaustion", "dependency_timeout",
            "retry_storm", "single_pod_slow", "bad_health");
    private final AtomicReference<String> mode =
            new AtomicReference<>(System.getenv().getOrDefault("FAULT_MODE", "normal"));

    /** 返回无锁的原子快照，供请求路径决定是否注入故障。 */
    public String mode() {
        return mode.get();
    }

    /** 只接受预定义实验模式，禁止把任意字符串当作可执行行为。 */
    public boolean changeTo(String requestedMode) {
        if (!ALLOWED.contains(requestedMode)) {
            return false;
        }
        mode.set(requestedMode);
        return true;
    }
}
