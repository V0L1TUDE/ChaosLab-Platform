"""
ChaosLab Online MVP - server entrypoint
=======================================
    cd chaoslab && python -m webapp [--host 127.0.0.1] [--port 8090]
"""

import argparse
import importlib
import os
import sys

from . import config, db
from .http_core import create_server

# Feature modules register their routes on import. Optional ones are added as they land.
ROUTE_MODULES = ["routes_auth", "routes_apps", "routes_experiments", "routes_reports", "routes_dashboard"]


def load_routes() -> None:
    for name in ROUTE_MODULES:
        try:
            importlib.import_module(f"{__package__}.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"{__package__}.{name}":
                raise


def run_startup_hooks() -> None:
    """Seed built-in data and recover interrupted experiments (module is optional early on)."""
    try:
        bootstrap = importlib.import_module(f"{__package__}.bootstrap")
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.bootstrap":
            raise
        return
    bootstrap.on_startup()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="ChaosLab Online MVP server")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    args = parser.parse_args(argv)

    db.init_db()
    load_routes()
    run_startup_hooks()

    httpd = create_server(args.host, args.port)
    print("=" * 78)
    print("CHAOSLAB ONLINE MVP")
    print("=" * 78)
    print(f"Serving at   : http://{args.host}:{args.port}")
    print(f"Database     : {config.DB_PATH}")
    if config.STORAGE_BACKEND == "s3":
        print(f"Uploads      : s3://{config.S3_BUCKET}/{config.S3_PREFIX} (region {config.S3_REGION or 'default'}, SSE {config.S3_SSE})")
    else:
        print(f"Uploads      : {config.UPLOAD_DIR} (local)")
    print(f"Reports      : {config.REPORT_DIR}")
    print(f"Live mode    : {'enabled' if config.LIVE_ENABLED else 'disabled (CHAOSLAB_LIVE_ENABLED=false)'}")
    print("Press Ctrl+C to stop.")
    print("=" * 78)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
