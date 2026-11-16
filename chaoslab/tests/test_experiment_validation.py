"""Offline ChaosLab safety checks — never accesses a Kubernetes cluster."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

VERIFIER_DIR = Path(__file__).resolve().parents[1] / "verifier"
sys.path.insert(0, str(VERIFIER_DIR))
import engine  # noqa: E402

EXPERIMENT_DIR = Path(__file__).resolve().parents[1] / "experiments"


class ExperimentValidationTests(unittest.TestCase):
    def setUp(self):
        self.valid = {
            "name": "safe-test",
            "target": {"namespace": "default", "label": {"app": "paymentservice"}},
            "fault": {"type": "pod-kill", "mode": "one"},
            "recovery": {"timeout_seconds": 60},
        }

    def invalid(self, update):
        config = copy.deepcopy(self.valid)
        update(config)
        with self.assertRaises(ValueError):
            engine.validate_experiment_config(config)

    def test_all_nine_existing_experiments_are_accepted(self):
        configs = list(EXPERIMENT_DIR.glob("*.yaml"))
        self.assertEqual(len(configs), 9)
        for path in configs:
            with self.subTest(file=path.name):
                config = engine.load_experiment_config(str(path))
                engine.generate_chaos_manifest(config)

    def test_empty_selector_rejected(self):
        self.invalid(lambda c: c["target"].update(label={}))

    def test_unsupported_fault_rejected(self):
        self.invalid(lambda c: c["fault"].update(type="pod-kil"))

    def test_protected_namespace_rejected(self):
        self.invalid(lambda c: c["target"].update(namespace="kube-system"))

    def test_broad_mode_rejected(self):
        self.invalid(lambda c: c["fault"].update(mode="all"))

    def test_invalid_timeout_rejected(self):
        for value in (0, -1, 301, True, "60"):
            with self.subTest(timeout=value):
                self.invalid(lambda c: c["recovery"].update(timeout_seconds=value))

    def test_invalid_duration_rejected(self):
        for value in ("0s", "301s", "6m", "forever"):
            with self.subTest(duration=value):
                self.invalid(lambda c: c["fault"].update(duration=value))

    def test_no_kubectl_called_for_invalid_config(self):
        config = copy.deepcopy(self.valid)
        config["target"]["label"] = {}
        with patch.object(engine, "run_command", side_effect=AssertionError("kubectl called")):
            with self.assertRaises(ValueError):
                engine.run_experiment(config)
            with self.assertRaises(ValueError):
                engine.apply_chaos(config)

    def test_valid_manifest_retains_original_fault(self):
        name, kind, manifest = engine.generate_chaos_manifest(self.valid)
        self.assertEqual(kind, "PodChaos")
        self.assertEqual(manifest["spec"]["action"], "pod-kill")
        self.assertEqual(manifest["spec"]["selector"]["labelSelectors"], {"app": "paymentservice"})


if __name__ == "__main__":
    unittest.main()
