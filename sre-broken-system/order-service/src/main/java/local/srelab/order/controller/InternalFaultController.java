package local.srelab.order.controller;
import java.util.Map;
import org.springframework.web.bind.annotation.*;
import local.srelab.order.config.FaultState;
@RestController
@RequestMapping("/internal/faults")
public class InternalFaultController {
 private final FaultState state;
 public InternalFaultController(FaultState state){this.state=state;}
 @GetMapping public Map<String,Object> get(){return state.snapshot();}
 @PostMapping public Map<String,Object> set(@RequestBody Input input){
 int delay=input.parameters()==null?3000:input.parameters().getOrDefault("delay_ms",3000);
 if(!state.enable(input.fault(),input.duration_seconds()==null?120:input.duration_seconds(),delay))throw new IllegalArgumentException("invalid fault parameters");return get();}
 @DeleteMapping("/{fault}") public Map<String,Object> clear(@PathVariable String fault){if(state.mode().equals(fault))state.changeTo("normal");return get();}
 public record Input(String fault,Integer duration_seconds,Map<String,Integer> parameters){}
}
