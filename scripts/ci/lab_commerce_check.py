"""Isolated commerce + Collector contract/fault suite. Never touches an existing Compose project.

Use --build for an explicit full-system validation; normal per-service CI stays differential.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import urllib.error
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ["order", "inventory", "user", "payment", "notification", "recommendation"]

def run(*args):
    return subprocess.check_output(list(args), text=True).strip()

def request(base, path, body=None, method=None, expected=200, traceparent=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base+path, data=data, method=method, headers={"content-type":"application/json","x-request-id":"commerce-contract"})
    if traceparent: req.add_header("traceparent", traceparent)
    try: response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as error: response = error
    raw = response.read().decode()
    assert response.status in (expected if isinstance(expected, tuple) else (expected,)), (path, response.status, expected, raw)
    if response.headers.get("Content-Type","").startswith("application/json"):
        return json.loads(raw)
    return raw

def main(build=False):
    project = "sre-commerce-test-"+uuid.uuid4().hex[:10]
    scratch=ROOT/".cache"/project
    scratch.mkdir(parents=True)
    config=yaml.safe_load((ROOT/"compose.yaml").read_text(encoding="utf-8"))
    selected={n:config["services"][n] for n in [*[s+"-service" for s in SERVICES],"lab-mysql","lab-gateway"]}
    gateway=yaml.safe_load((ROOT/"sre-broken-system/sre-lab-infra/gateway/envoy.yaml").read_text())
    gateway_file=scratch/"envoy.yaml"
    gateway_file.write_text(yaml.safe_dump(gateway),encoding="utf-8")
    for name,service in selected.items():
        service.pop("profiles",None)
        service.pop("logging",None)
        if name.endswith("-service"):
            short=name.removesuffix("-service")
            service["build"]=str(ROOT/"sre-broken-system"/name)
            service["image"]="sre-commerce-"+short+":local"
            service.setdefault("environment",{}).update(OTEL_SDK_DISABLED="false",SKYWALKING_AGENT_ENABLED="false")
            if short=="order":service["environment"]["ORDER_PROCESSING_DELAY_MS"]="1500"
            service["ports"]=[f"127.0.0.1::{8080+SERVICES.index(short)}"]
        elif name=="lab-mysql":
            service["environment"]["MYSQL_ROOT_PASSWORD"]="disposable-only"
            service["volumes"]=["db:/var/lib/mysql",str(ROOT/"sre-broken-system/sre-lab-infra/mysql/init")+":/docker-entrypoint-initdb.d:ro"]
            service.pop("ports",None)
        else:
            service["ports"]=["127.0.0.1::8080"]
            service["volumes"]=[str(gateway_file)+":/etc/envoy/envoy.yaml:ro"]
    collector_file=scratch/"collector.yaml"
    collector_file.write_text(yaml.safe_dump({"receivers":{"otlp":{"protocols":{"grpc":{"endpoint":"0.0.0.0:4317"},"http":{"endpoint":"0.0.0.0:4318"}}}},"processors":{"batch":{"timeout":"1s"}},"exporters":{"file":{"path":"/traces/traces.json"}},"service":{"pipelines":{"traces":{"receivers":["otlp"],"processors":["batch"],"exporters":["file"]}}}}),encoding="utf-8")
    selected["otel-collector"]={"user":"0:0","image":"otel/opentelemetry-collector-contrib:0.130.1","command":["--config=/etc/otel/config.yaml"],"volumes":[str(collector_file)+":/etc/otel/config.yaml:ro","trace-data:/traces"]}
    file=scratch/"compose.yaml"
    file.write_text(yaml.safe_dump({"services":selected,"volumes":{"db":{},"inventory-data":{},"trace-data":{}}}),encoding="utf-8")
    command=["docker","compose","-p",project,"-f",str(file)]
    def compose(*args):return run(*command,*args)
    try:
        if build:subprocess.run([*command,"build"],check=True)
        compose("up","-d")
        urls={s:"http://"+compose("port",s+"-service",str(8080+SERVICES.index(s))) for s in SERVICES}
        urls["gateway"]="http://"+compose("port","lab-gateway","8080")
        def restart(service):
            name="lab-gateway" if service=="gateway" else service+"-service"
            port="8080" if service=="gateway" else str(8080+SERVICES.index(service))
            compose("restart",name)
            urls[service]="http://"+compose("port",name,port)
            for _ in range(40):
                try:request(urls[service],"/api/users/1/status" if service=="gateway" else "/ready");return
                except (OSError,AssertionError):time.sleep(.5)
            raise RuntimeError(name+" failed restart readiness")
        for service,url in urls.items():
            path="/actuator/health/readiness" if service=="order" else "/api/users/1/status" if service=="gateway" else "/ready"
            for _ in range(120):
                try: request(url,path);break
                except (OSError,AssertionError):time.sleep(1)
            else:raise RuntimeError(service+" failed readiness")
        print("All services ready",flush=True)
        for name in SERVICES:
            path="/actuator/prometheus" if name=="order" else "/metrics"
            assert request(urls[name],path)
            assert request(urls[name],"/internal/faults")["fault_mode"]=="normal"
        for path in ("/internal/faults","/debug/fault","/api/payments","/api/inventory/SKU-1","/api/notifications","/api/orders-bypass","/api/orders/../internal/faults","/api/orders/%2e%2e/internal/faults"):
            request(urls["gateway"],path,expected=404)
        order={"userId":1,"customerEmail":"user1@example.com","items":[{"productId":1,"sku":"SKU-1","quantity":2,"unitPrice":12.25},{"productId":2,"sku":"SKU-2","quantity":1,"unitPrice":10}]}
        def create(expected=201):return request(urls["gateway"],"/api/orders",order,expected=expected)
        normal=request(urls["gateway"],"/api/orders",order,expected=201,traceparent="00-"+"1"*32+"-"+"2"*16+"-01");assert normal["status"]=="PAID" and len(normal["items"])==2
        time.sleep(10)
        collector=compose("ps","-q","otel-collector")
        trace_file=scratch/"traces.json"
        run("docker","cp",collector+":/traces/traces.json",str(trace_file))
        spans=[]
        for line in trace_file.read_text(encoding="utf-8").splitlines():
            for resource in json.loads(line).get("resourceSpans",[]):
                attrs={a["key"]:a["value"].get("stringValue") for a in resource["resource"].get("attributes",[])}
                for scope in resource.get("scopeSpans",[]):
                    for span in scope.get("spans",[]):
                        if span.get("traceId")=="1"*32:spans.append((attrs.get("service.name"),span))
        expected_services={"envoy-gateway","order-service","user-service","inventory-service","payment-service","notification-service"}
        assert expected_services <= {name for name,_ in spans}, ("missing connected traces",{name for name,_ in spans})
        ids={s["spanId"] for _,s in spans}
        assert all(s.get("parentSpanId") in ids|{"2"*16} for _,s in spans), "broken trace parents"
        print("Connected Envoy + five service trace verified",flush=True)
        stock=request(urls["inventory"],"/inventory/SKU-1");assert stock["available"]==99 and stock["reserved"]==0
        restart("inventory")
        assert request(urls["inventory"],"/inventory/SKU-1")["available"]==99
        payment={"order_id":normal["id"],"amount":34.5,"idempotency_key":"order-"+str(normal["id"])}
        paid=request(urls["payment"],"/payments",payment,expected=201);assert paid["status"]=="CAPTURED"
        restart("payment")
        assert request(urls["payment"],"/payments",payment,expected=201)["id"]==paid["id"]
        request(urls["payment"],"/payments",{**payment,"amount":35},expected=409)
        assert request(urls["gateway"],"/api/recommendations/1")
        def fault(service,name,delay=3000,duration=30):
            return request(urls[service],"/internal/faults",{"fault":name,"duration_seconds":duration,"parameters":{"delay_ms":delay,"error_rate":1}})
        def clear(service,name):request(urls[service],"/internal/faults/"+name,method="DELETE")
        fault("notification","random_error");assert create()["status"]=="PAID";clear("notification","random_error")
        fault("user","high_latency");assert create(504)["code"]=="DEPENDENCY_TIMEOUT";clear("user","high_latency")
        fault("inventory","high_cpu",1500);assert create(504)["code"]=="DEPENDENCY_TIMEOUT";clear("inventory","high_cpu");time.sleep(2)
        fault("payment","connection_pool_exhaustion",4000)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(request,urls["payment"],"/payments",{"order_id":900000+i,"amount":1,"idempotency_key":f"pool-{i}"},expected=(201,504)) for i in range(4)]
            time.sleep(.5);metrics=request(urls["payment"],"/metrics");assert "db_pool_active 3" in metrics and "db_pool_pending 1" in metrics
            # Any three requests may win the pool slots; exactly one must time out.
            results=[f.result() for f in futures]
            assert sum("id" in result for result in results)==3, results
            assert sum(result.get("code")=="PAYMENT_DB_POOL_TIMEOUT" for result in results)==1, results
        assert create(504)["code"]=="DEPENDENCY_TIMEOUT";clear("payment","connection_pool_exhaustion");time.sleep(4)
        fault("order","retry_amplification");fault("payment","random_500")
        create(502)
        metrics=request(urls["order"],"/actuator/prometheus");retry_line=next(line for line in metrics.splitlines() if line.startswith("sre_dependency_retries_total{"));assert float(retry_line.rsplit(" ",1)[1])==2
        clear("order","retry_amplification");clear("payment","random_500")
        fault("user","high_latency",10,1);time.sleep(1.2);assert request(urls["user"],"/internal/faults")["fault_mode"]=="normal"
        hcm=gateway["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]
        hcm["route_config"]["virtual_hosts"][0]["routes"][0]["route"]["timeout"]="1s"
        gateway_file.write_text(yaml.safe_dump(gateway),encoding="utf-8");restart("gateway")
        create(504);time.sleep(2)
        assert compose("exec","-T","lab-mysql","mysql","-uroot","-pdisposable-only","-N","-e","SELECT status FROM sre_lab.orders ORDER BY id DESC LIMIT 1")=="PAID"
        # Envoy and Docker flush access logs asynchronously after the response.
        for _ in range(30):
            gateway_logs=compose("logs","lab-gateway")
            if '"response_flags":"UT"' in gateway_logs:break
            time.sleep(.5)
        else:raise AssertionError("missing Envoy upstream timeout access log: "+gateway_logs[-3000:])
        final_faults={s:request(urls[s],"/internal/faults") for s in SERVICES}
        assert all(state["fault_mode"]=="normal" for state in final_faults.values()), final_faults
        print("PASS: commerce, persistence, contracts, private routes and all six fault scenarios",flush=True)
    except Exception:
        subprocess.run([*command,"logs","--tail","120"],check=False)
        raise
    finally:
        subprocess.run([*command,"down","--volumes","--remove-orphans"],check=True)

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--build",action="store_true")
    main(parser.parse_args().build)
