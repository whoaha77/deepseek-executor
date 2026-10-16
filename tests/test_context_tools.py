"""Synthetic context retrieval checks; no services, credentials or model calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    s=importlib.util.spec_from_file_location(name,ROOT/'bin'/(name+'.py'))
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


class CodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.tool=load('code_context')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.p=self.root/'sample.py'
        self.source="raise RuntimeError('must never execute')\n\nclass Example:\n    @undefined_decorator\n    async def method(self, x):\n        def inner(y):\n            return y + 1\n        return inner(x)\n"
        self.p.write_bytes(self.source.encode('utf-8'))
    def test_outline_qualified_symbols_without_execution(self):
        result=self.tool.outline(self.p)
        names={s['name'] for s in result['symbols']}
        self.assertTrue({'Example','Example.method','Example.method.inner'}<=names)
        self.assertNotIn('must never execute',json.dumps(result))
        self.assertEqual(result['sha256'],hashlib.sha256(self.p.read_bytes()).hexdigest())
    def test_symbol_exact_decorated_span_and_hash(self):
        result=self.tool.show_symbol(self.p,'Example.method')
        self.assertEqual(result['text'],''.join(self.source.splitlines(keepends=True)[3:8]))
        self.assertEqual(result['start'],4);self.assertEqual(result['end'],8)
    def test_stale_oversized_and_ambiguous_refused(self):
        sha=hashlib.sha256(self.p.read_bytes()).hexdigest();self.p.write_text('def changed():\n    pass\n')
        with self.assertRaises(Exception):self.tool.show_symbol(self.p,'changed',expected_sha=sha)
        with self.assertRaises(Exception):self.tool.show_symbol(self.p,'changed',max_chars=1)
        self.p.write_text('def dup():\n    pass\ndef dup():\n    pass\n')
        with self.assertRaises(Exception):self.tool.show_symbol(self.p,'dup')
    def test_search_is_bounded_and_skips_archives(self):
        hidden=self.root/'archives';hidden.mkdir();(hidden/'old.py').write_text('def hidden():pass\n')
        for folder in ('dist','BUILD','_archive'):
            ignored=self.root/folder;ignored.mkdir();(ignored/'old.py').write_text('def method_hidden():pass\n')
        (self.root/'bad.py').write_text('def ?')
        result=self.tool.find_symbols(self.root,'method',limit=1)
        self.assertEqual(len(result['symbols']),1)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['errors'])
        self.assertNotIn('hidden',json.dumps(result))
    def test_conditional_symbols_and_exact_limit(self):
        self.p.write_bytes(b'if True:\n    def conditional():\n        pass\n')
        result=self.tool.find_symbols(self.root,'conditional',limit=1)
        self.assertEqual(result['symbols'][0]['name'],'conditional')
        self.assertFalse(result['truncated'])
        self.assertEqual(self.tool.outline(self.p,limit=1)['total_symbols'],1)
        with self.assertRaises(Exception):self.tool.find_symbols(self.root,' ')
    def test_crlf_preserved_and_non_python_refused(self):
        self.p.write_bytes(b'def crlf():\r\n    pass\r\n')
        self.assertEqual(self.tool.show_symbol(self.p,'crlf')['text'],'def crlf():\r\n    pass\r\n')
        other=self.root/'other.js';other.write_text('function x(){}')
        with self.assertRaises(Exception):self.tool.outline(other)
    def test_special_files_refused_before_read_and_walk_errors_reported(self):
        with patch.object(Path,'stat',return_value=SimpleNamespace(st_mode=0o010600)):
            with self.assertRaisesRegex(ValueError,'regular file'):self.tool.outline(self.p)
        def failed_walk(*args,**kwargs):
            kwargs['onerror'](PermissionError(13,'synthetic denied','blocked'))
            return iter(())
        with patch.object(self.tool.os,'walk',side_effect=failed_walk):
            result=self.tool.find_symbols(self.root,'method')
        self.assertTrue(result['errors']);self.assertEqual(result['files_scanned'],0)


class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.tool=load('memory_index')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.p=self.root/'note.md';self.p.write_bytes('# Budget\n预算 timeout policy\nLine 3\nalpha beta\n'.encode('utf-8'))
        self.db=self.root/'index.sqlite'
    def index(self,**kwargs):return self.tool.index_documents(self.root,['note.md'],self.db,chunk_lines=2,**kwargs)
    def test_progressive_search_and_exact_get(self):
        self.index();result=self.tool.search_index(self.db,'预算 timeout')
        self.assertEqual(result['total'],1)
        item=result['results'][0]
        self.assertNotIn('text',item);self.assertNotIn('body',item)
        self.assertEqual(item['start_line'],1);self.assertEqual(item['end_line'],2)
        detail=self.tool.get_chunk(self.db,item['id'])
        self.assertEqual(detail['text'],'# Budget\n预算 timeout policy\n')
        self.assertEqual(detail['stale_status'],'verified')
    def test_stale_source_refused(self):
        self.index();item=self.tool.search_index(self.db,'timeout')['results'][0]
        self.p.write_text('changed')
        with self.assertRaises(Exception):self.tool.get_chunk(self.db,item['id'])
    def test_all_any_and_literal_sql_terms(self):
        self.index()
        self.assertEqual(self.tool.search_index(self.db,'timeout beta')['total'],0)
        self.assertEqual(self.tool.search_index(self.db,'timeout beta',match='any')['total'],2)
        self.assertEqual(self.tool.search_index(self.db,"' OR 1=1")['total'],0)
        with self.assertRaises(Exception):self.tool.search_index(self.db,'   ')
    def test_overwrite_requires_explicit_replace_and_backup(self):
        self.index();old=self.db.read_bytes()
        with self.assertRaises(Exception):self.index()
        self.p.write_text('# new\nnew content\n');self.index(replace=True)
        self.assertTrue(any(p.is_file() and p!=self.db and p.read_bytes()==old for p in self.root.iterdir()))
    def test_failure_preserves_old_database(self):
        self.index();old=self.db.read_bytes()
        self.p.write_text('fake credential '+'sk-'+'A'*24)
        with self.assertRaises(Exception) as failure:self.index(replace=True)
        self.assertNotIn('sk-'+'A'*24,str(failure.exception))
        self.assertEqual(self.db.read_bytes(),old)
    def test_scope_and_source_type_checks(self):
        for files in (['../outside.md'],['note.md','./note.md'],['source.py']):
            with self.assertRaises(Exception):self.tool.index_documents(self.root,files,self.db)
        (self.root/'.env.md').write_text('not a credential')
        with self.assertRaises(Exception):self.tool.index_documents(self.root,['.env.md'],self.db)
    def test_excerpt_never_silently_truncated(self):
        self.index();item=self.tool.search_index(self.db,'timeout')['results'][0]
        with self.assertRaises(Exception):self.tool.get_chunk(self.db,item['id'],max_chars=1)
    def test_totals_and_limit_are_not_inferred_from_page_length(self):
        self.index()
        result=self.tool.search_index(self.db,'timeout beta',match='any',limit=1)
        self.assertEqual(result['total'],2);self.assertTrue(result['truncated'])
        self.assertFalse(self.tool.search_index(self.db,'timeout',limit=1)['truncated'])
        for limit in (0,101,True):
            with self.assertRaises(Exception):self.tool.search_index(self.db,'timeout',limit=limit)
    def test_publish_failure_leaves_previous_index_readable(self):
        self.index();old=self.db.read_bytes();self.p.write_text('changed')
        with patch.object(self.tool.os,'replace',side_effect=OSError('synthetic publish failure')):
            with self.assertRaises(Exception):self.index(replace=True)
        self.assertEqual(self.db.read_bytes(),old)
    def test_build_failure_leaves_previous_index_readable(self):
        self.index();old=self.db.read_bytes()
        with patch.object(self.tool.sqlite3,'connect',side_effect=RuntimeError('synthetic build failure')):
            with self.assertRaises(Exception):self.index(replace=True)
        self.assertEqual(self.db.read_bytes(),old)
    def test_special_sources_refused_before_open(self):
        with patch.object(self.tool.os,'stat',return_value=SimpleNamespace(st_mode=0o010600,st_size=0)):
            with self.assertRaisesRegex(Exception,'regular file'):self.tool._read_source(self.p)
    def test_create_only_publish_does_not_overwrite_a_racing_writer(self):
        def racing_writer(source,target):
            Path(target).write_bytes(b'another writer')
            raise FileExistsError('synthetic race')
        with patch.object(self.tool.os,'link',side_effect=racing_writer):
            with self.assertRaises(Exception):self.index()
        self.assertEqual(self.db.read_bytes(),b'another writer')
    def test_protected_database_output_refused(self):
        with self.assertRaisesRegex(Exception,'protected output'):
            self.tool.index_documents(self.root,['note.md'],Path('D:/protected-data/new-index.sqlite'))


if __name__=='__main__':unittest.main()
