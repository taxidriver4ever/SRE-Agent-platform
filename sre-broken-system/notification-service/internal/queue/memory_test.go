package queue
import("testing";"local/sre-lab/notification-service/internal/domain")
// Bounded queue must reject overflow instead of growing memory without limit.
func TestQueueRejectsOverflow(t *testing.T){queue:=New(1);if err:=queue.Enqueue(domain.Notification{ID:"1"});err!=nil{t.Fatal(err)};if err:=queue.Enqueue(domain.Notification{ID:"2"});err==nil{t.Fatal("expected full queue error")}}
