"""
Automated Test for ChaosLab Cluster Agent & Remote Execution
===========================================================
Validates:
- Agent one-time token enrollment handshake
- Outbound heartbeat telemetry reporting
- Remote experiment job queuing and polling
- Mock / live execution dispatch and status updates
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHAOSLAB_DIR = os.path.join(BASE_DIR, "chaoslab")
BACKEND_DIR = os.path.join(CHAOSLAB_DIR, "platform", "backend")
AGENT_DIR = os.path.join(CHAOSLAB_DIR, "platform", "agent")

for p in [BASE_DIR, CHAOSLAB_DIR, BACKEND_DIR, AGENT_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from auth import hash_password, create_jwt_token
from database import Database
from agent import ChaosLabAgent

class TestAgentExecution(unittest.TestCase):

    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        self.db = Database(self.temp_db_path)
        self.temp_creds_fd, self.temp_creds_path = tempfile.mkstemp(suffix=".json")

    def tearDown(self):
        os.close(self.temp_db_fd)
        os.close(self.temp_creds_fd)
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)
        if os.path.exists(self.temp_creds_path):
            os.unlink(self.temp_creds_path)

    def test_enrollment_and_job_execution_lifecycle(self):
        # 1. Setup Tenant and Cluster in DB
        user = self.db.create_user("dev@org.com", hash_password("pass123"), "Dev Lead")
        app = self.db.create_application(user["id"], "Online Boutique", target_namespace="default")
        cluster = self.db.create_cluster(user["id"], "KinD Lab", environment="development")
        token = self.db.create_enrollment_token(cluster["id"])

        # 2. Simulate Agent enrollment handshake directly with database layer
        consumed_id = self.db.validate_and_consume_enrollment_token(token)
        self.assertEqual(consumed_id, cluster["id"])

        # Agent JWT created by SaaS
        agent_jwt = create_jwt_token({"sub": f"agent-{consumed_id}", "cluster_id": consumed_id, "role": "agent"})
        with open(self.temp_creds_path, "w") as f:
            json.dump({"cluster_id": consumed_id, "agent_token": agent_jwt}, f)

        # 3. Simulate heartbeat
        telemetry = {"nodes": 1, "total_pods": 21, "chaos_mesh_healthy": True}
        self.db.update_cluster_heartbeat(cluster["id"], agent_version="1.0.0", cluster_info=telemetry)
        updated_cluster = self.db.get_cluster(user["id"], cluster["id"])
        self.assertEqual(updated_cluster["status"], "active")
        self.assertEqual(updated_cluster["cluster_info"]["total_pods"], 21)

        # 4. User launches experiment run
        run = self.db.create_experiment_run(user["id"], app["id"], cluster["id"], "payment-service-pod-kill")
        self.assertEqual(run["status"], "pending")

        # 5. Agent polls job
        job = self.db.poll_pending_job_for_cluster(cluster["id"])
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], run["id"])
        self.assertEqual(job["status"], "dispatched")

        # 6. Agent updates status to running
        self.db.update_experiment_status(run["id"], status="running")
        running_run = self.db.get_experiment_run(user["id"], run["id"])
        self.assertEqual(running_run["status"], "running")

        # 7. Agent finishes and stores results
        self.db.update_experiment_status(run["id"], status="completed", verdict="PASS", recovery_duration=12.17)
        report = self.db.store_report(
            run_id=run["id"],
            invariants_total=10,
            invariants_passed=10,
            invariants_failed=0,
            evidence_json={"target_impact": True, "recovery_time": 12.17, "verdict": "PASS"}
        )
        self.assertIsNotNone(report["id"])
        self.assertEqual(report["invariants_passed"], 10)

        # 8. Completed run verification
        final_run = self.db.get_experiment_run(user["id"], run["id"])
        self.assertEqual(final_run["status"], "completed")
        self.assertEqual(final_run["verdict"], "PASS")
        self.assertEqual(final_run["recovery_duration_seconds"], 12.17)

if __name__ == "__main__":
    unittest.main()
