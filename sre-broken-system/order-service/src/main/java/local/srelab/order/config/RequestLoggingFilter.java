package local.srelab.order.config;
import java.io.IOException;
import jakarta.servlet.*;import jakarta.servlet.http.*;
import org.slf4j.*;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
@Component
public class RequestLoggingFilter extends OncePerRequestFilter {
 private static final Logger log=LoggerFactory.getLogger(RequestLoggingFilter.class);
 private final FaultState faults;
 private final java.util.concurrent.atomic.AtomicInteger active=new java.util.concurrent.atomic.AtomicInteger();
 public RequestLoggingFilter(FaultState faults,io.micrometer.core.instrument.MeterRegistry registry){this.faults=faults;registry.gauge("sre_http_active_requests",active);}
 @Override protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain)throws ServletException,IOException{
 String rid=request.getHeader("x-request-id");if(rid==null||!rid.matches("[a-zA-Z0-9._-]{1,128}"))rid=java.util.UUID.randomUUID().toString();
 MDC.put("request_id",rid);response.setHeader("X-Request-ID",rid);long start=System.nanoTime();active.incrementAndGet();
 try {
  if(request.getRequestURI().equals("/orders") && java.util.Set.of("thread_pool_saturation","downstream_timeout","dependency_timeout").contains(faults.mode())){
   try{Thread.sleep(faults.delayMs());}catch(InterruptedException e){Thread.currentThread().interrupt();}
  }
  chain.doFilter(request,response);
 }finally{
  active.decrementAndGet();
  Object route=request.getAttribute(org.springframework.web.servlet.HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE);
  log.atInfo().addKeyValue("endpoint",route==null?"unmatched":route).addKeyValue("latency_ms",(System.nanoTime()-start)/1000000.0).addKeyValue("status",response.getStatus()).log("http_request");
  MDC.remove("request_id");
 }
 }
}
