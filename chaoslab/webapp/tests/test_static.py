"""Static assets: the favicon is served and linked from the page."""

import unittest

from harness import Client


class StaticTests(unittest.TestCase):
    def test_favicon(self):
        c = Client()
        for path in ("/static/favicon.svg", "/favicon.ico"):
            status, body, resp = c.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(resp.getheader("Content-Type"), "image/svg+xml", path)
            self.assertTrue(body.startswith(b"<svg"), path)
        status, body, _ = c.get("/")
        self.assertIn(b'rel="icon" type="image/svg+xml" href="/static/favicon.svg"', body)


if __name__ == "__main__":
    unittest.main()
