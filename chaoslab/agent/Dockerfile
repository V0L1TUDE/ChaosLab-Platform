# ChaosLab agent image. Build from the REPOSITORY ROOT:
#   docker build -f chaoslab/agent/Dockerfile -t chaoslab-agent:1.0.0 .
FROM python:3.12-slim

ARG TARGETARCH=amd64
ARG KUBECTL_VERSION=v1.30.4

RUN pip install --no-cache-dir pyyaml==6.0.2 \
 && apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
 && echo "$(curl -fsSL https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod +x /usr/local/bin/kubectl \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY chaoslab/agent/agent.py chaoslab/agent/safe_probes.py /app/
COPY chaoslab/verifier /app/verifier

# Non-root. The credential lives in /state (mount a volume so it survives restarts).
RUN useradd --uid 10001 --create-home agent && mkdir /state && chown agent /state
USER 10001
ENV CHAOSLAB_STATE_DIR=/state CHAOSLAB_VERIFIER_DIR=/app/verifier PYTHONUNBUFFERED=1
VOLUME /state
ENTRYPOINT ["python", "/app/agent.py"]
