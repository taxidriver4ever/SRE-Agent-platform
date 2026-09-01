import { createApp } from "vue";
// 页面状态保留在 App.vue，可复用的目录数据与服务拓扑拆分到独立模块。
import App from "./App.vue";
// SRE Console 的登录、服务目录、详情与 Incident 视图共用一套样式。
import "./minimal.css";

createApp(App).mount("#app");
