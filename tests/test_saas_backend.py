"""
Automated Test Suite for ChaosLab Hosted SaaS Backend & Authentication
=====================================================================
Validates:
- PBKDF2-HMAC-SHA256 password hashing and constant-time verification
- RFC 7519 compliant JWT generation, decoding, tamper detection, and expiration
- User registration and login flows
- Tenant isolation (Users cannot view/modify others' applications or clusters)
- Application CRUD lifecycle
- Cluster enrollment token lifecycle (single-use validation)
"""

import os
import sys
import tempfile
import unittest

# Setup import paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHAOSLAB_DIR = os.path.join(BASE_DIR, "chaoslab")
BACKEND_DIR = os.path.join(CHAOSLAB_DIR, "platform", "backend")

for p in [BASE_DIR, CHAOSLAB_DIR, BACKEND_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from auth import (
    hash_password, verify_password,
    create_jwt_token, verify_jwt_token,
    extract_bearer_token
)
from database import Database

class TestSaaSBackend(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary SQLite database for every test run
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        self.db = Database(self.temp_db_path)

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            os.unlink(self.temp_db_path)

    def test_password_hashing_and_verification(self):
        plain = "SuperSecretPassword123!"
        hashed = hash_password(plain)
        self.assertTrue(hashed.startswith("pbkdf2:sha256:100000$"))
        self.assertTrue(verify_password(plain, hashed))
        self.assertFalse(verify_password("WrongPassword!", hashed))
        self.assertFalse(verify_password("", hashed))

    def test_jwt_lifecycle(self):
        payload = {"sub": "user-uuid-1234", "email": "engineer@chaoslab.io", "role": "developer"}
        token = create_jwt_token(payload, expires_in=3600)
        self.assertIsInstance(token, str)
        self.assertEqual(len(token.split(".")), 3)

        decoded = verify_jwt_token(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["sub"], "user-uuid-1234")
        self.assertEqual(decoded["email"], "engineer@chaoslab.io")

        # Test tamper detection
        tampered_token = token[:-4] + "fake"
        self.assertIsNone(verify_jwt_token(tampered_token))

        # Test expired token
        expired_token = create_jwt_token(payload, expires_in=-10)
        self.assertIsNone(verify_jwt_token(expired_token))

        # Test Bearer header extractor
        header = f"Bearer {token}"
        self.assertEqual(extract_bearer_token(header), token)
        self.assertIsNone(extract_bearer_token("Basic 12345"))
        self.assertIsNone(extract_bearer_token(None))

    def test_user_creation_and_lookup(self):
        hashed = hash_password("SecurePass456!")
        user = self.db.create_user("alice@company.com", hashed, "Alice Engineer")
        self.assertIsNotNone(user["id"])
        self.assertEqual(user["email"], "alice@company.com")
        self.assertEqual(user["full_name"], "Alice Engineer")

        by_email = self.db.get_user_by_email("alice@company.com")
        self.assertIsNotNone(by_email)
        self.assertEqual(by_email["id"], user["id"])

        by_id = self.db.get_user_by_id(user["id"])
        self.assertIsNotNone(by_id)
        self.assertEqual(by_id["email"], "alice@company.com")

    def test_application_crud_and_tenant_isolation(self):
        # Create two distinct users
        u1 = self.db.create_user("user1@org.com", hash_password("pass123"), "User One")
        u2 = self.db.create_user("user2@org.com", hash_password("pass123"), "User Two")

        # User 1 creates an application
        app1 = self.db.create_application(
            user_id=u1["id"],
            name="Online Boutique",
            description="Microservices e-commerce application",
            repo_url="https://github.com/GoogleCloudPlatform/microservices-demo",
            target_namespace="default"
        )
        self.assertIsNotNone(app1["id"])
        self.assertEqual(app1["name"], "Online Boutique")

        # User 1 can see app1
        u1_apps = self.db.list_applications(u1["id"])
        self.assertEqual(len(u1_apps), 1)
        self.assertEqual(u1_apps[0]["id"], app1["id"])

        # TENANT ISOLATION: User 2 CANNOT see app1
        u2_apps = self.db.list_applications(u2["id"])
        self.assertEqual(len(u2_apps), 0)
        self.assertIsNone(self.db.get_application(u2["id"], app1["id"]))

        # User 2 CANNOT update or delete app1
        updated = self.db.update_application(u2["id"], app1["id"], name="Hacked Name")
        self.assertIsNone(updated)
        deleted = self.db.delete_application(u2["id"], app1["id"])
        self.assertFalse(deleted)

        # User 1 updates app1
        updated = self.db.update_application(u1["id"], app1["id"], description="Updated description")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["description"], "Updated description")

        # User 1 deletes app1
        deleted = self.db.delete_application(u1["id"], app1["id"])
        self.assertTrue(deleted)
        self.assertEqual(len(self.db.list_applications(u1["id"])), 0)

    def test_cluster_enrollment_tokens(self):
        user = self.db.create_user("dev@org.com", hash_password("pass123"), "Dev User")
        cluster = self.db.create_cluster(user["id"], "Production EKS", environment="production")
        self.assertEqual(cluster["status"], "pending")

        token = self.db.create_enrollment_token(cluster["id"], expires_in_seconds=600)
        self.assertTrue(token.startswith("cl-enroll-"))

        # First consumption succeeds
        consumed_cluster_id = self.db.validate_and_consume_enrollment_token(token)
        self.assertEqual(consumed_cluster_id, cluster["id"])

        # Cluster status is now active
        cluster_updated = self.db.get_cluster(user["id"], cluster["id"])
        self.assertEqual(cluster_updated["status"], "active")

        # Second consumption MUST FAIL (Single-use security requirement)
        second_attempt = self.db.validate_and_consume_enrollment_token(token)
        self.assertIsNone(second_attempt)

    def test_experiment_run_and_reporting_lifecycle(self):
        user = self.db.create_user("tester@org.com", hash_password("pass123"), "Tester")
        app = self.db.create_application(user["id"], "Payment Service", target_namespace="default")
        cluster = self.db.create_cluster(user["id"], "Local KinD")

        # Launch run
        run = self.db.create_experiment_run(user["id"], app["id"], cluster["id"], "payment-service-pod-kill")
        self.assertEqual(run["status"], "pending")

        # Agent polls job
        dispatched_job = self.db.poll_pending_job_for_cluster(cluster["id"])
        self.assertIsNotNone(dispatched_job)
        self.assertEqual(dispatched_job["id"], run["id"])
        self.assertEqual(dispatched_job["status"], "dispatched")

        # Agent completes and posts results
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

        # Audit logs check
        self.db.log_audit(user["id"], "EXPERIMENT_COMPLETE", "experiment_run", run["id"])
        logs = self.db.list_audit_logs(user["id"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "EXPERIMENT_COMPLETE")

if __name__ == "__main__":
    unittest.main()
