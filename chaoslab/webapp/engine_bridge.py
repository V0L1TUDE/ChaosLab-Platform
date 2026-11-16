"""
Bridge to the existing ChaosLab verification engine (chaoslab/verifier).
The engine modules use flat imports (`from oracle import ...`), so the verifier
directory is placed on sys.path once, lazily, and the modules are re-exported.
"""

import sys
from typing import Any

from . import config

_loaded = {}


def _ensure_path() -> None:
    if config.VERIFIER_DIR not in sys.path:
        sys.path.insert(0, config.VERIFIER_DIR)


def engine() -> Any:
    """The existing universal engine module (run_experiment, load_experiment_config, ...)."""
    if "engine" not in _loaded:
        _ensure_path()
        import engine as _engine  # noqa: WPS433 (verifier flat import)
        _loaded["engine"] = _engine
    return _loaded["engine"]


def oracle() -> Any:
    """The existing Verification Oracle module (VerificationOracle, RuleStatus, ...)."""
    if "oracle" not in _loaded:
        _ensure_path()
        import oracle as _oracle
        _loaded["oracle"] = _oracle
    return _loaded["oracle"]
