package local.srelab.order.exception;
import java.util.Map;
import org.slf4j.Logger;import org.slf4j.LoggerFactory;import org.slf4j.MDC;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.*;
@RestControllerAdvice
public class ApiExceptionHandler {
 private static final Logger log=LoggerFactory.getLogger(ApiExceptionHandler.class);
 @ExceptionHandler(Exception.class)
 public ResponseEntity<Map<String,Object>> handle(Exception error){
 int status=503;String code="ORDER_UNAVAILABLE",message="order temporarily unavailable";
 if(error instanceof ResourceAccessException || hasTimeoutCause(error)){status=504;code="DEPENDENCY_TIMEOUT";message="downstream request timeout";}
 else if(error instanceof RestClientResponseException e){status=e.getStatusCode().is4xxClientError()?409:502;code="DEPENDENCY_REJECTED";message="downstream request rejected";}
 else if(error instanceof org.springframework.dao.EmptyResultDataAccessException){status=404;code="ORDER_NOT_FOUND";message="order not found";}
 else if(error instanceof IllegalArgumentException || error instanceof org.springframework.http.converter.HttpMessageNotReadableException){status=400;code="INVALID_ORDER";message="invalid order parameters";}
 else if(error instanceof IllegalStateException){status=409;code="ORDER_STATE_CONFLICT";message=error.getMessage();}
 log.error("order request failed code={}",code,error);
 return ResponseEntity.status(status).body(Map.of("code",code,"message",message,"request_id",java.util.Objects.toString(MDC.get("request_id"),"")));
 }
 private static boolean hasTimeoutCause(Throwable error){
  // RestClient can wrap a response-body timeout in a plain RestClientException.
  for(int depth=0;error!=null && depth<20;depth++,error=error.getCause()){
   if(error instanceof java.net.SocketTimeoutException || error instanceof java.net.http.HttpTimeoutException)return true;
  }
  return false;
 }
}
