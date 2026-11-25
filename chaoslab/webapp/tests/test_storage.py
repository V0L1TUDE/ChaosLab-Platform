"""S3 storage backend: fake-client tests (always run) + moto/boto3 tests (run when boto3 & moto are installed)."""

import os
import unittest

from harness import Client


class FakeS3:
    """Records calls and mimics the bits of the boto3 S3 client that S3Storage uses."""

    def __init__(self):
        self.objects, self.calls = {}, []
        self.fail = None   # set to an exception to make the next calls fail

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        if self.fail:
            raise self.fail
        self.objects[(kw["Bucket"], kw["Key"])] = kw["Body"]

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete_object", {"Bucket": Bucket, "Key": Key}))
        if self.fail:
            raise self.fail
        self.objects.pop((Bucket, Key), None)

    def head_bucket(self, Bucket):
        if self.fail:
            raise self.fail


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def use_s3(client, **overrides):
    """Switch the running server to S3 storage with the given client; returns an undo function."""
    from webapp import config, storage
    saved = {k: getattr(config, k) for k in ("STORAGE_BACKEND", "S3_BUCKET", "S3_PREFIX", "S3_SSE", "S3_KMS_KEY_ID")}
    config.STORAGE_BACKEND, config.S3_BUCKET, config.S3_PREFIX = "s3", "test-bucket", "uploads/"
    config.S3_SSE, config.S3_KMS_KEY_ID = "AES256", ""
    for k, v in overrides.items():
        setattr(config, k, v)
    storage.reset_cache()
    storage._instances["s3"] = storage.S3Storage(client=client)

    def undo():
        for k, v in saved.items():
            setattr(config, k, v)
        storage.reset_cache()
    return undo


class S3WithFakeClientTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.undo = use_s3(self.s3)
        self.c = Client()
        self.c.register()

    def tearDown(self):
        self.undo()

    def _upload(self, name="S3 App", filename="a.yaml", data=b"a: 1\n"):
        return self.c.upload("/api/applications", {"name": name}, filename, data)

    def test_upload_goes_to_s3_with_safe_parameters_and_db_records_backend(self):
        from webapp import db
        status, data, _ = self._upload(filename="../../my app.yaml")
        self.assertEqual(status, 201, data)
        (name, kw), = self.s3.calls
        self.assertEqual(name, "put_object")
        self.assertEqual(kw["Bucket"], "test-bucket")
        self.assertRegex(kw["Key"], r"^uploads/user_\d+/[0-9a-f]{32}\.yaml$")     # generated key, no user input
        self.assertEqual(kw["ContentType"], "application/octet-stream")
        self.assertEqual(kw["ServerSideEncryption"], "AES256")
        self.assertEqual(kw["Body"], b"a: 1\n")
        with db.session() as conn:
            row = conn.execute("SELECT file_path, storage_backend FROM applications WHERE id = ?",
                               (data["application"]["id"],)).fetchone()
        self.assertEqual((row["file_path"], row["storage_backend"]), (kw["Key"], "s3"))
        self.assertFalse(os.path.exists(row["file_path"]))                          # nothing written locally

    def test_validation_still_runs_before_anything_reaches_s3(self):
        self.assertEqual(self._upload(filename="tool.exe", data=b"MZ\x90")[0], 415)
        self.assertEqual(self._upload(filename="x.json", data=b"{bad")[0], 415)
        self.assertEqual(self.s3.calls, [])

    def test_kms_encryption_parameters(self):
        from webapp import config
        config.S3_SSE, config.S3_KMS_KEY_ID = "aws:kms", "alias/chaoslab"
        self._upload()
        kw = self.s3.calls[-1][1]
        self.assertEqual((kw["ServerSideEncryption"], kw["SSEKMSKeyId"]), ("aws:kms", "alias/chaoslab"))

    def test_delete_removes_the_object(self):
        app = self._upload()[1]["application"]
        key = self.s3.calls[0][1]["Key"]
        self.assertIn(("test-bucket", key), self.s3.objects)
        self.assertEqual(self.c.delete(f"/api/applications/{app['id']}")[0], 200)
        self.assertNotIn(("test-bucket", key), self.s3.objects)
        self.assertEqual(self.c.get(f"/api/applications/{app['id']}")[0], 404)

    def test_s3_failure_on_upload_is_503_and_creates_no_record(self):
        self.s3.fail = ClientError("AccessDenied")
        status, data, _ = self._upload(name="Will Fail")
        self.assertEqual(status, 503)
        self.assertIn("AccessDenied", data["error"])
        self.assertNotIn("secret", data["error"].lower())
        self.s3.fail = None
        self.assertNotIn("Will Fail", [a["name"] for a in self.c.get("/api/applications")[1]["applications"]])

    def test_s3_failure_on_delete_keeps_the_record_for_retry(self):
        app = self._upload()[1]["application"]
        self.s3.fail = ClientError("ServiceUnavailable")
        self.assertEqual(self.c.delete(f"/api/applications/{app['id']}")[0], 502)
        self.assertEqual(self.c.get(f"/api/applications/{app['id']}")[0], 200)     # still there
        self.s3.fail = None
        self.assertEqual(self.c.delete(f"/api/applications/{app['id']}")[0], 200)  # retry works

    def test_delete_refuses_keys_outside_prefix(self):
        from webapp import storage
        with self.assertRaises(storage.StorageError):
            storage.get_storage("s3").delete("other-prefix/secret.txt")
        self.assertFalse([c for c in self.s3.calls if c[0] == "delete_object"])

    def test_files_uploaded_under_local_are_deleted_from_local_after_switching_to_s3(self):
        from webapp import config, storage
        self.undo()                                       # back to local
        local_app = self._upload(name="Was Local")[1]["application"]
        from webapp import db
        with db.session() as conn:
            path = conn.execute("SELECT file_path FROM applications WHERE id = ?", (local_app["id"],)).fetchone()[0]
        self.assertTrue(os.path.isfile(path))
        self.undo = use_s3(self.s3)                       # switch to S3
        self.assertEqual(self.c.delete(f"/api/applications/{local_app['id']}")[0], 200)
        self.assertFalse(os.path.exists(path))            # removed via the backend recorded on the row
        self.assertEqual([c for c in self.s3.calls if c[0] == "delete_object"], [])

    def test_capabilities_reports_backend_without_secrets(self):
        caps = self.c.get("/api/capabilities")[1]
        self.assertEqual(caps["storage"], "s3")
        self.assertNotIn("test-bucket", str(caps))


