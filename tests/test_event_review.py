"""Offline end-to-end tests for audited sampled-change events, no external RPC."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'engine'))
from scripts import event_review as ev, proxy_monitor as pm
from scripts.refresh_site import build_outputs, write_outputs
from tests.test_proxy_monitor import mock_snapshot, new_site, Transport, word, IMPL, ADMIN
from stock_integrity import ASSETS
from equity_lens import EvidenceError


def prepared(root, *, change=False, admin_change=False, implementation_change=False, only_one=False):
    old,new=new_site(root)
    x=pm.collect(new,transport=Transport(block=101, hash_char='b'))
    pm.refresh_proxy(root,collector=lambda _:x)
    if only_one:return
    following=mock_snapshot(102,minutes=0,mark='c')
    if change:
        a=following['assets'][0]
        a['total_supply_raw']=str(int(a['total_supply_raw'])+500000)
        a['token_units_reported']='1.5'
    write_outputs(root,build_outputs([old,new,following]))
    slots={}
    if admin_change: slots[(ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['admin'])]=word(ADMIN)
    if implementation_change: slots[(ASSETS['AAPLc'].lower(),pm.ERC1967_SLOTS['implementation'])]=word(IMPL)
    y=pm.collect(following,transport=Transport(block=102,hash_char='c',slots=slots))
    pm.refresh_proxy(root,collector=lambda _:y)


class EventReviewTests(unittest.TestCase):
    def test_baseline_proxy_no_alert(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,only_one=True)
            result=ev.generate(p,preview=True)
            self.assertEqual(result['event_count'],0)
            self.assertFalse(result['written'])
    def test_existing_main_base_changes_without_proxy_are_not_false_positive(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);new_site(p)
            with self.assertRaisesRegex(EvidenceError,'PROXY_HISTORY_NOT_READY'):
                ev.generate(p,preview=True)
    def test_supply_increase_and_admin_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True,admin_change=True)
            core,proxy,dig=ev.inputs(p);report=ev.build(core,proxy,dig)
            kinds={x['kind'] for x in report['events']}
            self.assertIn('REPORTED_SUPPLY_INCREASED',kinds)
            self.assertIn('ERC1967_ADMIN_SLOT_CHANGED',kinds)
            self.assertEqual(report['proxy_latest_block'],102)
            supply=next(x for x in report['events'] if x['kind']=='REPORTED_SUPPLY_INCREASED')
            self.assertEqual(supply['previous']['units'],'1')
            self.assertEqual(supply['current']['units'],'1.5')
            self.assertEqual(supply['from_block'],101)
            self.assertEqual(supply['to_block'],102)
            self.assertIn('does not prove',supply['meaning'])
    def test_supply_decrease_from_zero_and_no_percent_divide(self):
        x=mock_snapshot(100,minutes=5,mark='a');y=mock_snapshot(101,minutes=1,mark='b')
        x['assets'][0]['total_supply_raw']='0';x['assets'][0]['token_units_reported']='0'
        found=ev.supply_events([x,y]);self.assertIn('REPORTED_SUPPLY_INCREASED',[e['kind'] for e in found])
        found=ev.supply_events([y,x | {'block':{'number':102,'hash':'0x'+'c'*64}}])
        self.assertIn('REPORTED_SUPPLY_DECREASED',[e['kind'] for e in found])
    def test_decimals_change_skips_misleading_unit_delta(self):
        x=mock_snapshot(100,minutes=5,mark='a');y=mock_snapshot(101,minutes=1,mark='b')
        y['assets'][0]['decimals_reported']=8
        y['assets'][0]['total_supply_raw']='111000000'
        y['assets'][0]['token_units_reported']='1.11'
        events=ev.supply_events([x,y])
        self.assertIn('DECIMALS_CHANGED',[e['kind'] for e in events])
        self.assertNotIn('REPORTED_SUPPLY_INCREASED',[e['kind'] for e in events])
    def test_implementation_target_changes_observed_not_privileges(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,implementation_change=True)
            core,proxy,dig=ev.inputs(p);report=ev.build(core,proxy,dig)
            self.assertIn('IMPLEMENTATION_TARGET_CHANGED',[e['kind'] for e in report['events']])
            self.assertTrue(all('privilege' not in e['label'].lower() for e in report['events']))
    def test_zero_change_produces_non_alarm_honest_message(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p)
            result=ev.generate(p)
            self.assertEqual(result['event_count'],0)
            self.assertIn('değişiklik bulunmadı',(p/'index.html').read_text())
            self.assertIn('güvenli olduğunu kanıtlamaz',(p/'index.html').read_text())
    def test_public_artifact_written_and_verified(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True)
            result=ev.generate(p)
            self.assertTrue(result['written'])
            self.assertEqual(ev.generate(p,check_only=True)['status'],'CURRENT_VERIFIED')
            self.assertIn('id="equity-alerts-v1"',(p/'index.html').read_text())
            dct=json.loads((p/'data/alerts.json').read_text())
            self.assertEqual(dct['event_count'],1)
            self.assertEqual(dct['chain_id'],8453)
    def test_repeat_is_idempotent_and_sha_stable(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True)
            ev.generate(p)
            original=(p/'index.html').read_bytes()
            digest=hashlib.sha256((p/'data/alerts.json').read_bytes()).hexdigest()
            self.assertEqual(ev.generate(p)['mode'],'ALERTS_ALREADY_CURRENT')
            self.assertEqual((p/'index.html').read_bytes(),original)
            self.assertEqual(hashlib.sha256((p/'data/alerts.json').read_bytes()).hexdigest(),digest)
    def test_mutated_alert_json_hash_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True);ev.generate(p)
            (p/'data/alerts.json').write_text('{}')
            with self.assertRaisesRegex(EvidenceError,'ALERT_MANIFEST_HASH_MISMATCH'):
                ev.generate(p,check_only=True)
    def test_mutated_history_without_updated_manifest_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True);ev.generate(p)
            page=(p/'index.html').read_bytes()
            with (p/'data/history.json').open('a') as f:f.write('\n')
            with self.assertRaises(EvidenceError):ev.generate(p)
            self.assertEqual((p/'index.html').read_bytes(),page)
    def test_missing_partial_alert_artifacts_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p);ev.generate(p)
            (p/'data/alerts_manifest.json').unlink()
            with self.assertRaisesRegex(EvidenceError,'PARTIAL_ALERT_PUBLICATION'):
                ev.generate(p,check_only=True)
    def test_check_only_has_no_writes_and_no_network(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p)
            original=(p/'index.html').read_bytes()
            with patch.object(pm,'rpc',side_effect=AssertionError('No RPC')):
                self.assertFalse(ev.generate(p,check_only=True)['written'])
            self.assertEqual((p/'index.html').read_bytes(),original)
    def test_invalid_existing_html_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p)
            page=(p/'index.html').read_text().replace('</main>','</main></main>')
            (p/'index.html').write_text(page)
            with self.assertRaisesRegex(EvidenceError,'INVALID_EXISTING_HTML_WRAPPER'):
                ev.generate(p)
            self.assertFalse((p/'data/alerts.json').exists())
    def test_html_escapes_untrusted_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True)
            core,proxy,dig=ev.inputs(p);report=ev.build(core,proxy,dig)
            report['events'][0]['meaning']='<img src=x onerror=alert(1)>'
            rendered=ev.render_section(report)
            self.assertNotIn('<img',rendered)
            self.assertIn('&lt;img',rendered)
    def test_source_schema_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p)
            a=json.loads((p/'data/proxy_history.json').read_text())
            a['chain_id']=1
            (p/'data/proxy_history.json').write_text(json.dumps(a))
            with self.assertRaises(EvidenceError):ev.generate(p,preview=True)
    def test_fixed_ids_for_same_values(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True)
            core,proxy,dig=ev.inputs(p)
            x,y=ev.build(core,proxy,dig),ev.build(core,proxy,dig)
            self.assertEqual(x,y)
            self.assertEqual(ev.jbytes(x),ev.jbytes(y))
    def test_more_than_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);prepared(p,change=True)
            core, proxies, digests=ev.inputs(p)
            with patch.object(ev,'MAX_ALERTS',0):
                with self.assertRaisesRegex(EvidenceError,'EXCESSIVE_EVENTS'):
                    ev.build(core,proxies,digests)
    def test_patch_installed_and_repeated(self):
        from apply_alert_patch import plan
        self.assertIsNone(plan(ROOT))
        text=(ROOT/'.github/workflows/equity-evidence-pages.yml').read_text()
        self.assertLess(text.index('run: python scripts/event_review.py'),text.index('name: Save complete history'))
        self.assertIn('cp data/alerts.json data/alerts_manifest.json _site/data/',text)
    def test_patch_unknown_workflow_stops(self):
        from apply_alert_patch import plan
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'.github/workflows').mkdir(parents=True)
            (p/'scripts').mkdir()
            (p/'scripts/proxy_monitor.py').write_text('')
            (p/'scripts/refresh_site.py').write_text('')
            (p/'.github/workflows/equity-evidence-pages.yml').write_text('on: always')
            with self.assertRaisesRegex(RuntimeError,'UNRECOGNIZED_WORKFLOW_LAYOUT'):
                plan(p)

if __name__ == '__main__':unittest.main()
