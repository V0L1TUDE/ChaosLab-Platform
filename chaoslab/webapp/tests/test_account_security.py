"""Password change and sign-out-everywhere (POST /api/auth/change-password, POST /api/auth/logout-all)."""

import unittest

from harness import Client


def _second_session(email, password):
    """Sign the same account in from another 'device' (a separate cookie jar)."""
    other = Client()
    status, _, _ = other.post("/api/auth/login", {"email": email, "password": password})
    assert status == 200, status
    return other


class ChangePasswordTests(unittest.TestCase):
    def test_change_password_success(self):
        c = Client()
        _, email, pw = c.register()
        status, data, _ = c.post("/api/auth/change-password",
                                 {"current_password": pw, "new_password": "brand-new-pass-2"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        # The current tab stays signed in.
        self.assertEqual(c.get("/api/auth/me")[1]["user"]["email"], email)
        # Old password no longer works, new one does.
        self.assertEqual(Client().post("/api/auth/login", {"email": email, "password": pw})[0], 401)
        self.assertEqual(Client().post("/api/auth/login", {"email": email, "password": "brand-new-pass-2"})[0], 200)

    def test_change_password_revokes_other_sessions(self):
        c = Client()
        _, email, pw = c.register()
        laptop = _second_session(email, pw)
        phone = _second_session(email, pw)
        self.assertIsNotNone(laptop.get("/api/auth/me")[1]["user"])

        status, data, _ = c.post("/api/auth/change-password",
                                 {"current_password": pw, "new_password": "brand-new-pass-2"})
        self.assertEqual(status, 200)
        self.assertEqual(data["other_sessions_revoked"], 2)
        self.assertIsNone(laptop.get("/api/auth/me")[1]["user"])
        self.assertIsNone(phone.get("/api/auth/me")[1]["user"])
        self.assertIsNotNone(c.get("/api/auth/me")[1]["user"])

    def test_wrong_current_password_rejected(self):
        c = Client()
        _, email, pw = c.register()
        status, data, _ = c.post("/api/auth/change-password",
                                 {"current_password": "not-my-password", "new_password": "brand-new-pass-2"})
        self.assertEqual(status, 403)
        self.assertIn("incorrect", data["error"])
        # Nothing changed.
        self.assertEqual(Client().post("/api/auth/login", {"email": email, "password": pw})[0], 200)

    def test_new_password_validation(self):
        c = Client()
        _, _, pw = c.register()
        self.assertEqual(c.post("/api/auth/change-password", {"current_password": pw, "new_password": "short"})[0], 400)
        self.assertEqual(c.post("/api/auth/change-password", {"current_password": pw, "new_password": "x" * 257})[0], 400)
        self.assertEqual(c.post("/api/auth/change-password", {"current_password": pw, "new_password": pw})[0], 400)
        self.assertEqual(c.post("/api/auth/change-password", {"current_password": pw})[0], 400)

    def test_requires_login(self):
        status, _, _ = Client().post("/api/auth/change-password",
                                     {"current_password": "whatever-1", "new_password": "brand-new-pass-2"})
        self.assertEqual(status, 401)

    def test_wrong_current_password_is_throttled(self):
        c = Client()
        _, _, pw = c.register()
        statuses = [c.post("/api/auth/change-password",
                           {"current_password": "wrong-guess-%d" % i, "new_password": "brand-new-pass-2"})[0]
                    for i in range(9)]
        self.assertEqual(statuses[:8], [403] * 8)
        self.assertEqual(statuses[8], 429)
        # Even the right password is refused while throttled.
        self.assertEqual(c.post("/api/auth/change-password",
                                {"current_password": pw, "new_password": "brand-new-pass-2"})[0], 429)

    def test_cross_origin_change_rejected(self):
        c = Client()
        _, _, pw = c.register()
        status, _, _ = c.request("POST", "/api/auth/change-password",
                                 {"current_password": pw, "new_password": "brand-new-pass-2"},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)


class LogoutAllTests(unittest.TestCase):
    def test_logout_all_signs_out_every_session(self):
        c = Client()
        _, email, pw = c.register()
        other = _second_session(email, pw)

        status, data, _ = c.post("/api/auth/logout-all")
        self.assertEqual(status, 200)
        self.assertEqual(data["sessions_revoked"], 2)
        self.assertIsNone(c.get("/api/auth/me")[1]["user"])
        self.assertIsNone(other.get("/api/auth/me")[1]["user"])
        # The account itself still works.
        self.assertEqual(Client().post("/api/auth/login", {"email": email, "password": pw})[0], 200)

    def test_logout_all_only_affects_own_account(self):
        alice, bob = Client(), Client()
        alice.register()
        bob.register()
        alice.post("/api/auth/logout-all")
        self.assertIsNotNone(bob.get("/api/auth/me")[1]["user"])

    def test_logout_all_requires_login(self):
        self.assertEqual(Client().post("/api/auth/logout-all")[0], 401)


if __name__ == "__main__":
    unittest.main()
