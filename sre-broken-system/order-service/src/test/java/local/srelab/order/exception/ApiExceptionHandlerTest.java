package local.srelab.order.exception;

import java.net.SocketTimeoutException;
import org.junit.jupiter.api.Test;
import org.springframework.web.client.RestClientException;
import static org.junit.jupiter.api.Assertions.*;

class ApiExceptionHandlerTest {
 @Test void responseExtractionTimeoutReturnsGatewayTimeout(){
  var response=new ApiExceptionHandler().handle(new RestClientException("response extraction failed",new SocketTimeoutException("Read timed out")));
  assertEquals(504,response.getStatusCode().value());
  assertEquals("DEPENDENCY_TIMEOUT",response.getBody().get("code"));
 }
 @Test void otherExtractionFailuresAreNotTimeouts(){
  var response=new ApiExceptionHandler().handle(new RestClientException("invalid response",new IllegalArgumentException("invalid data")));
  assertEquals(503,response.getStatusCode().value());
 }
}
