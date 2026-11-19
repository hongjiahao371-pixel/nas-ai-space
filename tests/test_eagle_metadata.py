"""Synthetic fixtures only. No third-party media is distributed."""
import io
import json
import tempfile
from pathlib import Path
import unittest
import zipfile
from app.services.eagle_metadata import read_eagle_metadata


def synthetic_pack(change=None):
    item={'id':'ITEM1','name':'Synthetic','ext':'txt','size':5,'tags':['review','中文'],
          'star':4,'annotation':'A note <script>not executable</script>',
          'url':'https://example.com/source','folders':['F1'],'isDeleted':False}
    if change:
        item.update(change)
    pack={'images':[item], 'folder':{'id':'F1','name':'References','children':[], 'password':'NEVER_IMPORT'}}
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z:
        z.writestr('pack.json',json.dumps(pack))
        z.writestr('ITEM1.info/metadata.json',json.dumps(item))
        z.writestr('ITEM1.info/Synthetic.txt',b'hello')
        z.writestr('ITEM1.info/Synthetic_thumbnail.png',b'not an image')
    return data.getvalue()


class EagleMetadataTests(unittest.TestCase):
    def read(self,data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return read_eagle_metadata(z)

    def test_confirmed_layout_preserves_plain_fields(self):
        records,warnings=self.read(synthetic_pack())
        self.assertEqual(warnings,[])
        self.assertEqual(list(records),['ITEM1.info/Synthetic.txt'])
        record=next(iter(records.values()))
        self.assertEqual(record['rating'],4)
        for text in ['中文','A note','https://example.com/source','References']:
            self.assertIn(text,record['description'])
        self.assertNotIn('NEVER_IMPORT',str(records))

    def test_invalid_values_and_paths(self):
        for change in [{'id':'../x'},{'name':'../x'},{'name':'bad\ud800'},{'isDeleted':'false'},{'star':True},{'star':6},{'size':6},{'tags':['x']*101},{'annotation':'x'*12001},{'folders':'F1'}]:
            with self.subTest(change=str(change)[:100]),self.assertRaises(ValueError):
                self.read(synthetic_pack(change))

    def test_unsafe_urls_omitted_no_fetch(self):
        for url in ['file:///etc/passwd','javascript:alert(1)','https://user:pass@example.com/']:
            records,warnings=self.read(synthetic_pack({'url':url}))
            self.assertTrue(warnings)
            self.assertNotIn(url,str(records))

    def test_generic_fallback_and_ambiguous_layout(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('normal.txt','hello')
        records,warnings=self.read(b.getvalue());self.assertEqual(records,{});self.assertTrue(warnings)
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('pack.json','{"images":[],"images":[]}')
        with self.assertRaises(ValueError):self.read(b.getvalue())

    def test_zip_budget_and_traversal_remain_enforced(self):
        from app.services.ingest import _extract_packages
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            package=root/'plain.zip'
            with zipfile.ZipFile(package,'w') as z:z.writestr('nested/hello.txt','hello')
            with self.assertRaises(ValueError):_extract_packages(root,4)
            _extract_packages(root,1024)
            self.assertEqual((root/'plain-import/nested/hello.txt').read_text(),'hello')
            with zipfile.ZipFile(root/'bad.eaglepack','w') as z:z.writestr('../escape.txt','oops')
            with self.assertRaises(ValueError):_extract_packages(root,1024)
            self.assertFalse((root.parent/'escape.txt').exists())

    def test_conflicting_metadata_rejected(self):
        src=zipfile.ZipFile(io.BytesIO(synthetic_pack()));b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            for name in src.namelist():
                data=src.read(name)
                if name.endswith('/metadata.json'):
                    item=json.loads(data);item['annotation']='conflicting';data=json.dumps(item)
                z.writestr(name,data)
        with self.assertRaises(ValueError):self.read(b.getvalue())


class EagleInboxTests(unittest.TestCase):
    def test_existing_authenticated_inbox_route(self):
        import shutil,time
        from fastapi.testclient import TestClient
        from test_core import app,state,settings
        with TestClient(app) as client:
            project=client.post('/api/projects',json={'name':'Synthetic Eagle import'}).json()['id']
            inbox=settings.ingest_root/f'project-{project}';inbox.mkdir(parents=True,exist_ok=True)
            (inbox/'sample.eaglepack').write_bytes(synthetic_pack())
            def collect():
                response=client.post(f'/api/projects/{project}/inbox/collect')
                self.assertEqual(response.status_code,202,response.text)
                for _ in range(300):
                    task=state.database.get_task(response.json()['task_id'])
                    if task['status'] in {'completed','failed','cancelled'}:return task
                    time.sleep(.03)
                self.fail('task timeout')
            try:
                self.assertEqual(collect()['status'],'completed')
                assets=client.get(f'/api/projects/{project}/assets').json()['items']
                self.assertEqual(len(assets),1,assets)
                asset=state.database.fetchone('SELECT * FROM assets WHERE id=?',(assets[0]['id'],))
                self.assertEqual(asset['rating'],4);self.assertIn('review',asset['description'])
                state.database.execute('UPDATE assets SET description=? WHERE id=?',('human edited',asset['id']))
                self.assertEqual(collect()['status'],'completed')
                self.assertEqual(state.database.fetchone('SELECT description FROM assets WHERE id=?',(asset['id'],))['description'],'human edited')
                (inbox/'sample-import/ITEM1.info/Synthetic.txt').write_text('wrong')
                self.assertEqual(collect()['status'],'failed')
            finally:
                shutil.rmtree(inbox,ignore_errors=True)
                client.delete(f'/api/projects/{project}')
