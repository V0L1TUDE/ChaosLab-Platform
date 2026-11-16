"""Durable project data and identity survive independent SQLite connections."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

PATH=Path(__file__).resolve().parents[2]/'independent-apps/project-management/services/project-service/storage.py'
spec=importlib.util.spec_from_file_location('project_store_test',PATH)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
ProjectStore=module.ProjectStore


class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'data/projects.sqlite3'

    def test_record_and_id_survive_reopening(self):
        record=ProjectStore(self.path).create({'name':"Customer's project",'owner_id':'usr-1'})
        self.assertEqual(ProjectStore(self.path).get(record['id']),record)

    def test_seeding_never_resets_existing_records(self):
        store=ProjectStore(self.path,{'proj-1':{'id':'proj-1','name':'original'}})
        created=store.create({'name':'new'})
        reopened=ProjectStore(self.path,{'proj-1':{'id':'proj-1','name':'replacement'}})
        self.assertEqual(reopened.get('proj-1')['name'],'original')
        self.assertEqual(reopened.get(created['id']),created)
        self.assertEqual(len(reopened.list()),2)

    def test_concurrent_creates_have_unique_ids(self):
        store=ProjectStore(self.path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            records=list(pool.map(lambda index:store.create({'name':str(index)}),range(20)))
        self.assertEqual(len({r['id'] for r in records}),20)
        self.assertEqual(len(ProjectStore(self.path).list()),20)

    def test_missing_or_sql_like_id_cannot_select_other_records(self):
        store=ProjectStore(self.path);store.create({'name':'one'})
        self.assertIsNone(store.get("' OR 1=1 --"))
        self.assertEqual(len(store.list()),1)
