"""
The database store: the same SQL on SQLite and Postgres, and the wipe that
resets a test site between rounds.
"""
import os
import tempfile
import unittest

import helpers  # noqa: F401

from resume_builder import record as mr
from resume_builder import store


class Store(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = os.environ.get("RB_DATABASE_URL") or os.path.join(self.tmp.name, "s.sqlite3")
        self.conn = store.connect(self.target)
        store.SqlStore.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_postgres_addresses_are_told_apart_from_file_paths(self):
        self.assertTrue(store.is_postgres("postgresql://u@/db?host=/cloudsql/p:r:i"))
        self.assertTrue(store.is_postgres("postgres://u@h/db"))
        self.assertFalse(store.is_postgres(r"C:\Users\x\web\t.sqlite3"))

    def test_a_record_its_versions_and_state_round_trip(self):
        s = store.SqlStore(self.conn, "a@example.com")
        s.save_record(mr.parse("# R\n\n## Roles\n\n### Acme — Analyst\n"))
        s.save_record(mr.parse("# R\n\n## Roles\n\n### Acme — Director\n"))
        s.save_state("interview", {"step": 2})
        self.assertEqual(s.load_record().roles[0].title, "Director")
        self.assertEqual(len(s.history()), 1)
        self.assertEqual(s.load_state("interview"), {"step": 2})

    def test_wiping_a_test_site_empties_every_user(self):
        for user in ("a@example.com", "b@example.com"):
            s = store.SqlStore(self.conn, user)
            s.save_record(mr.parse("# R\n"))
            s.save_state("interview", {"step": 1})
        store.SqlStore.wipe_all(self.conn)
        for user in ("a@example.com", "b@example.com"):
            s = store.SqlStore(self.conn, user)
            self.assertFalse(s.exists())
            self.assertIsNone(s.load_state("interview"))


if __name__ == "__main__":
    unittest.main()
