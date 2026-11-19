#!/usr/bin/env python3
"""
ChaosLab backup: a consistent copy of the SQLite database (online backup API, safe while the app runs) plus the
generated reports, compressed into one archive. Kept locally (last 7) and uploaded to
s3://$CHAOSLAB_BACKUP_BUCKET/backups/ when a bucket is configured (credentials from the instance's IAM role).

Restore: stop the app, extract the archive, put chaoslab.db and reports/ back into the data directory, start the app.
"""

import glob
import os
import sqlite3
import sys
import tarfile
import tempfile
import time

DATA_DIR = os.environ.get("CHAOSLAB_DATA_DIR", "/var/lib/chaoslab")
BUCKET = os.environ.get("CHAOSLAB_BACKUP_BUCKET", "").strip()
REGION = os.environ.get("CHAOSLAB_S3_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None
KEEP = 7


def main() -> int:
    db_path = os.path.join(DATA_DIR, "chaoslab.db")
    if not os.path.isfile(db_path):
        print(f"No database at {db_path}; nothing to back up.")
        return 0
    out_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    archive = os.path.join(out_dir, f"chaoslab-{stamp}.tar.gz")

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, "chaoslab.db")
        src, dst = sqlite3.connect(db_path), sqlite3.connect(snapshot)
        with dst:
            src.backup(dst)                       # consistent snapshot even while the app is writing
        src.close(); dst.close()
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="chaoslab.db")
            reports = os.path.join(DATA_DIR, "reports")
            if os.path.isdir(reports):
                tar.add(reports, arcname="reports")
    os.chmod(archive, 0o640)
    print(f"Wrote {archive} ({os.path.getsize(archive)} bytes)")

    for old in sorted(glob.glob(os.path.join(out_dir, "chaoslab-*.tar.gz")))[:-KEEP]:
        os.remove(old)

    if BUCKET:
        import boto3
        key = f"backups/{os.path.basename(archive)}"
        boto3.client("s3", region_name=REGION).upload_file(
            archive, BUCKET, key, ExtraArgs={"ServerSideEncryption": "AES256"})
        print(f"Uploaded to s3://{BUCKET}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
