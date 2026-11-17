# Deploy ChaosLab on AWS EC2 (no domain needed)

What you get: ChaosLab at `https://<your-ip>.sslip.io` with a real HTTPS certificate, running as a service that
survives reboots and crashes, uploads in S3, nightly backups to S3. Users connect **their own** clusters with the
agent; the server itself needs no Kubernetes.

```
Browser (any laptop) ──HTTPS──▶ EC2: Caddy (HTTPS) ──▶ ChaosLab (127.0.0.1:8090) ──▶ S3 (uploads, backups)
                                       ▲
Agent next to any cluster ──HTTPS, outbound only─┘
```

`sslip.io` is a free public DNS service: `13-201-5-7.sslip.io` resolves to `13.201.5.7`. That gives the server a
hostname, so Caddy can get a free Let's Encrypt certificate without you buying a domain. You can switch to your own
domain later (see the end).

Time: about 30 minutes. Cost: a `t3.small` plus a public IPv4 is roughly US$20/month on demand (check the AWS pricing
page for your region); stop the instance when not in use to pay less.

---

## Part 1: AWS console (browser)

### 1. S3 bucket (skip if you already made one)
S3 → **Create bucket** → name e.g. `chaoslab-uploads-yourname`, your region, **Block all public access: ON**,
default encryption on → Create.

