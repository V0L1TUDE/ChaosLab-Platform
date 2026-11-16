import io
import os
import unittest
import zipfile

from harness import Client


def make_zip(names=("app/main.py",)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, "print('x')")
    return buf.getvalue()


class ApplicationTests(unittest.TestCase):
    def test_requires_auth(self):
        self.assertEqual(Client().get("/api/applications")[0], 401)

    def test_builtin_app_visible_and_not_deletable(self):
        c = Client()
        c.register()
        apps = c.get("/api/applications")[1]["applications"]
        demo = [a for a in apps if a["is_builtin"]]
        self.assertEqual(len(demo), 1)
        self.assertEqual(demo[0]["services_count"], 11)
        self.assertEqual(c.delete(f"/api/applications/{demo[0]['id']}")[0], 403)
        detail = c.get(f"/api/applications/{demo[0]['id']}")[1]["application"]
        self.assertEqual(len(detail["catalog"]["faults"]), 3)

    def test_upload_list_delete(self):
        c = Client()
        c.register()
        status, data, _ = c.upload("/api/applications", {"name": "My App", "description": "d"}, "../../evil name?.zip", make_zip())
        self.assertEqual(status, 201, data)
        app = data["application"]
        self.assertEqual(app["status"], "registered")
        self.assertEqual(app["original_filename"], "evil name_.zip")  # path stripped + sanitized
        self.assertFalse(app["executable"])
        names = [a["name"] for a in c.get("/api/applications")[1]["applications"]]
        self.assertIn("My App", names)
        self.assertEqual(c.delete(f"/api/applications/{app['id']}")[0], 200)
        self.assertEqual(c.get(f"/api/applications/{app['id']}")[0], 404)

    def test_invalid_uploads_rejected(self):
        c = Client()
        c.register()
        up = lambda fn, content, name="App": c.upload("/api/applications", {"name": name}, fn, content)[0]
        self.assertEqual(up("tool.exe", b"MZ\x90\x00"), 415)                 # extension
        self.assertEqual(up("fake.zip", b"not a zip at all"), 415)           # content mismatch
        self.assertEqual(up("script.txt", b"#!/bin/sh\nrm -rf /"), 415)      # shebang
        self.assertEqual(up("bad.json", b"{nope"), 415)
        self.assertEqual(up("empty.txt", b""), 400)
        self.assertEqual(up("slip.zip", make_zip(("../../etc/passwd",))), 415)  # traversal
        self.assertEqual(up("big.txt", b"a" * (1024 * 1024 + 10)), 413)         # > 1 MB test limit
        self.assertEqual(up("ok.yaml", b"a: 1\n", name=""), 400)                # missing name
        self.assertEqual(c.upload("/api/applications", {"name": "x"}, None, b"")[0], 400)  # missing file

    def test_user_isolation(self):
        a, b = Client(), Client()
        a.register()
        b.register()
        app = a.upload("/api/applications", {"name": "Private"}, "p.yaml", b"a: 1\n")[1]["application"]
        self.assertEqual(b.get(f"/api/applications/{app['id']}")[0], 404)
        self.assertEqual(b.delete(f"/api/applications/{app['id']}")[0], 404)
        self.assertNotIn("Private", [x["name"] for x in b.get("/api/applications")[1]["applications"]])
        self.assertEqual(a.get(f"/api/applications/{app['id']}")[0], 200)

    def test_stored_file_lives_under_upload_dir_with_generated_name(self):
        from webapp import config, db
        c = Client()
        c.register()
        app = c.upload("/api/applications", {"name": "Stored"}, "../x.md", b"# hi")[1]["application"]
        with db.session() as conn:
            row = conn.execute("SELECT file_path FROM applications WHERE id = ?", (app["id"],)).fetchone()
        self.assertTrue(os.path.realpath(row["file_path"]).startswith(os.path.realpath(config.UPLOAD_DIR)))
        self.assertTrue(os.path.isfile(row["file_path"]))
        c.delete(f"/api/applications/{app['id']}")
        self.assertFalse(os.path.exists(row["file_path"]))


if __name__ == "__main__":
    unittest.main()
