"""Exercise reuse against real external writes, WAL, and database replacement."""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import Database


class ReadConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.db'
        self.db = Database(self.path)
        with sqlite3.connect(self.path) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('CREATE TABLE grants(owner TEXT, allowed INTEGER)')
            writer.execute("INSERT INTO grants VALUES('alice', 1)")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_external_commit_and_revocation_are_immediately_visible(self):
        self.assertEqual(self.db.fetchone('SELECT allowed FROM grants')['allowed'], 1)
        writer = sqlite3.connect(self.path)
        writer.execute('UPDATE grants SET allowed=0')
        # An unfinished write never leaks through a reused connection.
        self.assertEqual(self.db.fetchone('SELECT allowed FROM grants')['allowed'], 1)
        writer.commit()
        self.assertEqual(self.db.fetchone('SELECT allowed FROM grants')['allowed'], 0)
        writer.close()

    def test_fetchone_drains_statement_and_releases_checkpoint_snapshot(self):
        writer = sqlite3.connect(self.path)
        writer.executemany('INSERT INTO grants VALUES(?,1)', [(str(i),) for i in range(100)])
        writer.commit()
        self.db.fetchone('SELECT * FROM grants')
        writer.execute('UPDATE grants SET allowed=0'); writer.commit()
        self.assertEqual(tuple(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()), (0, 0, 0))
        self.assertTrue(all(row['allowed'] == 0 for row in self.db.fetchall('SELECT * FROM grants')))
        writer.close()

    def test_read_api_cannot_mutate_and_recovers_from_error(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.db.fetchall('DELETE FROM grants RETURNING *')
        with self.assertRaises(sqlite3.OperationalError):
            self.db.fetchall('SELECT * FROM missing_table')
        self.assertEqual(len(self.db.fetchall('SELECT * FROM grants')), 1)

    def test_parallel_requests_and_real_writer_keep_connections_separate(self):
        def read(_):
            return self.db.fetchone('SELECT COUNT(*) AS n FROM grants')['n']
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(read, range(80))), [1] * 80)

    def test_live_database_replacement_fails_closed_instead_of_serving_old_permissions(self):
        self.db.fetchone('SELECT * FROM grants')
        self.db.close()
        replacement = self.path.with_suffix('.new')
        with sqlite3.connect(replacement) as db:
            db.execute('CREATE TABLE grants(owner TEXT, allowed INTEGER)')
            db.execute("INSERT INTO grants VALUES('alice',0)")
        # Live replacement is not a supported restore: old WAL sidecars may
        # still exist. A reader must reject it rather than mix the two files.
        self.db.fetchone('SELECT * FROM grants')
        os.replace(replacement, self.path)
        for _ in range(2):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'offline restore'):
                self.db.fetchone('SELECT allowed FROM grants')

    def test_read_connection_never_pins_explicit_old_transaction(self):
        with self.db.read_connection() as db:
            db.execute('BEGIN').close()
            db.execute('SELECT * FROM grants').fetchall()
        with sqlite3.connect(self.path) as writer:
            writer.execute('UPDATE grants SET allowed=0')
        self.assertEqual(self.db.fetchone('SELECT allowed FROM grants')['allowed'], 0)
