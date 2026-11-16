import unittest

from harness import Client


class AuthTests(unittest.TestCase):
    def test_register_login_logout_flow(self):
        c = Client()
        user, email, pw = c.register()
        self.assertEqual(user["email"], email)
        self.assertNotIn("password_hash", user)

        status, data, _ = c.get("/api/auth/me")
        self.assertEqual(data["user"]["email"], email)

        c.post("/api/auth/logout")
        self.assertIsNone(c.get("/api/auth/me")[1]["user"])

        status, data, _ = c.post("/api/auth/login", {"email": email, "password": pw})
        self.assertEqual(status, 200)
        self.assertEqual(c.get("/api/auth/me")[1]["user"]["email"], email)

    def test_duplicate_and_validation(self):
        c = Client()
        _, email, _ = c.register()
        self.assertEqual(Client().post("/api/auth/register", {"name": "x", "email": email, "password": "longenough1"})[0], 409)
        self.assertEqual(Client().post("/api/auth/register", {"name": "x", "email": "bad", "password": "longenough1"})[0], 400)
        self.assertEqual(Client().post("/api/auth/register", {"name": "x", "email": "a@b.co", "password": "short"})[0], 400)
        self.assertEqual(Client().post("/api/auth/register", {"name": "", "email": "a@b.co", "password": "longenough1"})[0], 400)

    def test_bad_login_is_generic(self):
        c = Client()
        _, email, _ = c.register()
        s1, d1, _ = Client().post("/api/auth/login", {"email": email, "password": "wrong-password"})
        s2, d2, _ = Client().post("/api/auth/login", {"email": "nobody@example.com", "password": "wrong-password"})
        self.assertEqual((s1, s2), (401, 401))
        self.assertEqual(d1["error"], d2["error"])

    def test_protected_routes_require_auth(self):
        c = Client()
        self.assertEqual(c.get("/api/auth/me")[1]["user"], None)
        self.assertEqual(c.get("/api/does-not-exist")[0], 404)

    def test_cross_origin_post_rejected(self):
        c = Client()
        status, _, _ = c.request("POST", "/api/auth/login", {"email": "a@b.co", "password": "x"},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_non_json_content_type_rejected(self):
        c = Client()
        status, _, _ = c.request("POST", "/api/auth/login", raw=b"email=a", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


if __name__ == "__main__":
    unittest.main()
