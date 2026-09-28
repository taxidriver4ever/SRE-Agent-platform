import {NodeSDK} from "@opentelemetry/sdk-node";
import {HttpInstrumentation} from "@opentelemetry/instrumentation-http";
import {ExpressInstrumentation} from "@opentelemetry/instrumentation-express";
import {MySQL2Instrumentation} from "@opentelemetry/instrumentation-mysql2";
import {UndiciInstrumentation} from "@opentelemetry/instrumentation-undici";
const sdk=new NodeSDK({serviceName:"payment-service",instrumentations:[new HttpInstrumentation(),new ExpressInstrumentation(),new MySQL2Instrumentation(),new UndiciInstrumentation()]});
sdk.start();
await import("./server.js");
process.once("SIGTERM",()=>{void sdk.shutdown().finally(()=>process.exit(0));});
