"""Offline ChaosLab safety checks — never accesses a Kubernetes cluster."""
import copy
import contextlib
import io
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


    def test_report_tracks_the_replica_that_actually_disappeared(self):
        def pod(name):
            return {"name": name, "ready": "1/1", "status": "Running"}
        first, second, replacement = pod("first"), pod("second"), pod("replacement")
        for removed, survivor in ((first, second), (second, first)):
            with self.subTest(removed=removed["name"]), contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(engine, "get_pods", side_effect=[
                    [first, second], [survivor, replacement], [survivor, replacement]]))
                stack.enter_context(patch.object(engine, "get_ready_pods", side_effect=[
                    [first, second], [survivor, replacement], [survivor, replacement]]))
                stack.enter_context(patch.object(engine, "apply_chaos", return_value=(True, "test", "ok", "PodChaos")))
                cleanup = stack.enter_context(patch.object(engine, "cleanup_chaos"))
                stack.enter_context(patch.object(engine, "get_chaos_mesh_evidence", return_value={}))
                stack.enter_context(patch.object(engine, "get_kubernetes_events", return_value=[]))
                stack.enter_context(patch.object(engine, "probe_application", return_value={"enabled": False}))
                stack.enter_context(patch.object(engine.time, "sleep"))
                save = stack.enter_context(patch.object(engine.ExperimentReporter, "save_all", return_value={
                    "html": "report.html", "markdown": "report.md", "json": "report.json"}))
                engine.run_experiment({**self.valid, "recovery": {"expected_ready_replicas": 2, "timeout_seconds": 60}})
                evidence = save.call_args.args[1]
                self.assertEqual(evidence["original_pod"], removed["name"])
                self.assertEqual(evidence["impacted_pods"], [removed["name"]])
                self.assertEqual(evidence["baseline"]["pod_name"], "first")
                self.assertTrue(evidence["replacement_ready"])
                cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
