"""Print local-only demo manifests using current repository application source."""
from pathlib import Path
import sys
import argparse
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


def register_experiment(destination=None):
    """Opt-in installation; never overwrite an edited local experiment."""
    demo = Path(__file__).resolve().parent
    target_dir = Path(destination) if destination is not None else demo.parent / "data" / "experiments"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "local-project-create-readback.yaml"
    content = (demo / "project_transaction_experiment.yaml").read_bytes()
    if target.exists():
        if target.read_bytes() != content:
            raise ValueError("Local transaction experiment has edits; refusing to overwrite it")
        return target
    with target.open("xb") as stream:
        stream.write(content)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register-experiment", action="store_true", help="Also install the reviewed transaction config into local experiment discovery")
    args = parser.parse_args()
    if args.register_experiment:
        path = register_experiment()
        print(f"Registered local experiment: {path}", file=sys.stderr)
    sys.stdout.write(yaml.safe_dump_all(prepare_manifests(), sort_keys=False))
