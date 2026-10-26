"""Exercise the real LIKE fallback with permissions and equal timestamps."""
import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.services.search import SearchService


class SearchQueryPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'test.db')
        self.db.initialize()
        self.libs = [self.db.create_library(str(i), str(self.root / str(i)))['id'] for i in range(2)]
        with self.db.transaction() as db:
            for i in range(200):
                db.execute('''INSERT INTO files(library_id,path,relative_path,name,kind,
                    mtime_ns,ai_caption,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (self.libs[i % 2], str(self.root / ('item'+str(i))), 'item'+str(i),
                     'item'+str(i), 'image' if i % 3 else 'video', i // 2,
                     '灰色墙上白色开关' if i % 5 else '蓝色天空', 'now', 'now'))
        self.search = SearchService(self.db, None, None)

    def tearDown(self):
        self.temp.cleanup()

    def test_kind_fallback_uses_ordered_index_without_sorting_all_matches(self):
        statements = []
        original = self.db.fetchall
        def record(sql, params=()):
            if ' LIKE ' in sql:
                statements.append((sql, params))
            return original(sql, params)
        self.db.fetchall = record
        rows = self.search._lexical('灰色', 'image', 20, like_terms=['灰色'])
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(statements), 1)
        sql, params = statements[0]
        with self.db.connect() as db:
            plan = ' '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN '+sql, params))
        self.assertIn('idx_files_kind_mtime', plan)
        self.assertNotIn('TEMP B-TREE', plan)

    def test_adding_index_keeps_ordered_ids_and_permission_filters(self):
        cases = [dict(), dict(library_ids=[self.libs[0]]), dict(library_ids=[]),
                 dict(file_ids=list(range(1, 70))), dict(file_ids=[]),
                 dict(filter_sql=('f.library_id = ? AND f.id < ?', [self.libs[1], 80]))]
        def results():
            return [[row['id'] for row in self.search._lexical(
                '灰色', 'image', 20, like_terms=['灰色'], **case)] for case in cases]
        self.db.execute('DROP INDEX idx_files_kind_mtime')
        before = results()
        self.db.execute('CREATE INDEX idx_files_kind_mtime ON files(kind, mtime_ns DESC)')
        self.assertEqual(results(), before)
        self.assertEqual(before[2], [])
        self.assertEqual(before[4], [])

    def test_existing_database_gets_index_without_changing_media_rows(self):
        # Raw fixture inserts bypass scanner state normalization; establish the
        # existing migration baseline before testing an already-used database.
        self.db.initialize()
        before = self.db.fetchall('SELECT * FROM files ORDER BY id')
        self.db.execute('DROP INDEX idx_files_kind_mtime')
        self.db.initialize()
        self.assertIsNotNone(self.db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_files_kind_mtime'"))
        self.assertEqual(self.db.fetchall('SELECT * FROM files ORDER BY id'), before)
