<script setup>
import { computed } from "vue";
import { getService, graphEdges, graphNodes, statusLabel } from "../data/services";

const props = defineProps({
  focusService: { type: String, default: "" },
  involvedServices: { type: Array, default: () => [] },
  rootCauseService: { type: String, default: "" },
  graph: { type: Object, default: () => ({ nodes: [], edges: [] }) },
  compact: { type: Boolean, default: false },
});

const involved = computed(() => new Set(props.involvedServices));
const displayNodes = computed(() => {
  if (!props.graph?.nodes?.length) return graphNodes.map((node) => ({ ...node, name: node.id, type: "SERVICE", graphStatus: "" }));
  const nodes = props.graph.nodes;
  const edges = props.graph.edges || [];
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  edges.forEach((edge) => indegree.set(edge.target, (indegree.get(edge.target) || 0) + 1));
  const depth = new Map();
  const queue = nodes.filter((node) => !indegree.get(node.id)).map((node) => [node.id, 0]);
  while (queue.length) {
    const [id, level] = queue.shift();
    if ((depth.get(id) ?? -1) >= level) continue;
    depth.set(id, level);
    edges.filter((edge) => edge.source === id).forEach((edge) => queue.push([edge.target, level + 1]));
  }
  nodes.forEach((node) => { if (!depth.has(node.id)) depth.set(node.id, 0); });
  const rows = new Map();
  nodes.forEach((node) => {
    const level = depth.get(node.id);
    if (!rows.has(level)) rows.set(level, []);
    rows.get(level).push(node);
  });
  return nodes.map((node) => {
    const level = depth.get(node.id);
    const row = rows.get(level);
    const index = row.findIndex((item) => item.id === node.id);
    const gap = 714 / (row.length + 1);
    return { ...node, x: Math.round(gap * (index + 1) - 70), y: 18 + level * 88, graphStatus: node.status };
  });
});
const displayEdges = computed(() => props.graph?.edges?.length
  ? props.graph.edges.map((edge) => ({ ...edge, key: edge.id || `${edge.source}-${edge.target}-${edge.relation}` }))
  : graphEdges.map(([source, target]) => ({ source, target, relation: "DEPENDS_ON", key: `${source}-${target}` })));
const nodeMap = computed(() => new Map(displayNodes.value.map((node) => [node.id, node])));
const graphHeight = computed(() => Math.max(348, ...displayNodes.value.map((node) => node.y + 70)));

function edgePath(edge) {
  const source = nodeMap.value.get(edge.source);
  const target = nodeMap.value.get(edge.target);
  if (!source || !target) return "";
  const startX = source.x + 70;
  const startY = source.y + 42;
  const endX = target.x + 70;
  const endY = target.y;
  const middleY = startY + (endY - startY) / 2;
  return `M ${startX} ${startY} C ${startX} ${middleY}, ${endX} ${middleY}, ${endX} ${endY}`;
}

function nodeClass(node) {
  const nodeName = node.name || node.id.replace(/^[^:]+:/, "");
  return {
    focused: nodeName === props.focusService,
    involved: involved.value.has(nodeName),
    root: nodeName === props.rootCauseService || node.graphStatus === "ROOT_CAUSE",
    critical: node.graphStatus === "ROOT_CAUSE",
    warning: node.graphStatus === "AFFECTED",
    healthy: node.graphStatus === "HEALTHY",
    muted: involved.value.size > 0 && !involved.value.has(nodeName),
  };
}

function nodeStatus(node) {
  if (node.graphStatus) return { ROOT_CAUSE: "Root Cause", AFFECTED: "Affected", HEALTHY: "Healthy", UNKNOWN: "Unknown" }[node.graphStatus] || node.graphStatus;
  return statusLabel(getService(node.name || node.id).status);
}
</script>

<template>
  <div class="service-graph" :class="{ compact }">
    <svg :viewBox="`0 0 714 ${graphHeight}`" role="img" aria-label="服务依赖拓扑图">
      <defs>
        <marker id="graph-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
          <path d="M0,0 L7,3.5 L0,7 Z" />
        </marker>
      </defs>
      <path
        v-for="edge in displayEdges"
        :key="edge.key"
        class="graph-edge"
        :class="{ active: involved.has(nodeMap.get(edge.source)?.name) && involved.has(nodeMap.get(edge.target)?.name) }"
        :d="edgePath(edge)"
      />
      <g
        v-for="node in displayNodes"
        :key="node.id"
        class="graph-node"
        :class="nodeClass(node)"
        :transform="`translate(${node.x} ${node.y})`"
      >
        <rect width="140" height="42" rx="8" />
        <circle cx="13" cy="13" r="4" />
        <text x="24" y="16">{{ node.name || node.id }}</text>
        <text class="node-status" x="13" y="32">{{ nodeStatus(node) }} · {{ node.type }}</text>
      </g>
    </svg>
    <div class="graph-legend">
      <span><i class="healthy"></i>Healthy</span>
      <span><i class="warning"></i>Warning</span>
      <span><i class="critical"></i>Critical</span>
      <span v-if="rootCauseService"><i class="root"></i>疑似根因</span>
    </div>
  </div>
</template>
