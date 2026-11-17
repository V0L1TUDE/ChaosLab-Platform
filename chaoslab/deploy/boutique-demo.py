#!/usr/bin/env python3
"""Adapt Online Boutique's release manifest for the small always-on demo cluster (used by demo-cluster.sh).

    python3 boutique-demo.py release/kubernetes-manifests.yaml NAMESPACE > out.yaml

* everything goes into NAMESPACE (not `default`, where Jaeger runs), so the agent's rights never cover Jaeger;
* no load generator (the agent's tracing check sends its own light traffic) and no LoadBalancer Service
  (k3s runs without ServiceLB, and the shop is not exposed publicly);
* tracing on, to the shared OpenTelemetry collector in `default`;
* small CPU requests: the defaults reserve ~1.3 cores, which would fill a 2-vCPU instance on their own.
  Limits are unchanged, so services can still burst.
"""

import sys

import yaml

CPU_REQUEST = "25m"
COLLECTOR = "opentelemetrycollector.default.svc.cluster.local:4317"


def adapt(docs, namespace):
    out = []
    for d in docs:
        if not d:
            continue
        name, kind = d["metadata"]["name"], d["kind"]
        if name == "loadgenerator" or (kind == "Service" and d.get("spec", {}).get("type") == "LoadBalancer"):
            continue
        d["metadata"]["namespace"] = namespace
        if kind == "Deployment":
            for c in d["spec"]["template"]["spec"].get("containers", []):
                c.setdefault("resources", {}).setdefault("requests", {})["cpu"] = CPU_REQUEST
                if c["name"] == "server":
                    env = [e for e in c.get("env", []) if e["name"] not in ("ENABLE_TRACING", "COLLECTOR_SERVICE_ADDR")]
                    c["env"] = env + [{"name": "ENABLE_TRACING", "value": "1"},
                                      {"name": "COLLECTOR_SERVICE_ADDR", "value": COLLECTOR}]
        out.append(d)
    return out


if __name__ == "__main__":
    src, ns = sys.argv[1], sys.argv[2]
    with open(src) as f:
        yaml.safe_dump_all(adapt(yaml.safe_load_all(f), ns), sys.stdout, sort_keys=False)
