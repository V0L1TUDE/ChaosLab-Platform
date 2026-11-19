# Running uploaded workloads safely: the ChaosLab tenant cluster

ChaosLab can deploy Kubernetes manifests uploaded by **untrusted users** and run chaos experiments against them.
Namespaces, quotas and NetworkPolicies keep tenants apart from each other, but every container on a node shares
that node's **kernel**: one kernel exploit escapes all of them. Uploaded workloads therefore run only behind a
stronger boundary, and the web app **refuses to deploy** until that boundary is configured and verified.

**Default: OFF.** `CHAOSLAB_DEPLOY_ENABLED` defaults to `false` and the public EC2 deployment sets it to `false`.
Do not enable it until every step below passes.

## Target architecture

```
 ChaosLab web server (EC2)                     Tenant cluster (dedicated; NOT the demo cluster, NOT the web host)
 ─────────────────────────                     ───────────────────────────────────────────────────────────────────
 CHAOSLAB_TENANT_KUBECONFIG ── token of ──▶    ServiceAccount chaoslab-system/chaoslab-deployer (least privilege)
   (the only cluster credential                 │
    the server process has)                     ├─ control-plane nodes ........ never run tenant pods
                                                ├─ tenant node pool ........... label + taint chaoslab.io/tenant-pool=true
                                                │    containerd + gVisor (runsc) or Kata Containers
                                                ├─ RuntimeClass gvisor ........ handler runsc, schedules only onto the pool
                                                ├─ ValidatingAdmissionPolicies  chaoslab-tenant-workloads (Deny)
                                                │                               chaoslab-deployer-scope   (Deny)
                                                ├─ CNI that ENFORCES NetworkPolicy (Calico, Cilium, k3s kube-router)
                                                └─ Chaos Mesh (for Live experiments on deployed sandbox apps)
```

Per application the web app creates `cl-app-<id>` with Pod Security `restricted`, a ResourceQuota, a LimitRange,
a default-deny NetworkPolicy (same namespace + cluster DNS only), and the sanitized workloads, hardened at render
time whatever the upload said.

## What is enforced, and where

| Control | manifests.py (upload) | k8s_deploy (render) | Admission (cluster) | Checked at deploy |
| --- | --- | --- | --- | --- |
| Only Deployment / Service (ClusterIP) / ConfigMap; minimal field allow-list | ✔ | ✔ (kind re-check) | | |
| Sandboxed runtime (gVisor / Kata) | (field forbidden) | `runtimeClassName` | `chaoslab-tenant-workloads` | preflight + pod placement |
| Dedicated tenant nodes, never the control plane | (field forbidden) | nodeSelector + toleration | policy | preflight (nodes) + pod placement |
| Non-root (`runAsNonRoot`, no uid 0) | ✔ | forced | policy + PSA restricted | canary |
| seccomp `RuntimeDefault` | (field forbidden) | forced | policy + PSA restricted | canary |
| No privilege escalation, not privileged | ✔ | forced | policy + PSA restricted | canary |
| Drop ALL capabilities, add none | (field forbidden) | forced | policy + PSA restricted | canary |
| No host network / PID / IPC, hostPath, hostPort | ✔ | stripped | policy + PSA restricted | canary |
| No service-account token | ✔ | forced | policy | pod placement |
| CPU / memory limits | ✔ (max 1 CPU / 1 Gi) | always set | policy + LimitRange | |
| Approved registries, pinned tag or digest, always pulled | ✔ | fully qualified, `Always` | policy (prefix match) | |
| Platform labels / names / kinds cannot be overridden | ✔ (reserved domains + names) | platform labels last | `chaoslab-deployer-scope` | |
| Deployer confined to `cl-app-*` managed namespaces | | | `chaoslab-deployer-scope` | preflight (`auth can-i`) |
| NetworkPolicy really enforced; sandbox kernel really used | | | | `verify_isolation.py` (operator attestation) |

Admission is **proved live on every deploy**: before any workload is applied, a deliberately non-compliant
Deployment (root, privileged, host network, hostPath, no sandbox runtime) is sent as a server-side dry run and **must
be rejected by ChaosLab's policy**; otherwise the namespace is deleted and the deploy fails. After the pods are
Ready, every pod must show the sandbox RuntimeClass, a tenant node and no token, or everything is torn down.

## Operator steps (in order)

