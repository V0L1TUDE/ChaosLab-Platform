#!/usr/bin/env python3
"""
Verify the ChaosLab tenant isolation on a real cluster (operator tool; run before enabling deployments).

    cd chaoslab
    CHAOSLAB_TENANT_KUBECONFIG=/etc/chaoslab/tenant.kubeconfig CHAOSLAB_TENANT_RUNTIME_CLASS=gvisor \
        python3 deploy/tenant/verify_isolation.py [--reader-kubeconfig ~/.kube/admin.conf] [--keep]

Writes go through the TENANT credentials (the same ones the web app uses); pod logs are read with the reader
kubeconfig (default: your current kubectl context), because the deployer is deliberately not allowed to read logs.
It creates two throw-away sandbox namespaces (cl-app-900000001 / -2) with exactly the objects the web app would
create (k8s_deploy.platform_objects / workload_objects), checks, and deletes them again.

Checks, each PASS/FAIL:
  1. web-app preflight (identity, RuntimeClass, tenant nodes, admission policies)
  2. admission canary denied by the ChaosLab policy (privileged/root/hostNetwork/hostPath dry run)
  3. deployer scope: the tenant credentials cannot write outside ChaosLab namespaces
  4. a compliant workload runs, with the sandbox RuntimeClass, on a tenant node, without a token
  5. sandbox kernel: `uname -r` inside the pod differs from the node's kernel (gVisor / Kata run their own)
  6. quota: a container above the LimitRange maximum is rejected
  7. NetworkPolicy: same-namespace traffic works (control), cross-namespace and internet egress are blocked
  8. cleanup: both namespaces are gone
On success it prints the attestation line for /etc/chaoslab/chaoslab.env.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))          # chaoslab/ (for `webapp`)

from webapp import config, k8s_deploy, tenant_isolation  # noqa: E402

import yaml  # noqa: E402

IDS = (900000001, 900000002)
SERVER_IMAGE = "docker.io/nginxinc/nginx-unprivileged:1.27-alpine"     # listens on 8080, runs as uid 101
CLIENT_IMAGE = "docker.io/library/busybox:1.36"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def reader(args, reader_kc, timeout=30):
    cmd = ["kubectl"] + (["--kubeconfig", reader_kc] if reader_kc else []) + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()


def reader_apply(doc, reader_kc):
    cmd = ["kubectl"] + (["--kubeconfig", reader_kc] if reader_kc else []) + ["apply", "--dry-run=server", "-f", "-"]
    r = subprocess.run(cmd, input=yaml.safe_dump(doc), capture_output=True, text=True, timeout=30)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()


def deployment(name, image, command, labels, port=None):
    c = {"name": "c", "image": image, "securityContext": {"runAsUser": 65534 if image == CLIENT_IMAGE else 101}}
    if command:
        c["command"] = command
    if port:
        c["ports"] = [{"containerPort": port}]
        c["readinessProbe"] = {"tcpSocket": {"port": port}, "periodSeconds": 2}    # Ready = really listening
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "labels": {}},
            "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                     "template": {"metadata": {"labels": labels}, "spec": {"containers": [c]}}}}


def wait_pod(ns, app, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        ok, out = k8s_deploy.kubectl(["get", "pods", "-n", ns, "-l", f"app={app}", "-o", "json"])
        if ok:
            items = json.loads(out).get("items", [])
            for p in items:
                st = p.get("status", {})
                if st.get("phase") == "Running" and all(c.get("ready") for c in st.get("containerStatuses", [])):
                    return p
        time.sleep(3)
    return None


PROBE_SECONDS = 40
SETTLE_SECONDS = 10


def parse_samples(out):
    """'T=12 PEER=R NET=B' lines -> [(12, 'R', 'B'), ...]"""
    res = []
    for line in out.splitlines():
        parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
        if {"T", "PEER", "NET"} <= set(parts) and parts["T"].isdigit():
            res.append((int(parts["T"]), parts["PEER"], parts["NET"]))
    return res


def summary(samples):
    return " ".join(f"t{t}:{p}{n}" for t, p, n in samples[-6:]) + "  (peer/net, R=reachable B=blocked)"


def logs(ns, pod, reader_kc, needle, timeout=90):
    end = time.time() + timeout
    out = ""
    while time.time() < end:
        ok, out = reader(["logs", "-n", ns, pod], reader_kc)
        if ok and needle in out:
            return out
        time.sleep(3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reader-kubeconfig", default=None, help="kubeconfig allowed to read pod logs (default: current)")
    ap.add_argument("--keep", action="store_true", help="do not delete the test namespaces (debugging)")
    a = ap.parse_args()
    if not config.TENANT_KUBECONFIG or not config.TENANT_RUNTIME_CLASS:
        print("Set CHAOSLAB_TENANT_KUBECONFIG and CHAOSLAB_TENANT_RUNTIME_CLASS (as in /etc/chaoslab/chaoslab.env).")
        return 2
    ns_a, ns_b = (k8s_deploy.namespace_for(i) for i in IDS)
    print(f"ChaosLab tenant isolation check (policy {tenant_isolation.POLICY_VERSION})")

    # 1. the web app's own cluster checks (the deploy switch and the attestation are what this script decides)
    pre = []
    tenant_isolation._identity_checks(pre)
    tenant_isolation._runtime_checks(pre)
    _, tenant_nodes = tenant_isolation._node_checks(pre)
    tenant_isolation._admission_checks(pre)
    failed = [c["detail"] for c in pre if not c["ok"]]
    check("preflight (identity, RuntimeClass, tenant nodes, admission policies)", not failed, "; ".join(failed[:3]))

    try:
        for app_id in IDS:
            ok, out = k8s_deploy.kubectl(["apply", "-f", "-"], timeout=60,
                                         input_text=yaml.safe_dump_all(k8s_deploy.platform_objects(app_id, 0)))
            check(f"namespace {k8s_deploy.namespace_for(app_id)} with guard rails", ok, out[:160] if not ok else "")

        ok, msg = k8s_deploy.verify_admission(ns_a)
        check("admission canary denied by the ChaosLab policy", ok, msg if not ok else "")

        ok, out = k8s_deploy.kubectl(["create", "configmap", "chaoslab-scope-probe", "-n", "default", "--dry-run=server"])
        check("deployer cannot write outside ChaosLab namespaces", not ok, "write to 'default' was ACCEPTED" if ok else "")
        ok, out = k8s_deploy.kubectl(["create", "namespace", "not-chaoslab", "--dry-run=server"])
        check("deployer cannot create arbitrary namespaces", not ok, "ACCEPTED" if ok else "")

        server = deployment("web", SERVER_IMAGE, None, {"app": "web"}, port=8080)
        ok, out = k8s_deploy.kubectl(["apply", "-f", "-"], timeout=90,
                                     input_text=yaml.safe_dump_all(k8s_deploy.workload_objects(IDS[1], 0, [server])))
        check("compliant workload accepted", ok, out[:200] if not ok else "")
        pod = wait_pod(ns_b, "web")
        check("workload Ready", bool(pod))
        if pod:
            spec = pod["spec"]
            check("pod uses the sandbox RuntimeClass", spec.get("runtimeClassName") == config.TENANT_RUNTIME_CLASS)
            check("pod runs on a tenant node", spec.get("nodeName") in tenant_nodes, spec.get("nodeName", ""))
            check("no service-account token mounted", spec.get("automountServiceAccountToken") is False
                  and not any("kube-api-access" in (v.get("name") or "") for v in spec.get("volumes") or []))
            server_ip = pod["status"].get("podIP")
            node = spec.get("nodeName")
            ok, node_json = reader(["get", "node", node, "-o", "json"], a.reader_kubeconfig)
            host_kernel = json.loads(node_json)["status"]["nodeInfo"]["kernelVersion"] if ok else "?"

            # Probe every 2 s for PROBE_SECONDS: the settled state (last SETTLE_SECONDS) is what is judged, and the
            # start-up window (how long a NEW pod could still reach things it must not) is measured and reported.
            probe = ("echo KERNEL=$(uname -r); s=$(date +%s); "
                     f"while [ $(( $(date +%s) - s )) -lt {PROBE_SECONDS} ]; do t=$(( $(date +%s) - s )); "
                     f"if wget -q -T 2 -O /dev/null http://{server_ip}:8080/ 2>/dev/null; then p=R; else p=B; fi; "
                     "if wget -q -T 2 -O /dev/null http://1.1.1.1/ 2>/dev/null; then n=R; else n=B; fi; "
                     "echo \"T=$t PEER=$p NET=$n\"; sleep 2; done; echo DONE; sleep 3600")
            for app_id, ns, label in ((IDS[1], ns_b, "same-namespace"), (IDS[0], ns_a, "cross-namespace")):
                client = deployment("probe", CLIENT_IMAGE, ["sh", "-c", probe], {"app": "probe"})
                k8s_deploy.kubectl(["apply", "-f", "-"], timeout=90,
                                   input_text=yaml.safe_dump_all(k8s_deploy.workload_objects(app_id, 0, [client])))
                cpod = wait_pod(ns, "probe")
                out = logs(ns, cpod["metadata"]["name"], a.reader_kubeconfig, "DONE",
                           timeout=PROBE_SECONDS + 60) if cpod else ""
                samples = parse_samples(out)
                settled = [x for x in samples if x[0] >= PROBE_SECONDS - SETTLE_SECONDS]
                if label == "same-namespace":
                    kernel = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("KERNEL=")), "?")
                    check("sandbox kernel (pod kernel differs from the node's)", kernel not in ("?", host_kernel),
                          f"pod {kernel} / node {host_kernel}")
                    check("same-namespace traffic allowed (control)", bool(settled) and all(x[1] == "R" for x in settled),
                          summary(samples))
                else:
                    check("cross-namespace traffic blocked by NetworkPolicy",
                          bool(settled) and all(x[1] == "B" for x in settled), summary(samples))
                check(f"internet egress blocked ({label})", bool(settled) and all(x[2] == "B" for x in settled),
                      summary(samples))
                leaks = [x[0] for x in samples if x[2] == "R" or (label == "cross-namespace" and x[1] == "R")]
                print(f"  INFO  start-up window ({label}): "
                      + (f"forbidden traffic still possible up to t={max(leaks)}s after the pod started "
                         "(NetworkPolicy not yet programmed)" if leaks else "no forbidden traffic observed"))

        big = deployment("too-big", SERVER_IMAGE, None, {"app": "too-big"})
        big["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"cpu": "2", "memory": "4Gi"}}
        # LimitRange applies to pods, so dry-run a pod with the hardened template (as the reader: needs pods/create)
        pod_doc = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "too-big", "namespace": ns_a},
                   "spec": k8s_deploy.workload_objects(IDS[0], 0, [big])[0]["spec"]["template"]["spec"]}
        ok, out = reader_apply(pod_doc, a.reader_kubeconfig)
        check("LimitRange rejects a container above the sandbox maximum", not ok and "maximum" in out.lower(), out[:160])
    finally:
        if not a.keep:
            for app_id in IDS:
                k8s_deploy.undeploy(app_id)
            end = time.time() + 180
            while time.time() < end:
                gone = all("NotFound" in k8s_deploy.kubectl(["get", "namespace", k8s_deploy.namespace_for(i)])[1]
                           for i in IDS)
                if gone:
                    break
                time.sleep(3)
            check("cleanup: test namespaces deleted", gone)

    if all(results):
        print("\nALL CHECKS PASSED. Add to /etc/chaoslab/chaoslab.env (then restart chaoslab):")
        print(f"CHAOSLAB_TENANT_ISOLATION_VERIFIED={tenant_isolation.POLICY_VERSION}")
        return 0
    print(f"\n{results.count(False)} CHECK(S) FAILED: keep CHAOSLAB_DEPLOY_ENABLED=false.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
