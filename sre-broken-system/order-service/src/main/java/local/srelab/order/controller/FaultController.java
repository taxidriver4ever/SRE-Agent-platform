package local.srelab.order.controller;

import java.util.Map;
import local.srelab.order.config.FaultState;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/** 隔离实验环境专用故障控制器；生产构建应通过配置完全禁用该端点。 */
@RestController
@RequestMapping("/debug/fault")
public class FaultController {
    private final FaultState state;
    private final String version;

    public FaultController(FaultState state, @Value("${service.version:dev}") String version) {
        this.state = state;
        this.version = version;
    }

    @GetMapping
    public Map<String, String> current() {
        return Map.of("service", "order-service", "version", version, "fault_mode", state.mode());
    }

    @PostMapping("/{mode}")
    public ResponseEntity<Map<String, String>> change(@PathVariable String mode) {
        return state.changeTo(mode) ? ResponseEntity.ok(current())
                : ResponseEntity.badRequest().body(Map.of("error", "unsupported fault mode", "requested", mode));
    }
}
