"""Hostile uploads: zip/gzip bombs, lying headers, entry floods, traversal, links, deep nesting, slow inputs.
Every case must end in a controlled 4xx (or an 'invalid' manifest result), never a 500, never unbounded work, and
nothing may be stored before validation has finished."""

import gzip
import io
import stat
import tarfile
import time
import unittest
import zipfile

import harness
from harness import Client

from webapp import archive_guard, manifests, storage, uploads  # noqa: E402


def make_zip(entries, compression=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for e in entries:
            if isinstance(e, zipfile.ZipInfo):
                zf.writestr(e, b"x")
            else:
                name, data = e
                zf.writestr(name, data)
    return buf.getvalue()


def make_tar(members, gz=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w") as tf:
        for m, data in members:
            tf.addfile(m, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def tar_file(name, size):
    m = tarfile.TarInfo(name)
    m.size = size
    return m, b"\0" * size


class ArchiveGuardTests(unittest.TestCase):
    def rejects(self, fn, needle):
        with self.assertRaises(archive_guard.ArchiveRejected) as cm:
            fn()
        self.assertIn(needle, str(cm.exception))

    def test_zip_bomb_by_ratio_is_rejected_without_decompressing(self):
        bomb = make_zip([("zeros.bin", b"\0" * (8 * 1024 * 1024))])          # ~8 KB compressed
        self.assertLess(len(bomb), 64 * 1024)
        t = time.monotonic()
        self.rejects(lambda: archive_guard.inspect_zip(bomb), "compression ratio")
        self.assertLess(time.monotonic() - t, 1.0)

    def test_total_decompressed_budget(self):
        z = make_zip([(f"f{i}.txt", bytes(range(256)) * 40) for i in range(10)])      # ~100 KB decompressed
        self.rejects(lambda: archive_guard.inspect_zip(z, archive_guard.Budget(max_bytes=50_000)), "decompressed limit")
        archive_guard.inspect_zip(z)                                                  # fine within the default

    def test_declared_size_is_checked_before_reading(self):
        saved = archive_guard.MAX_UNCOMPRESSED_BYTES
        archive_guard.MAX_UNCOMPRESSED_BYTES = 1000
        try:
            z = make_zip([("a.txt", b"a" * 600), ("b.txt", b"b" * 600)], compression=zipfile.ZIP_STORED)
            self.rejects(lambda: archive_guard.zip_entries(z), "decompressed limit")
        finally:
            archive_guard.MAX_UNCOMPRESSED_BYTES = saved

    def test_lying_header_is_bounded_by_the_read_limit(self):
        z = bytearray(make_zip([("m.yaml", b"k: " + b"v" * 5000)], compression=zipfile.ZIP_STORED))
        # shrink the declared size in the central directory: the real member is far bigger
        cd = z.rfind(b"PK\x01\x02")
        z[cd + 24:cd + 28] = (10).to_bytes(4, "little")
        zf, infos = archive_guard.zip_entries(bytes(z))
        with zf:
            with self.assertRaises(archive_guard.ArchiveRejected):
                archive_guard.read_zip_member(zf, infos[0], archive_guard.Budget(), 10)

    def test_entry_flood(self):
        z = make_zip([(f"d/{i}.txt", b"") for i in range(archive_guard.MAX_ENTRIES + 1)], compression=zipfile.ZIP_STORED)
        self.rejects(lambda: archive_guard.inspect_zip(z), "too many entries")

    def test_paths(self):
        for name in ("../evil.yaml", "/etc/passwd", "C:/win.ini", "a/../../b", "x\x01y",
                     "/".join(["d"] * (archive_guard.MAX_PATH_DEPTH + 1)) + "/f", "a" * 300):
            z = make_zip([(name, b"x")], compression=zipfile.ZIP_STORED)
            with self.assertRaises(archive_guard.ArchiveRejected, msg=name):
                archive_guard.inspect_zip(z)

    def test_zip_symlink_and_encrypted_entries(self):
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.rejects(lambda: archive_guard.inspect_zip(make_zip([link])), "symlinks")
        enc = bytearray(make_zip([("secret.txt", b"x")], compression=zipfile.ZIP_STORED))
        cd = enc.rfind(b"PK\x01\x02")
        enc[cd + 8] |= 0x1                                                    # general purpose flag: encrypted
        self.rejects(lambda: archive_guard.inspect_zip(bytes(enc)), "Encrypted")

    def test_corrupt_and_truncated_zip(self):
        good = make_zip([("a.yaml", b"k: v\n" * 200)])
        self.rejects(lambda: archive_guard.inspect_zip(good[: len(good) // 2]), "not a valid ZIP")
        flipped = bytearray(good)
        flipped[30 + len("a.yaml") + 2] ^= 0xFF                              # inside the member's compressed data
        with self.assertRaises(archive_guard.ArchiveRejected):
            archive_guard.inspect_zip(bytes(flipped))

    def test_gzip_bomb_is_stopped_at_the_budget(self):
        saved = archive_guard.MAX_UNCOMPRESSED_BYTES
        archive_guard.MAX_UNCOMPRESSED_BYTES = 1024 * 1024
        try:
            bomb = make_tar([tar_file("zeros.bin", 4 * 1024 * 1024)], gz=True)
            self.assertLess(len(bomb), 64 * 1024)
            t = time.monotonic()
            self.rejects(lambda: archive_guard.inspect_tar(bomb, gz=True), "decompressed limit")
            self.assertLess(time.monotonic() - t, 2.0)
        finally:
            archive_guard.MAX_UNCOMPRESSED_BYTES = saved

    def test_tar_links_devices_and_traversal(self):
        sym = tarfile.TarInfo("ln"); sym.type = tarfile.SYMTYPE; sym.linkname = "/etc/shadow"
        hard = tarfile.TarInfo("hl"); hard.type = tarfile.LNKTYPE; hard.linkname = "x"
        dev = tarfile.TarInfo("dev"); dev.type = tarfile.CHRTYPE
        for m in (sym, hard, dev):
            self.rejects(lambda m=m: archive_guard.inspect_tar(make_tar([(m, None)]), gz=False), "special files")
        self.rejects(lambda: archive_guard.inspect_tar(make_tar([tar_file("../x", 1)]), gz=False), "unsafe paths")
        self.assertEqual(archive_guard.inspect_tar(make_tar([tar_file("ok/x.txt", 10)], gz=True), gz=True), 0)

    def test_time_budget(self):
        saved = archive_guard.TIME_BUDGET_SECONDS
        archive_guard.TIME_BUDGET_SECONDS = -1
        try:
            self.rejects(lambda: archive_guard.inspect_zip(make_zip([("a.txt", b"x")])), "too long")
        finally:
            archive_guard.TIME_BUDGET_SECONDS = saved

    def test_not_gzip(self):
        self.rejects(lambda: archive_guard.inspect_tar(b"plain bytes", gz=True), "gzip")


class ManifestParsingTests(unittest.TestCase):
    def test_deep_yaml_nesting_is_rejected_not_a_crash(self):
        deep = ("a: " + "[" * 5000 + "]" * 5000).encode()
        r = manifests.analyze("yaml", deep)
        self.assertFalse(r["valid"])
        self.assertTrue(any("nested deeper" in e for e in r["errors"]), r["errors"])

    def test_anchors_aliases_and_bad_utf8(self):
        r = manifests.analyze("yaml", b"a: &x [1, 2]\nb: *x\n")
        self.assertTrue(any("aliases" in e for e in r["errors"]))
        r = manifests.analyze("yaml", b"kind: \xff\xfe\n")
        self.assertTrue(any("UTF-8" in e for e in r["errors"]))

    def test_zip_manifest_limits(self):
        many = make_zip([(f"m{i}.yaml", b"a: 1\n") for i in range(manifests.MAX_FILES + 1)])
        self.assertTrue(any("Too many manifest files" in e for e in manifests.analyze("zip", many)["errors"]))
        big = make_zip([("big.yaml", b"# " + b"x" * (manifests.MAX_FILE_BYTES + 10))])
        self.assertTrue(any("larger than" in e for e in manifests.analyze("zip", big)["errors"]))
        bomb = make_zip([("bomb.yaml", b" " * (8 * 1024 * 1024))])
        r = manifests.analyze("zip", bomb)
        self.assertFalse(r["valid"])

    def test_parse_time_budget(self):
        saved = manifests.PARSE_BUDGET_SECONDS
        manifests.PARSE_BUDGET_SECONDS = -1
        try:
            r = manifests.analyze("yaml", b"a: 1\n")
            self.assertTrue(any("too long" in e for e in r["errors"]), r["errors"])
        finally:
            manifests.PARSE_BUDGET_SECONDS = saved


class RecordingStorage:
    name = "local"

    def __init__(self):
        self.puts = []

    def put(self, key, data):
        self.puts.append(key)
        return "ref/" + key

    def delete(self, ref):
        pass


class UploadEndpointTests(unittest.TestCase):
    def setUp(self):
        self.c = Client()
        self.c.register()
        self.store = RecordingStorage()
        self.saved = storage.get_storage
        storage.get_storage = lambda name=None: self.store

    def tearDown(self):
        storage.get_storage = self.saved

    def up(self, filename, data):
        return self.c.upload("/api/applications", {"name": "x"}, filename, data)

    def test_hostile_archives_get_controlled_errors_and_nothing_is_stored(self):
        bomb = make_zip([("zeros.bin", b"\0" * (8 * 1024 * 1024))])
        flood = make_zip([(f"{i}", b"") for i in range(archive_guard.MAX_ENTRIES + 1)], compression=zipfile.ZIP_STORED)
        trav = make_zip([("../../etc/cron.d/x", b"x")])
        gzb = make_tar([tar_file("z", 900 * 1024)], gz=True)            # fine: well under the budget
        for name, data, expected in (("b.zip", bomb, 415), ("f.zip", flood, 415), ("t.zip", trav, 415),
                                     ("c.zip", b"PK\x03\x04garbage", 415), ("g.tar.gz", b"\x1f\x8bnot-gzip", 415),
                                     ("d.json", b"[" * 100000, 415), ("e.yaml", b"\x7fELF\x02", 415)):
            s, d, _ = self.up(name, data)
            self.assertEqual(s, expected, (name, d))
            self.assertIn("error", d)
        self.assertEqual(self.store.puts, [])                              # rejected uploads are never stored
        s, d, _ = self.up("ok.tar.gz", gzb)
        self.assertEqual(s, 201, d)
        self.assertEqual(len(self.store.puts), 1)

    def test_manifest_is_validated_before_storage(self):
        order = []
        real_analyze = manifests.analyze
        manifests.analyze = lambda *a: order.append("analyze") or real_analyze(*a)
        real_put = self.store.put
        self.store.put = lambda k, d: order.append("store") or real_put(k, d)
        try:
            body = (b"apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web}\nspec:\n  selector: {matchLabels: {app: web}}\n"
                    b"  template:\n    metadata: {labels: {app: web}}\n    spec:\n      containers:\n"
                    b"      - {name: c, image: 'nginx:1.27'}\n")
            s, d, _ = self.up("app.yaml", body)
        finally:
            manifests.analyze = real_analyze
        self.assertEqual(s, 201, d)
        self.assertEqual(order, ["analyze", "store"])
        self.assertEqual(d["application"]["status"], "validated")

    def test_deeply_nested_manifest_is_a_controlled_invalid_result(self):
        s, d, _ = self.up("deep.yaml", ("a: " + "[" * 3000 + "]" * 3000).encode())
        self.assertEqual(s, 201, d)
        self.assertEqual(d["application"]["status"], "invalid")
        self.assertFalse(d["application"]["deployable"])


if __name__ == "__main__":
    unittest.main()
