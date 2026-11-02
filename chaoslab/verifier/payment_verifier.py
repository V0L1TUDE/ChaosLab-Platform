"""
ChaosLab Payment Verifier (Backward-Compatibility Shim)
=======================================================
Maintains 100% backward-compatible function signatures and imports
by aliasing and delegating to the Universal Verification Engine (engine.py).
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from engine import (
    DEFAULT_CONFIG_PATH,
    REPORTS_DIR,
    apply_chaos,
    generate_chaos_manifest,
    get_chaos_mesh_evidence,
    get_kubernetes_events,
    get_label_selector,
    get_pods,
    get_ready_pods,
    get_target_name,
    load_experiment_config,
    probe_application,
    run_command,
    run_experiment,
    verify_experiment
)

def verify_recovery():
    """Legacy backward-compatible entrypoint."""
    return verify_experiment()

if __name__ == "__main__":
    config_arg = sys.argv[1] if len(sys.argv) > 1 else None
    verify_experiment(config_arg)
