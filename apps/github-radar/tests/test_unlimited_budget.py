"""Unlimited cost policy retains accounting; no network/model calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import radar
from radar_store import Store
from analysis_queue import pause_reason
from reports import render

class UnlimitedBudgetTests(unittest.TestCase):
    def test_unlimited_still_records_unknown_reservations(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            try:
                self.assertTrue(store.reserve('a','2026-10-17',100000000000,None))
                self.assertTrue(store.reserve('b','2026-10-17',100000000000,None))
                self.assertEqual(store.budget('2026-10-17')['charged_micro'],200000000000)
            finally:store.close()
    def test_optional_explicit_cap_still_enforced(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            try:
                self.assertTrue(store.reserve('a','2026-10-17',80,100))
                self.assertFalse(store.reserve('b','2026-10-17',21,100))
            finally:store.close()
    def test_null_config_is_supported_and_missing_is_not(self):
        cfg=radar.config();self.assertIsNone(cfg['daily_budget_cny'])
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.json';del cfg['daily_budget_cny'];p.write_text(json.dumps(cfg),encoding='utf-8')
            with self.assertRaises(ValueError):radar.config(p)
    def test_previous_budget_pause_clears_when_cap_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            try:
                store.put('analysis-pause',{'kind':'budget','day':'2026-10-17','reason':'old cap'})
                self.assertIsNone(pause_reason(store,{'daily_budget_cny':None},datetime(2026,10,17,tzinfo=timezone.utc)))
            finally:store.close()
    def test_report_renders_unlimited(self):
        run=dict(local_time='test',status='done',discovered=0,evidence_count=0,analyzed=0,cache_hits=0,daily_budget=None,errors=[])
        markdown,_=render(run,[],dict(charged_micro=200000000,unknown_count=1))
        self.assertIn('不限额',markdown);self.assertIn('200.0000',markdown)

if __name__=='__main__':unittest.main()