Requirements: Kubernetes **1.30+** (ValidatingAdmissionPolicy GA), a CNI that enforces NetworkPolicy, and at
least one node that can run gVisor or Kata (for Kata: nested virtualization or bare metal).

1. **Create a dedicated tenant cluster** (recommended) or at minimum a dedicated node pool in a cluster that runs
   nothing else of value. Do **not** use the EC2 demo cluster or the web server host: its single node is the control
   plane and runs the platform.

2. **Install the sandboxed runtime on the tenant nodes.** gVisor on containerd:
   install `runsc` and `containerd-shim-runsc-v1` (https://gvisor.dev/docs/user_guide/install/), add to the containerd
   config `[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc] runtime_type = "io.containerd.runsc.v1"`
   (k3s: `/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl`), restart containerd/k3s-agent.
   Kata: install kata-deploy and use its handler (e.g. `kata-qemu`).

3. **Label and taint the tenant nodes** (and only those):

   ```bash
   kubectl label node <node> chaoslab.io/tenant-pool=true
   kubectl taint node <node> chaoslab.io/tenant-pool=true:NoSchedule
   ```

4. **Install Chaos Mesh** (only needed for Live experiments on deployed sandbox apps).

5. **Render and apply the RuntimeClass and admission policies from the web server's own configuration** (the policy
   carries a digest of the runtime class, node label and registry list; the web app refuses policies rendered for a
   different configuration). On the web server, with the same environment as the service:

   ```bash
   cd /opt/chaoslab/chaoslab
   sudo -u chaoslab env $(grep -v '^#' /etc/chaoslab/chaoslab.env | xargs) \
       python3 -m webapp.tenant_isolation render-runtimeclass runsc > /tmp/runtimeclass.yaml   # or: kata-qemu
   sudo -u chaoslab env $(grep -v '^#' /etc/chaoslab/chaoslab.env | xargs) \
       python3 -m webapp.tenant_isolation render-policy > /tmp/admission-policies.yaml
   ```

   Then, with a cluster-admin kubeconfig **on your workstation** (never on the web server):

   ```bash
   kubectl apply -f /tmp/runtimeclass.yaml
   kubectl apply -f /tmp/admission-policies.yaml
   kubectl apply -f chaoslab/deploy/tenant/deployer-rbac.yaml
   ```

   (`admission-policies.yaml` / `runtimeclass-gvisor.yaml` in this directory are the renderings for the default
   configuration, kept for review; a test keeps them in sync with the renderer.)

