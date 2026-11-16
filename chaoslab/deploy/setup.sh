#!/usr/bin/env bash
# ChaosLab one-shot server setup for Ubuntu 22.04/24.04 (e.g. an AWS EC2 instance).
#
#   sudo ./setup.sh --bucket my-chaoslab-uploads --region ap-south-1
#
# Safe to re-run: it updates the code and services but keeps your settings file and data.
#
# Options
#   --hostname NAME   public name for HTTPS. Default on EC2: <public-ip-with-dashes>.sslip.io (free, no domain needed)
#   --bucket NAME     S3 bucket for uploads + backups (uses the instance's IAM role; omit for local disk)
#   --region REGION   AWS region of the bucket
#   --tls MODE        auto (default: real certificate via Let's Encrypt) | internal (self-signed, for testing)
#   --no-systemd      only install files (for containers/testing); do not enable or start services
set -euo pipefail

HOSTNAME_ARG="" BUCKET="" REGION="" TLS_MODE="auto" USE_SYSTEMD=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_ARG="$2"; shift 2 ;;
    --bucket)   BUCKET="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    --tls)      TLS_MODE="$2"; shift 2 ;;
    --no-systemd) USE_SYSTEMD=0; shift ;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0 $*" >&2; exit 1; }
[[ "$TLS_MODE" == "auto" || "$TLS_MODE" == "internal" ]] || { echo "--tls must be auto or internal" >&2; exit 2; }
if [[ -n "$BUCKET" && ! "$BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then echo "Invalid bucket name: $BUCKET" >&2; exit 2; fi
if [[ -n "$REGION" && ! "$REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]]; then echo "Invalid region: $REGION" >&2; exit 2; fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # repository root (this file is chaoslab/deploy/setup.sh)
APP_DIR=/opt/chaoslab
DATA_DIR=/var/lib/chaoslab
ETC_DIR=/etc/chaoslab
ENV_FILE="$ETC_DIR/chaoslab.env"
step() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------------------------------------- hostname
if [[ -z "$HOSTNAME_ARG" ]]; then
  step "Detecting the public IP (EC2 instance metadata)"
  TOKEN=$(curl -fsS -m 3 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
  IP=$(curl -fsS -m 3 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4 || true)
  [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Could not detect a public IPv4. Pass --hostname." >&2; exit 1; }
  HOSTNAME_ARG="${IP//./-}.sslip.io"
fi
[[ "$HOSTNAME_ARG" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || { echo "Invalid hostname: $HOSTNAME_ARG" >&2; exit 2; }
echo "Hostname: $HOSTNAME_ARG"

# ---------------------------------------------------------------------------------------------- packages
step "Installing packages (Python, PyYAML, boto3, Caddy)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-yaml python3-boto3 rsync curl ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

# ---------------------------------------------------------------------------------------------- user + files
step "Installing ChaosLab into $APP_DIR"
id chaoslab >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin chaoslab
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR/public"
rsync -a --delete --exclude '__pycache__' --exclude 'webapp/data' --exclude 'reports' "$REPO_DIR/chaoslab/" "$APP_DIR/chaoslab/"
chown -R root:root "$APP_DIR" && chmod -R a+rX,go-w "$APP_DIR"     # code is read-only for the service user
chown chaoslab:chaoslab "$DATA_DIR" && chmod 750 "$DATA_DIR"

# ---------------------------------------------------------------------------------------------- settings
if [[ ! -f "$ENV_FILE" ]]; then
  step "Writing $ENV_FILE"
  STORAGE=local; [[ -n "$BUCKET" ]] && STORAGE=s3
  cat > "$ENV_FILE" <<EOF
# ChaosLab settings (sourced by systemd). Edit, then: sudo systemctl restart chaoslab
CHAOSLAB_HOST=127.0.0.1
CHAOSLAB_PORT=8090
CHAOSLAB_DATA_DIR=$DATA_DIR
CHAOSLAB_PUBLIC_URL=https://$HOSTNAME_ARG
CHAOSLAB_TRUST_PROXY=true
CHAOSLAB_COOKIE_SECURE=true
CHAOSLAB_TLS_MODE=$TLS_MODE

# This server has no Kubernetes cluster: users connect their own clusters through the agent.
CHAOSLAB_LIVE_ENABLED=false
CHAOSLAB_DEPLOY_ENABLED=false
CHAOSLAB_DISCOVERY_ENABLED=false
CHAOSLAB_SHOW_DEMO_APPS=false

# Uploads (credentials come from the EC2 instance's IAM role, never from this file)
CHAOSLAB_STORAGE=$STORAGE
CHAOSLAB_S3_BUCKET=$BUCKET
CHAOSLAB_S3_REGION=$REGION
AWS_DEFAULT_REGION=$REGION

# Nightly backups (database + reports) go to s3://<bucket>/backups/ when a bucket is set
CHAOSLAB_BACKUP_BUCKET=$BUCKET
PYTHONDONTWRITEBYTECODE=1
EOF
else
  echo "Keeping existing $ENV_FILE (delete it to regenerate)."
fi
chown root:chaoslab "$ENV_FILE" && chmod 640 "$ENV_FILE"
TLS_MODE=$(grep -E '^CHAOSLAB_TLS_MODE=' "$ENV_FILE" | cut -d= -f2)
HOSTNAME_ARG=$(grep -E '^CHAOSLAB_PUBLIC_URL=' "$ENV_FILE" | sed 's#.*https://##')

# ---------------------------------------------------------------------------------------------- Caddy (HTTPS)
step "Configuring Caddy for https://$HOSTNAME_ARG ($TLS_MODE certificate)"
if [[ "$TLS_MODE" == "internal" ]]; then TLS_LINE="tls internal"; else TLS_LINE=""; fi
cat > /etc/caddy/Caddyfile <<EOF
# Managed by chaoslab/deploy/setup.sh
$HOSTNAME_ARG {
	$TLS_LINE
	encode gzip
	request_body {
		max_size 12MB
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
	# Public CA certificate for self-signed deployments (agents pass it with --ca-cert). Not secret.
	handle /chaoslab-ca.crt {
		root * $ETC_DIR/public
		file_server
	}
	handle {
		# Caddy sets X-Forwarded-For/-Proto itself and ignores client-supplied ones.
		reverse_proxy 127.0.0.1:8090
	}
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

# ---------------------------------------------------------------------------------------------- services
step "Installing systemd services"
install -m 644 "$APP_DIR/chaoslab/deploy/chaoslab.service" /etc/systemd/system/chaoslab.service
install -m 644 "$APP_DIR/chaoslab/deploy/chaoslab-backup.service" /etc/systemd/system/chaoslab-backup.service
install -m 644 "$APP_DIR/chaoslab/deploy/chaoslab-backup.timer" /etc/systemd/system/chaoslab-backup.timer

if [[ $USE_SYSTEMD -eq 1 ]]; then
  systemctl daemon-reload
  systemctl enable --now chaoslab.service
  systemctl restart chaoslab.service
  systemctl enable --now chaoslab-backup.timer
  systemctl enable caddy && systemctl restart caddy

  step "Waiting for ChaosLab to answer"
  for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8090/ && break; sleep 1; done
  curl -fsS -o /dev/null http://127.0.0.1:8090/ || { journalctl -u chaoslab -n 40 --no-pager; exit 1; }

  if [[ "$TLS_MODE" == "internal" ]]; then
    for _ in $(seq 1 20); do
      ROOT=$(find /var/lib/caddy -path '*pki/authorities/local/root.crt' 2>/dev/null | head -1)
      [[ -n "$ROOT" ]] && break; sleep 1
    done
    [[ -n "${ROOT:-}" ]] && install -m 644 "$ROOT" "$ETC_DIR/public/chaoslab-ca.crt"
  fi
fi

step "Done"
cat <<EOF
  Open:      https://$HOSTNAME_ARG
  Settings:  $ENV_FILE   (then: sudo systemctl restart chaoslab)
  Logs:      sudo journalctl -u chaoslab -f     |  HTTPS: sudo journalctl -u caddy -f
  Backups:   sudo systemctl start chaoslab-backup   (runs nightly)
  Update:    git pull && sudo ./chaoslab/deploy/setup.sh
EOF
