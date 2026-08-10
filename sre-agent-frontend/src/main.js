import { createApp } from "vue";
// Vue 入口保持极简，复杂诊断状态全部封装在 App.vue 中。
import App from "./App.vue";
// 问答页使用独立样式文件；旧鉴权页样式不再参与构建。
import "./minimal.css";

createApp(App).mount("#app");