### 2. IAM role for the server (no access keys needed on the server)
IAM → **Policies → Create policy → JSON**, paste (replace the bucket name twice), name it `ChaosLabServer`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::chaoslab-uploads-yourname/uploads/*"},
    {"Effect": "Allow", "Action": ["s3:PutObject"],
     "Resource": "arn:aws:s3:::chaoslab-uploads-yourname/backups/*"},
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": "arn:aws:s3:::chaoslab-uploads-yourname"}
  ]
}
```
IAM → **Roles → Create role** → Trusted entity: **AWS service**, use case **EC2** → attach `ChaosLabServer` →
name it `chaoslab-server` → Create.

### 3. Launch the instance
EC2 → **Launch instance**:
* **Name**: `chaoslab`
* **AMI**: Ubuntu Server 24.04 LTS (64-bit x86)
* **Instance type**: `t3.small` (2 GB) is enough for the server; `t3.medium` for more headroom
* **Key pair**: create one (download the `.pem`; you need it to SSH in)
* **Network settings → Edit → Create security group** with these inbound rules:

  | Type | Port | Source | Why |
  | --- | --- | --- | --- |
  | SSH | 22 | **My IP** | your admin access only |
  | HTTP | 80 | Anywhere | certificate issuance + redirect to HTTPS |
  | HTTPS | 443 | Anywhere | the website and agents |

* **Storage**: 20 GB gp3
* **Advanced details → IAM instance profile**: `chaoslab-server`
* Launch.

### 4. Give it a fixed IP
EC2 → **Elastic IPs → Allocate** → select it → **Associate** with the `chaoslab` instance.
(Without this the IP changes when the instance stops, and so would your `sslip.io` address.)

---

## Part 2: on the server (one command)

From your laptop (PowerShell), with the key you downloaded:
```powershell
ssh -i C:\path\to\your-key.pem ubuntu@<ELASTIC-IP>
```
On the server:
```bash
git clone --branch feature/online-mvp https://github.com/V0L1TUDE/ChaosLab-Platform.git
cd ChaosLab-Platform
sudo ./chaoslab/deploy/setup.sh --bucket chaoslab-uploads-yourname --region ap-south-1
```
(Private repo? Use a GitHub personal access token as the password, or `scp` the folder up.)

The script installs Python, Caddy and the services, detects the public IP, writes `/etc/chaoslab/chaoslab.env`,
and ends with something like:
```
Open:      https://13-201-5-7.sslip.io
```
Open that URL from **any** laptop, register, and you are in. The first HTTPS request can take ~30 s while the
certificate is issued.

---

## Part 3: connect a cluster from another laptop
1. On that laptop: Kubernetes (Docker Desktop is fine) with Chaos Mesh and an app; `kubectl`, Python 3 + `pip install pyyaml`, and this repo's `chaoslab/agent` folder.
2. In the website: **Clusters → Connect a cluster**, copy the command. It now starts with
   `--server https://<ip>.sslip.io`.
3. Run it in `chaoslab/agent` on that laptop. The cluster turns **online**; register an application and run experiments.

---

## Part 4 (optional): an always-on demo cluster, so Live demos don't need a laptop
`demo-cluster.sh` turns the same instance (or a second one) into a small Kubernetes cluster with chaos tooling and the
demo app, plus a connected agent:

| Piece | Details |
| --- | --- |
| k3s | lightweight Kubernetes; its Traefik/ServiceLB are disabled so ports 80/443 stay with Caddy |
| Chaos Mesh | installed by k3s' Helm controller; 1 controller, no dashboard or DNS server |
| Jaeger | trace storage + the `opentelemetrycollector` address the apps send to |
| project-platform | user/project/task services + frontend, built from `independent-apps/` on the instance |
| ChaosLab agent | systemd service `chaoslab-agent`, Kubernetes service account limited by `agent-rbac.yaml`, tracing via `--jaeger default/jaeger:16686` |

Memory: about 2-3 GB on top of the website, so an 8 GB instance is comfortable. Nothing new is exposed to the internet
(the Kubernetes API listens on 6443, which the security group does not open).

```bash
cd ~/ChaosLab-Platform && git pull
sudo ./chaoslab/deploy/demo-cluster.sh                       # 5-10 min: installs everything, agent waits
```
Then on the website: **Clusters → Connect a cluster** (e.g. `ec2-demo`, namespace `project-platform`), copy only the
`cle_...` token from the command and run:
```bash
sudo ./chaoslab/deploy/demo-cluster.sh --token cle_YOUR_TOKEN
```
Register the application (namespace `project-platform`), add probes/tracing (same values as the laptop guide), and run
Live experiments any time. Agent logs: `sudo journalctl -u chaoslab-agent -f`. Cluster: `sudo k3s kubectl get pods -A`.
### Let every account use it (shared Live demo)
By default only you see the demo application. To let **any** signed-in user run Live experiments on it, find its id
(the number in the address bar, e.g. `#/apps/7`) and on the server:
```bash
echo 'CHAOSLAB_SHARED_APP_ID=7' | sudo tee -a /etc/chaoslab/chaoslab.env && sudo systemctl restart chaoslab
```
Other users then see it marked **SHARED DEMO** and can run experiments; their experiments and reports are private to
them. They cannot change its probes/tracing, delete it, or see/stop the cluster (only you can, from your account).
Fair use: one experiment at a time on the cluster, `CHAOSLAB_SHARED_LIVE_RUNS_PER_HOUR` (default 6) Live runs per user
per hour, faults up to `CHAOSLAB_SHARED_MAX_DURATION_SECONDS` (default 60). While shared, the app cannot be deleted;
remove the line and restart to stop sharing.

To update the agent after `git pull`, re-run `sudo ./chaoslab/deploy/demo-cluster.sh` (no token needed): it refreshes the
agent's own code copy in `/opt/chaoslab-agent` and restarts it with its saved credential. The agent log lists the
evidence each job collected; the Oracle verdict is computed by the server and shown on the website.

## Operating it
| Task | Command (on the server) |
| --- | --- |
| Status | `sudo systemctl status chaoslab caddy` |
| Logs | `sudo journalctl -u chaoslab -f` (app), `sudo journalctl -u caddy -f` (HTTPS) |
| Change settings | `sudo nano /etc/chaoslab/chaoslab.env` then `sudo systemctl restart chaoslab` |
| Update to new code | `cd ~/ChaosLab-Platform && git pull && sudo ./chaoslab/deploy/setup.sh` (keeps settings and data) |
| Backup now | `sudo systemctl start chaoslab-backup` → `/var/lib/chaoslab/backups/` and `s3://<bucket>/backups/` |
| Restore | `sudo systemctl stop chaoslab`, extract a backup, copy `chaoslab.db` and `reports/` into `/var/lib/chaoslab/`, `sudo chown -R chaoslab: /var/lib/chaoslab`, start it |

What the setup does for security: the app listens on localhost only behind Caddy (HTTPS, HSTS, 12 MB request cap);
it runs as an unprivileged `chaoslab` user with a read-only code directory and systemd sandboxing; cookies are
`Secure`; client IPs come from Caddy only; S3 access is via the instance role (no keys on disk); Live execution and
sandbox deploys are **off** on the server (there is no cluster there).

## Troubleshooting
* **Browser can't connect**: check the security group allows 80 and 443 from anywhere; `sudo systemctl status caddy`.
* **Certificate errors / `sslip.io` rate-limited**: Let's Encrypt occasionally limits shared hostnames. Use a
  self-signed certificate instead: `sudo sed -i 's/^CHAOSLAB_TLS_MODE=.*/CHAOSLAB_TLS_MODE=internal/' /etc/chaoslab/chaoslab.env && sudo ./chaoslab/deploy/setup.sh`.
  Browsers will warn once; agents download `https://<host>/chaoslab-ca.crt` and pass `--ca-cert chaoslab-ca.crt`
  (the website's Connect command includes it automatically).
* **Uploads fail with AccessDenied**: the instance role is missing or the bucket name in the policy is wrong
  (EC2 → instance → Actions → Security → Modify IAM role).
* **"Live execution is unavailable"** on the server: expected. Live runs happen through connected clusters.

## Later: your own domain
Point an `A` record (e.g. `chaoslab.example.com`) at the Elastic IP, then on the server:
`sudo rm /etc/chaoslab/chaoslab.env && sudo ./chaoslab/deploy/setup.sh --hostname chaoslab.example.com --bucket … --region …`.
Connected agents must then be re-pointed (their saved credential is tied to the old address): use **New enrollment token**.