class StorageConfigTests(unittest.TestCase):
    def test_s3_requires_bucket(self):
        from webapp import config, storage
        saved = config.S3_BUCKET
        config.S3_BUCKET = ""
        try:
            with self.assertRaises(storage.StorageError):
                storage.S3Storage(client=object())
        finally:
            config.S3_BUCKET = saved

    def test_unknown_backend_rejected(self):
        from webapp import storage
        with self.assertRaises(storage.StorageError):
            storage.get_storage("ftp")

    def test_prefix_is_normalised_with_trailing_slash(self):
        import importlib
        os.environ["CHAOSLAB_S3_PREFIX"] = "/tenant-a/uploads"
        try:
            from webapp import config
            importlib.reload(config)
            self.assertEqual(config.S3_PREFIX, "tenant-a/uploads/")
        finally:
            os.environ.pop("CHAOSLAB_S3_PREFIX")
            importlib.reload(config)


try:
    import boto3
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:
    HAVE_MOTO = False


@unittest.skipUnless(HAVE_MOTO, "boto3 + moto not installed (pip install boto3 moto)")
class S3WithRealBoto3Tests(unittest.TestCase):
    """Real boto3 client against moto's in-process S3: verifies actual call shapes, SSE and delete."""

    def test_upload_and_delete_roundtrip(self):
        os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_DEFAULT_REGION="us-east-1")
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")
            from webapp import config, storage
            saved = {k: getattr(config, k) for k in ("STORAGE_BACKEND", "S3_BUCKET", "S3_PREFIX", "S3_REGION")}
            config.STORAGE_BACKEND, config.S3_BUCKET, config.S3_PREFIX, config.S3_REGION = "s3", "test-bucket", "uploads/", "us-east-1"
            storage.reset_cache()
            try:
                self.assertIsNone(storage.get_storage().check())
                c = Client()
                c.register()
                app = c.upload("/api/applications", {"name": "Real boto3"}, "x.yaml", b"k: v\n")[1]["application"]
                s3 = boto3.client("s3", region_name="us-east-1")
                keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket").get("Contents", [])]
                self.assertEqual(len(keys), 1)
                self.assertTrue(keys[0].startswith("uploads/user_"))
                head = s3.head_object(Bucket="test-bucket", Key=keys[0])
                self.assertEqual(head["ServerSideEncryption"], "AES256")
                self.assertEqual(head["ContentType"], "application/octet-stream")
                self.assertEqual(s3.get_object(Bucket="test-bucket", Key=keys[0])["Body"].read(), b"k: v\n")
                self.assertEqual(c.delete(f"/api/applications/{app['id']}")[0], 200)
                self.assertEqual(s3.list_objects_v2(Bucket="test-bucket").get("KeyCount"), 0)
                # missing bucket -> clean 503, no record
                storage.reset_cache()
                config.S3_BUCKET = "no-such-bucket"
                status, data, _ = c.upload("/api/applications", {"name": "Nope"}, "x.yaml", b"k: v\n")
                self.assertEqual(status, 503)
                self.assertIn("NoSuchBucket", data["error"])
            finally:
                for k, v in saved.items():
                    setattr(config, k, v)
                storage.reset_cache()


if __name__ == "__main__":
    unittest.main()
