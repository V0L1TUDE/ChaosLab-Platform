"""Print local-only demo manifests using current repository application source."""
from pathlib import Path
import sys
import yaml


def prepare_manifests():
    demo = Path(__file__).resolve().parent
    repository = demo.parents[2]
    manifests = []
    for service in ("user", "project"):
        source = repository / "independent-apps" / "project-management" / "services" / f"{service}-service"
        manifests.append({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"chaoslab-{service}-source", "namespace": "chaoslab-demo"},
            "data": {name: (source / name).read_text(encoding="utf-8")
                     for name in ("main.py", "telemetry_helper.py", "requirements.txt")},
        })
    manifests.extend(yaml.safe_load_all((demo / "project-demo-workloads.yaml").read_text(encoding="utf-8")))
    return manifests


if __name__ == "__main__":
    sys.stdout.write(yaml.safe_dump_all(prepare_manifests(), sort_keys=False))
