package local.srelab.order.service;
import org.junit.jupiter.api.Test;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
import java.util.*;import java.math.BigDecimal;import java.time.Instant;
import local.srelab.order.client.CommerceClients;import local.srelab.order.config.FaultState;
import local.srelab.order.domain.*;import local.srelab.order.repository.OrderRepository;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
class CommerceFlowTest {
 @Test void successfulOrderSurvivesNotificationFailure(){
  var repo=mock(OrderRepository.class);var clients=mock(CommerceClients.class);var faults=new FaultState();
  var items=List.of(new OrderItem(1,"SKU-1",2,new BigDecimal("12.25")),new OrderItem(2,"SKU-2",1,BigDecimal.ONE));
  var command=new CreateOrderCommand(1,"lab@example.com",items);
  when(clients.getUser(1)).thenReturn(Map.of("status","ACTIVE"));when(repo.create(eq(command),any())).thenReturn(99L);
  var pending=new Order(99,1,"lab@example.com","PAYMENT_PENDING",new BigDecimal("25.50"),Instant.now(),items);
  var paid=new Order(99,1,"lab@example.com","PAID",pending.totalAmount(),pending.createdAt(),items);
  when(repo.find(99)).thenReturn(pending,paid);when(clients.pay(99,"25.50")).thenReturn(Map.of("id","payment-1","status","AUTHORIZED"));
  doThrow(new RuntimeException("notification down")).when(clients).notifyCreated(99,1);
  var service=new OrderApplicationService(repo,clients,faults,new SimpleMeterRegistry());
  assertEquals("PAID",service.create(command).status());verify(clients).reserve(items.get(0),"order-99-SKU-1");verify(clients).reserve(items.get(1),"order-99-SKU-2");verify(clients).commit("order-99-SKU-1");verify(clients).commit("order-99-SKU-2");verify(repo).setStatus(99,"PAID");
 }
 @Test void retriesAreBoundedAndPaymentIntentPreserved(){
  var repo=mock(OrderRepository.class);var clients=mock(CommerceClients.class);var faults=new FaultState();faults.changeTo("retry_amplification");
  when(repo.find(7)).thenReturn(new Order(7,1,"lab@example.com","PAYMENT_PENDING",BigDecimal.ONE,Instant.now(),List.of()));
  when(clients.pay(7,"1")).thenThrow(new org.springframework.web.client.ResourceAccessException("timeout"));
  var service=new OrderApplicationService(repo,clients,faults,new SimpleMeterRegistry());assertThrows(org.springframework.web.client.ResourceAccessException.class,()->service.completePayment(7));
  verify(clients,times(3)).pay(7,"1");verify(clients,never()).release(anyString());verify(repo,never()).setStatus(anyLong(),eq("PAID"));
 }
}
