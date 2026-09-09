import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ServiceGraph from "../src/components/ServiceGraph.vue";

describe("ServiceGraph", () => {
  it("marks involved, affected and root-cause services without hiding dependencies", () => {
    const wrapper = mount(ServiceGraph, {
      props: {
        involvedServices: ["order-service", "payment-service"],
        rootCauseService: "payment-service",
        graph: {
          nodes: [
            { id: "service:order", name: "order-service", type: "SERVICE", status: "AFFECTED" },
            { id: "service:payment", name: "payment-service", type: "SERVICE", status: "ROOT_CAUSE" },
            { id: "service:db", name: "payment-db", type: "DATABASE", status: "UNKNOWN" },
          ],
          edges: [
            { id: "e1", source: "service:order", target: "service:payment", relation: "HTTP" },
            { id: "e2", source: "service:payment", target: "service:db", relation: "SQL" },
          ],
        },
      },
    });

    const nodes = wrapper.findAll(".graph-node");
    expect(nodes).toHaveLength(3);
    expect(nodes[0].classes()).toEqual(expect.arrayContaining(["involved", "warning"]));
    expect(nodes[1].classes()).toEqual(expect.arrayContaining(["involved", "root", "critical"]));
    expect(nodes[2].classes()).toContain("muted");
    expect(wrapper.findAll(".graph-edge")).toHaveLength(2);
    expect(wrapper.text()).toContain("Root Cause · SERVICE");
  });

  it("falls back to the catalog graph when the API graph is empty", () => {
    const wrapper = mount(ServiceGraph, { props: { graph: { nodes: [], edges: [] } } });
    expect(wrapper.findAll(".graph-node").length).toBeGreaterThan(1);
    expect(wrapper.get("svg").attributes("aria-label")).toBe("服务依赖拓扑图");
  });
});
