#!/usr/bin/env bash
# Always-on demo cluster for ChaosLab on an Ubuntu EC2 (can share the instance with the website).
#
#   sudo ./chaoslab/deploy/demo-cluster.sh                 # install everything; the agent waits for a token
#   sudo ./chaoslab/deploy/demo-cluster.sh --token cle_..  # also connect the agent (token from Clusters -> Connect)
#
# Installs: k3s (Traefik/ServiceLB disabled, so it never touches ports 80/443), Chaos Mesh (via k3s' built-in Helm
# controller), Jaeger + the trace collector address, three demo apps (project-platform and learning-platform built
# locally from this repo, Online Boutique from its public release images), and the ChaosLab agent as a systemd service running as a least-privilege Kubernetes service account.
# Safe to re-run (updates in place).
#
# Options
#   --token TOKEN     one-time enrollment token from the website (Clusters -> Connect a cluster)
#   --server URL      ChaosLab URL for the agent (default: http://127.0.0.1:8090 when the website runs on this host)
set -euo pipefail

K3S_VERSION="v1.30.6+k3s1"
CHAOS_MESH_VERSION="2.7.2"
TARGET_NAMESPACES=(project-platform learning-platform online-boutique)   # the agent's allow-list, nothing wider
TRACING_NS="default"
TOKEN="" SERVER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)  TOKEN="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0 $*" >&2; exit 1; }
[[ -z "$TOKEN" || "$TOKEN" =~ ^cle_[A-Za-z0-9_-]{10,100}$ ]] || { echo "That does not look like an enrollment token (cle_...)." >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_SRC="$REPO_DIR/independent-apps/project-management"
LMS_SRC="$REPO_DIR/independent-apps/learning-management"
NS_LIST="$(IFS=,; echo "${TARGET_NAMESPACES[*]}")"
AGENT_ETC=/etc/chaoslab-agent
AGENT_STATE=/var/lib/chaoslab-agent
AGENT_CODE=/opt/chaoslab-agent
KUBECTL="/usr/local/bin/k3s kubectl"
step() { printf '\n==> %s\n' "$*"; }

if [[ -z "$SERVER" ]]; then
  if systemctl is-active --quiet chaoslab 2>/dev/null; then SERVER="http://127.0.0.1:8090"
  else echo "Pass --server https://<your ChaosLab address> (no ChaosLab website found on this host)." >&2; exit 2; fi
fi
[[ "$SERVER" =~ ^https?://[A-Za-z0-9.:-]+$ ]] || { echo "Invalid --server: $SERVER" >&2; exit 2; }

# ------------------------------------------------------------------------------------------------ k3s
if ! command -v k3s >/dev/null; then
  step "Installing k3s $K3S_VERSION (no Traefik/ServiceLB: ports 80/443 stay with Caddy)"
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" \
    INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --write-kubeconfig-mode 600" sh -
fi
step "Waiting for the Kubernetes node"
for _ in $(seq 1 60); do $KUBECTL get nodes 2>/dev/null | grep -q ' Ready' && break; sleep 3; done
$KUBECTL get nodes

# ------------------------------------------------------------------------------------------------ Chaos Mesh
step "Installing Chaos Mesh $CHAOS_MESH_VERSION (1 controller, no dashboard, k3s containerd)"
$KUBECTL apply -f - <<EOF
apiVersion: helm.cattle.io/v1
kind: HelmChart
metadata:
  name: chaos-mesh
  namespace: kube-system
spec:
  repo: https://charts.chaos-mesh.org
  chart: chaos-mesh
  version: "$CHAOS_MESH_VERSION"
  targetNamespace: chaos-mesh
  createNamespace: true
  valuesContent: |-
    chaosDaemon:
      runtime: containerd
      socketPath: /run/k3s/containerd/containerd.sock
    controllerManager:
      replicaCount: 1
    dashboard:
      create: false
    dnsServer:
      create: false
EOF
# The chart's CRDs appear before its namespace and workloads: wait for the actual controller objects.
for _ in $(seq 1 100); do
  $KUBECTL get crd podchaos.chaos-mesh.org networkchaos.chaos-mesh.org >/dev/null 2>&1     && $KUBECTL -n chaos-mesh get deploy/chaos-controller-manager ds/chaos-daemon >/dev/null 2>&1 && break
  sleep 3
done
$KUBECTL -n chaos-mesh rollout status deploy/chaos-controller-manager --timeout=300s
$KUBECTL -n chaos-mesh rollout status ds/chaos-daemon --timeout=300s

# ------------------------------------------------------------------------------------------------ demo app images
step "Building the project-platform and learning-platform images on this host"
command -v docker >/dev/null || { apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io; }
python3 -c 'import yaml' 2>/dev/null || apt-get install -y -q python3-yaml >/dev/null
for svc in user-service project-service task-service; do
  docker build -q -t "pulse-$svc:latest" "$APP_SRC/services/$svc" >/dev/null
done
docker build -q -t pulse-frontend:latest "$APP_SRC/frontend" >/dev/null
for svc in student-service course-service enrollment-service; do
  docker build -q -t "lms-$svc:latest" "$LMS_SRC/services/$svc" >/dev/null
done
docker build -q -t lms-frontend:latest "$LMS_SRC/frontend" >/dev/null
docker save pulse-user-service:latest pulse-project-service:latest pulse-task-service:latest pulse-frontend:latest \
  lms-student-service:latest lms-course-service:latest lms-enrollment-service:latest lms-frontend:latest \
  | /usr/local/bin/k3s ctr -n k8s.io images import - >/dev/null
echo "images imported into k3s (Online Boutique uses its public release images)"

# ------------------------------------------------------------------------------------------------ tracing + apps
step "Deploying Jaeger and the demo apps"
$KUBECTL apply -f "$REPO_DIR/chaoslab/k8s/jaeger.yaml"
$KUBECTL apply -f "$APP_SRC/k8s/namespace.yaml" -f "$APP_SRC/k8s/deployments.yaml"
$KUBECTL apply -f "$LMS_SRC/k8s/namespace.yaml" -f "$LMS_SRC/k8s/deployments.yaml"
$KUBECTL create namespace online-boutique --dry-run=client -o yaml | $KUBECTL apply -f -
python3 "$REPO_DIR/chaoslab/deploy/boutique-demo.py" "$REPO_DIR/release/kubernetes-manifests.yaml" online-boutique \
  | $KUBECTL apply -f -
$KUBECTL -n "$TRACING_NS" rollout status deploy/jaeger --timeout=300s
for ns in project-platform learning-platform; do                  # locally built images: pick them up on re-runs
  for d in $($KUBECTL -n "$ns" get deploy -o name); do $KUBECTL -n "$ns" rollout restart "$d" >/dev/null; done
done
for ns in "${TARGET_NAMESPACES[@]}"; do
  for d in $($KUBECTL -n "$ns" get deploy -o name); do $KUBECTL -n "$ns" rollout status "$d" --timeout=420s; done
done

# ------------------------------------------------------------------------------------------------ agent identity (least privilege)
step "Creating the agent's restricted service account"
for ns in "${TARGET_NAMESPACES[@]}"; do                           # one Role + RoleBinding per namespace
  sed -e "s/TARGET_NAMESPACE/$ns/g" -e "s/TRACING_NAMESPACE/$TRACING_NS/g" "$REPO_DIR/chaoslab/agent/agent-rbac.yaml" | $KUBECTL apply -f -
done
$KUBECTL apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: chaoslab-agent-token
  namespace: chaoslab-agent
  annotations:
    kubernetes.io/service-account.name: chaoslab-agent
type: kubernetes.io/service-account-token
EOF
for _ in $(seq 1 30); do
  SA_TOKEN=$($KUBECTL -n chaoslab-agent get secret chaoslab-agent-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
  [[ -n "$SA_TOKEN" ]] && break; sleep 1
done
[[ -n "$SA_TOKEN" ]] || { echo "service-account token was not issued" >&2; exit 1; }

id chaoslab-agent >/dev/null 2>&1 || useradd --system --home-dir "$AGENT_STATE" --shell /usr/sbin/nologin chaoslab-agent
mkdir -p "$AGENT_ETC" "$AGENT_STATE"
$KUBECTL -n chaoslab-agent get secret chaoslab-agent-token -o jsonpath='{.data.ca\.crt}' | base64 -d > "$AGENT_ETC/ca.crt"
cat > "$AGENT_ETC/kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: demo
  cluster:
    server: https://127.0.0.1:6443
    certificate-authority: $AGENT_ETC/ca.crt
users:
- name: chaoslab-agent
  user:
    token: $SA_TOKEN
contexts:
- name: demo
  context: {cluster: demo, user: chaoslab-agent}
current-context: demo
EOF
chown -R chaoslab-agent:chaoslab-agent "$AGENT_ETC" "$AGENT_STATE"
chmod 700 "$AGENT_STATE" && chmod 600 "$AGENT_ETC/kubeconfig"
can_i() { sudo -u chaoslab-agent env KUBECONFIG="$AGENT_ETC/kubeconfig" /usr/local/bin/kubectl auth can-i "$@" 2>/dev/null || true; }
for ns in "${TARGET_NAMESPACES[@]}"; do
  [[ "$(can_i create podchaos.chaos-mesh.org -n "$ns")" == yes ]] || { echo "agent identity check failed for $ns" >&2; exit 1; }
done
[[ "$(can_i create podchaos.chaos-mesh.org -n "$TRACING_NS")" == no ]] || { echo "agent must not have fault rights in $TRACING_NS" >&2; exit 1; }
echo "agent can run faults in $NS_LIST only (verified)"

# ------------------------------------------------------------------------------------------------ agent code + service
# The agent has its own code copy (agent + verifier only), refreshed on EVERY run so `git pull` + re-run updates it.
# It is independent of the website's /opt/chaoslab, which setup.sh manages.
python3 -c 'import yaml' 2>/dev/null && command -v rsync >/dev/null || apt-get install -y -q python3 python3-yaml rsync >/dev/null
mkdir -p "$AGENT_CODE"
for d in agent verifier; do
  rsync -a --delete --exclude '__pycache__' --exclude 'reports' "$REPO_DIR/chaoslab/$d/" "$AGENT_CODE/$d/"
done
chown -R root:root "$AGENT_CODE" && chmod -R go-w "$AGENT_CODE"
echo "agent code: $AGENT_CODE ($(grep -m1 '^VERSION' "$AGENT_CODE/agent/agent.py"))"
{
  echo "CHAOSLAB_SERVER=$SERVER"
  echo "CHAOSLAB_ALLOWED_NAMESPACES=$NS_LIST"
  echo "CHAOSLAB_JAEGER=$TRACING_NS/jaeger:16686"
  echo "CHAOSLAB_STATE_DIR=$AGENT_STATE"
  echo "KUBECONFIG=$AGENT_ETC/kubeconfig"
  echo "PYTHONDONTWRITEBYTECODE=1"
  [[ -n "$TOKEN" ]] && echo "CHAOSLAB_ENROLL_TOKEN=$TOKEN"
} > "$AGENT_ETC/agent.env"
chown root:chaoslab-agent "$AGENT_ETC/agent.env" && chmod 640 "$AGENT_ETC/agent.env"
install -m 644 "$REPO_DIR/chaoslab/deploy/chaoslab-agent.service" /etc/systemd/system/chaoslab-agent.service
systemctl daemon-reload

if [[ -n "$TOKEN" ]]; then
  step "Connecting the agent"
  rm -f "$AGENT_STATE/agent.json"
  systemctl enable chaoslab-agent >/dev/null 2>&1 && systemctl restart chaoslab-agent
  for _ in $(seq 1 30); do [[ -f "$AGENT_STATE/agent.json" ]] && break; sleep 1; done
  sed -i '/^CHAOSLAB_ENROLL_TOKEN=/d' "$AGENT_ETC/agent.env"          # the token is single-use: drop it
  if [[ -f "$AGENT_STATE/agent.json" ]]; then
    echo "agent enrolled"; journalctl -u chaoslab-agent -n 3 --no-pager | tail -2
  else
    journalctl -u chaoslab-agent -n 15 --no-pager; echo "Enrollment failed (see above)." >&2; exit 1
  fi
elif [[ -f "$AGENT_STATE/agent.json" ]]; then
  systemctl enable chaoslab-agent >/dev/null 2>&1 && systemctl restart chaoslab-agent; echo "agent restarted with its saved credential (new code loaded)"
fi

step "Done"
for ns in "${TARGET_NAMESPACES[@]}"; do echo "-- $ns"; $KUBECTL get pods -n "$ns" --no-headers | awk '{print "  ", $1, $2, $3}'; done
free -h | awk 'NR<=2'
if [[ ! -f "$AGENT_STATE/agent.json" ]]; then
  cat <<EOF

Next: on the website go to Clusters -> Connect a cluster, name it (e.g. ec2-demo), set namespaces to $NS_LIST,
copy ONLY the token (cle_...) from the command, then run:
  sudo ./chaoslab/deploy/demo-cluster.sh --token cle_YOUR_TOKEN
EOF
fi
