package local.srelab.order.config;
import java.util.Map;
import java.util.Set;
import org.springframework.stereotype.Component;
@Component
public class FaultState {
 private static final Set<String> ALLOWED=Set.of("normal","slow_sql","pool_exhaustion","dependency_timeout","retry_storm","single_pod_slow","bad_health","thread_pool_saturation","downstream_timeout","retry_amplification");
 private String current="normal";private long expires=0;private int delay=3000;
 public FaultState(){changeTo(System.getenv().getOrDefault("FAULT_MODE","normal"));}
 public synchronized String mode(){if(System.currentTimeMillis()>=expires)current="normal";return current;}
 public boolean changeTo(String mode){return enable(mode,120,3000);}
 public synchronized boolean enable(String mode,int seconds,int delayMs){if(mode==null||!ALLOWED.contains(mode)||seconds<1||seconds>300||delayMs<0||delayMs>10000)return false;current=mode;expires=System.currentTimeMillis()+seconds*1000L;delay=delayMs;return true;}
 public synchronized int delayMs(){return delay;}
 public synchronized Map<String,Object> snapshot(){return Map.of("fault_mode",mode(),"expires_at",mode().equals("normal")?0:expires,"parameters",Map.of("delay_ms",delay));}
}
