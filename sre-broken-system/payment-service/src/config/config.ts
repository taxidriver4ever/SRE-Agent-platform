/** Immutable process configuration shared by HTTP, telemetry and downstream clients. */
export interface Config { port:number; version:string; podName:string; notificationBaseUrl:string; }

/** Read environment variables once so requests cannot observe partially changed configuration. */
export const config:Config=Object.freeze({port:Number(process.env.PORT??8083),version:process.env.SERVICE_VERSION??"dev",podName:process.env.POD_NAME??"local",notificationBaseUrl:process.env.NOTIFICATION_BASE_URL??"http://notification-service:8084"});