6. **Create the tenant kubeconfig** for the deployer ServiceAccount and copy it to the web server:

   ```bash
   SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
   CA=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
   TOKEN=$(kubectl -n chaoslab-system get secret chaoslab-deployer-token -o jsonpath='{.data.token}' | base64 -d)
   cat > tenant.kubeconfig <<EOF
   apiVersion: v1
   kind: Config
   clusters: [{name: tenant, cluster: {server: $SERVER, certificate-authority-data: $CA}}]
   users: [{name: chaoslab-deployer, user: {token: $TOKEN}}]
   contexts: [{name: tenant, context: {cluster: tenant, user: chaoslab-deployer}}]
   current-context: tenant
   EOF
   scp tenant.kubeconfig ubuntu@<web-server>:/tmp/ && rm tenant.kubeconfig
   # on the web server:
   sudo install -o root -g chaoslab -m 0640 /tmp/tenant.kubeconfig /etc/chaoslab/tenant.kubeconfig && rm /tmp/tenant.kubeconfig
   ```

   The web server must reach the tenant API server (security group / firewall: the server's IP only).

7. **Configure the web server** in `/etc/chaoslab/chaoslab.env` (keep `CHAOSLAB_DEPLOY_ENABLED=false` for now):

   ```
   CHAOSLAB_TENANT_KUBECONFIG=/etc/chaoslab/tenant.kubeconfig
   CHAOSLAB_TENANT_RUNTIME_CLASS=gvisor
   CHAOSLAB_TENANT_NODE_LABEL=chaoslab.io/tenant-pool=true
   ```

8. **Run the live verification** on the web server (it uses the tenant credentials for writes; pass an admin
   kubeconfig only for reading pod logs and node kernel versions):

   ```bash
   cd /opt/chaoslab/chaoslab
   sudo -u chaoslab env $(grep -v '^#' /etc/chaoslab/chaoslab.env | xargs) \
       python3 deploy/tenant/verify_isolation.py --reader-kubeconfig /path/to/reader.kubeconfig
   ```

   It checks the preflight, admission denial, deployer scope, a compliant workload on the sandbox runtime on a
   tenant node without a token, a **different kernel** inside the pod than on the node, the LimitRange, same-
   namespace traffic (control), **blocked cross-namespace and internet traffic**, and cleanup. Only if every check
   passes does it print `CHAOSLAB_TENANT_ISOLATION_VERIFIED=<policy version>`.

9. **Attest and enable**: add that line and `CHAOSLAB_DEPLOY_ENABLED=true` to `/etc/chaoslab/chaoslab.env`, restart
   (`sudo systemctl restart chaoslab`) and confirm the preflight from the server:

   ```bash
   sudo -u chaoslab env $(grep -v '^#' /etc/chaoslab/chaoslab.env | xargs) python3 -m webapp.tenant_isolation preflight
   ```

10. **Keep it true.** Re-run steps 5, 8 and 9 whenever the runtime class, node label, registry list or ChaosLab's
    `POLICY_VERSION` changes (the attestation and the policy digest then stop matching and deploys stop by
    themselves). Rotate the deployer token by re-creating `chaoslab-deployer-token`.

## What the web app checks before every deploy (`python -m webapp.tenant_isolation preflight`)

* `CHAOSLAB_DEPLOY_ENABLED=true`, the tenant kubeconfig exists, a runtime class and node label are configured, and
  the attestation matches the current policy version;
* the credentials can **not**: do everything, create (Cluster)RoleBindings, read Secrets, exec into pods, modify
  nodes, change admission policies or bindings, create RuntimeClasses; and **can** create/delete namespaces and
  create Deployments, NetworkPolicies, ResourceQuotas;
* the RuntimeClass exists, its handler is gVisor/Kata, and it schedules onto the tenant pool;
* at least one Ready tenant node; none is a control-plane node; every one carries the taint;
* both policies exist with the digest of this server's configuration and `failurePolicy: Fail`, and both bindings
  use `validationActions: [Deny]`.

If any check fails, or cannot be performed, the deploy is refused with that reason (HTTP 503) and nothing is
created. A namespace created by a deploy that later fails a check is deleted again.

## Verified on a real cluster (2026-11-18)

Two-node test cluster on AWS: k3s v1.31.14 (built-in kube-router NetworkPolicy), control plane t3.small (tainted,
runs no workloads), tenant node c7i-flex.large with gVisor `runsc` release-20261116.0, Ubuntu 24.04 (kernel
7.0.0-1017-aws). The policies were applied from this directory unchanged; the API server's type checker reported no
warnings for either policy. `verify_isolation.py`: **all checks passed** (sandbox kernel inside the pod:
`4.19.0-gvisor`). The web app's own `k8s_deploy.deploy()` deployed `webapp/examples/hello-two-tier.yaml` in 6 s
(both pods on gVisor, on the tenant node, non-root, all capabilities dropped) and `undeploy()` removed the namespace.

Findings from that run:
* **NetworkPolicy start-up window.** With kube-router, a pod's rules are programmed shortly after it starts: in the
  test, forbidden traffic (internet, other namespace) still got through in the first sample (t=0 s) and was blocked
  from the next one (t≈2 s) on. `verify_isolation.py` measures and prints this window. For production prefer a CNI
  that enforces policy before the pod's network is up (Calico, Cilium).
* `kubectl auth can-i` prints a `Warning: ... not namespace scoped` line for cluster-scoped resources; the preflight
  now ignores it (it previously failed closed on a correctly configured cluster).

## Known limits

* `verify_isolation.py` is the only place NetworkPolicy enforcement and the sandbox kernel are observed; the web
  app relies on the operator attestation for those two between runs.
* Registry allow-listing is by registry, not by image: any public image on an approved registry can run (inside the
  sandbox, without credentials or a token, with egress blocked). Narrow `CHAOSLAB_ALLOWED_REGISTRIES` (e.g. to your
  own registry) for stricter control, then re-render the policy.
* gVisor reduces, but does not eliminate, kernel attack surface; Kata gives a VM boundary. Keep the tenant nodes
  free of secrets and patched, and the tenant cluster separate from anything valuable.
* Images are pulled with `imagePullPolicy: Always`, so Docker Hub rate limits apply to the tenant nodes.
