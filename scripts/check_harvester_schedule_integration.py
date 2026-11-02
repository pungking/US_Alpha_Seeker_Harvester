"""Offline integration checks: compile only isolated guards, never Harvester."""
import ast
import datetime as dt
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import harvester_schedule_contract as c
import run_harvester_schedule_gate as gate
from check_harvester_schedule_admission import MemoryTransport, NOW, REPO, HEAD, control_bytes
import harvester_schedule_receipts as receipts

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_collector_cancellation_condition(self):
        text=(ROOT/'.github/workflows/main.yml').read_text().split('  build:',1)[1]
        condition=text.split('    if: >-',1)[1].split('    env:',1)[0].strip()
        self.assertIn('!cancelled()',condition)
        self.assertNotIn('always()',condition)
        # Evaluate the actual small expression for admitted and skipped dependencies.
        for kind in ('legacy_daily','stage3','hybrid_daily'):
            for cancelled in (False,True):
                values={'needs.route.result':"'success'",'needs.route.outputs.kind':repr(kind),
                        'needs.reserve.result':"'success'",'needs.reserve.outputs.admitted':"'true'"}
                expression=condition.replace('!cancelled()',str(not cancelled)).replace('always()','True')
                for key,value in values.items(): expression=expression.replace(key,value)
                expression=expression.replace('&&',' and ').replace('||',' or ')
                self.assertEqual(eval(' '.join(expression.split()),{'__builtins__':{}},{}),not cancelled)

    def test_native_reserved_slot_never_rolls_forward(self):
        with tempfile.TemporaryDirectory() as temp:
            event=Path(temp)/'event.json'; event.write_bytes(c.encode({'schedule':c.CRONS['first']}))
            original=c.resolve_route('schedule',{'schedule':c.CRONS['first']},'refs/heads/main',1,NOW,NOW,'hybrid',REPO).slot
            env={'HARVESTER_SCHEDULE_MODE':'hybrid','GITHUB_EVENT_NAME':'schedule','GITHUB_EVENT_PATH':str(event),
                 'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/main','GITHUB_RUN_ID':'123',
                 'GITHUB_RUN_ATTEMPT':'1','GITHUB_SHA':HEAD,'HARVESTER_SCHEDULE_CONTROL_SHA256':c.digest(control_bytes()),
                 'RESOLVED_SLOT_JSON':c.encode(original.wire()).decode()}
            transport=MemoryTransport()
            with self.assertRaises(c.ContractError):
                gate.execute('reserve',env,lambda:c.parse_time('2026-10-30T22:20:00Z'),transport)
            self.assertEqual(transport.calls,[])
            self.assertEqual(gate.execute('reserve',env,lambda:NOW,transport)['admitted'],'true')

    def test_sec_completed_continuation_not_new_collector_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);env=self.bundle(root)
            summary_path=root/'summary.json'; env['HARVESTER_RUN_SUMMARY_PATH']=str(summary_path)
            started=c.collector_start_evidence(env,NOW)
            later=c.parse_time('2026-10-30T03:00:00Z')
            summary={'status':'success','mode':'daily','batchMode':'first','startedAt':c.stamp(NOW),
                     'completedAt':c.stamp(later),'scheduleAdmission':started}
            summary_path.write_bytes(c.encode(summary))
            self.assertEqual(c.assert_environment_start(env,later,completed_collector_required=True),'hybrid_daily')
            tree=ast.parse((ROOT/'harvester.py').read_text())
            guard=next(n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                       and getattr(n.value.func,'id',None)=='assert_schedule_start')
            auth=next(n for n in tree.body if isinstance(n,ast.Assign)
                      and any(getattr(t,'id',None)=='drive_service' for t in n.targets))
            calls=[]
            ns={'__name__':'harvester','get_drive_service':lambda:calls.append('auth'),
                'assert_schedule_start':lambda **kwargs:c.assert_environment_start(env,later,**kwargs)}
            exec(compile(ast.Module(body=[guard,auth],type_ignores=[]),'<sec-import>','exec'),ns)
            self.assertEqual(calls,['auth'])
            with self.assertRaises(c.ContractError): c.assert_environment_start(env,later)
            for key,value in [('status','failed'),('batchMode','second'),('startedAt','2026-10-29T00:00:00Z'),
                              ('scheduleAdmission',{'proofSha256':'0'*64,'startedAt':c.stamp(NOW)})]:
                summary_path.write_bytes(c.encode({**summary,key:value}))
                with self.assertRaises(c.ContractError): c.assert_environment_start(env,later,completed_collector_required=True)
            summary_path.unlink()
            with self.assertRaises(c.ContractError): c.assert_environment_start(env,later,completed_collector_required=True)
            with self.assertRaises(c.ContractError):
                exec(compile(ast.Module(body=[guard,auth],type_ignores=[]),'<sec-import>','exec'),ns)
            self.assertEqual(calls,['auth'])
        text=(ROOT/'.github/workflows/main.yml').read_text()
        sec=next(s for s in text.split('      - name: ') if s.startswith('Run scheduled Stage0 SEC'))
        self.assertIn('HARVESTER_SCHEDULE_PROOF_PATH:',sec)
        self.assertIn('DAILY_BATCH_MODE:',sec)
        source=(ROOT/'harvester.py').read_text()
        self.assertIn('completed_collector_required=__name__ != "__main__"',source)
        self.assertIn('"scheduleAdmission": schedule_admission',source)

    def test_importing_fixtures_do_not_inherit_live_admission(self):
        text=(ROOT/'.github/workflows/main.yml').read_text()
        for check in ('listing_lifecycle_contract','corporate_action_lineage_contract',
                      'earnings_event_coverage_contract','toss_same_stage3_handoff'):
            step=next(s for s in text.split('      - name: ') if f'python scripts/check_{check}.py' in s)
            self.assertIn('HARVESTER_SCHEDULE_MODE: legacy',step)
            self.assertIn('GITHUB_EVENT_NAME: offline_fixture',step)
            self.assertIn("GITHUB_EVENT_PATH: ''",step)
        collector=next(s for s in text.split('      - name: ') if s.startswith('Run Master Harvester'))
        self.assertNotIn('HARVESTER_SCHEDULE_MODE: legacy',collector)

    def test_ci_registration_and_runbook(self):
        ci=(ROOT/'.github/workflows/telegram-routing-ci.yml').read_text()
        for name in ('admission','integration'):
            self.assertIn(f'python scripts/check_harvester_schedule_{name}.py',ci)
        self.assertIn('python scripts/check_harvester_mac_assist.py',ci)
        self.assertIn('contents: read',ci)
        self.assertNotIn('contents: write',ci)
        main=(ROOT/'.github/workflows/main.yml').read_text()
        self.assertNotIn('pull_request:',main)
        runbook=(ROOT/'docs/ops/harvester-hybrid-schedule-runbook.md').read_text()
        for phrase in ('STATIC_READY_NOT_ACTIVATED','harvester-schedule-receipts',
                       'pending queue','gate-only rollback','no-delete','no-force',
                       'old-job drain','current-cutoff','manual auto/all','GET<=12'):
            self.assertIn(phrase,runbook)

    def test_assist_is_daily_not_stage3(self):
        source = (ROOT/'harvester.py').read_text()
        self.assertIn('GITHUB_EVENT_ACTION == "stage3_completed"', source)
        self.assertNotIn("if GITHUB_EVENT_NAME == 'repository_dispatch':", source)

    def test_only_stage3_preempts(self):
        text = (ROOT/'.github/workflows/main.yml').read_text()
        self.assertIn("github.event.action == 'stage3_completed'", text)
        self.assertIn('harvester_daily_slot_requested', text)
        self.assertIn('HARVESTER_SCHEDULE_MODE: legacy', text)
        self.assertIn('needs: [route, reserve]', text)
        self.assertIn("needs.reserve.outputs.admitted == 'true'", text)

    def test_job_dependency_skip_logic(self):
        text = (ROOT/'.github/workflows/main.yml').read_text()
        self.assertIn("needs.route.result == 'success'", text)
        self.assertIn("needs.route.outputs.kind != 'hybrid_daily'", text)
        self.assertIn('set -euo pipefail', text)
        self.assertIn('HARVESTER_SCHEDULE_PROOF_PATH:', text)
        self.assertIn('notion_phase:', text)

    def bundle(self, root):
        t = MemoryTransport()
        proof = receipts.reserve_slot(c.due_slot(NOW, REPO),c.digest(control_bytes()),"123",HEAD,lambda:NOW,t).proof
        path = root/'proof.json'
        gate.write_exclusive(path, c.encode({'proof': proof.wire(), 'control': control_bytes().decode()}))
        event = root/'event.json'; event.write_bytes(c.encode({'action': c.ASSIST_ACTION, 'client_payload': {
            'schemaVersion': c.REQUEST_SCHEMA, 'batch': 'first', 'scheduledFor': '2026-10-29T22:13:00Z'}}))
        env = {'HARVESTER_SCHEDULE_MODE': 'hybrid', 'HARVESTER_SCHEDULE_CONTROL_SHA256': c.digest(control_bytes()),
               'HARVESTER_SCHEDULE_PROOF_PATH': str(path), 'GITHUB_EVENT_PATH': str(event),
               'GITHUB_EVENT_NAME': 'repository_dispatch', 'GITHUB_REPOSITORY': REPO,
               'GITHUB_RUN_ID': '123', 'GITHUB_SHA': HEAD, 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_RUN_ATTEMPT': '1', 'DAILY_BATCH_MODE': 'first'}
        return env

    def test_dedupe_before_import_auth(self):
        tree = ast.parse((ROOT/'harvester.py').read_text())
        guard = next(n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                     and getattr(n.value.func,'id',None) == 'assert_schedule_start')
        auth = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t,'id',None)=='drive_service' for t in n.targets))
        self.assertLess(guard.lineno, auth.lineno)
        calls=[]
        ns={'__name__':'__main__','assert_schedule_start': lambda **kwargs: c.assert_environment_start({'HARVESTER_SCHEDULE_MODE':'hybrid'}, NOW, **kwargs),
            'get_drive_service':lambda: calls.append('auth')}
        with self.assertRaises(c.ContractError): exec(compile(ast.Module(body=[guard,auth],type_ignores=[]),'<guard>','exec'),ns)
        self.assertEqual(calls, [])

    def test_window_expires_during_install_and_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            env = self.bundle(Path(temp))
            self.assertEqual(c.assert_environment_start(env,NOW), 'hybrid_daily')
            calls=['auth']
            with self.assertRaises(c.ContractError): c.assert_environment_start(env,c.parse_time('2026-10-30T01:13:00Z'))
            self.assertEqual(calls, ['auth'])
            env['DAILY_BATCH_MODE']='second'
            with self.assertRaises(c.ContractError): c.assert_environment_start(env,NOW)

    def test_stage3_and_legacy_no_daily_proof(self):
        self.assertEqual(c.assert_environment_start({},NOW),'legacy_daily')
        with tempfile.TemporaryDirectory() as temp:
            event=Path(temp)/'event.json';event.write_text('{"action":"stage3_completed"}')
            env={'HARVESTER_SCHEDULE_MODE':'hybrid','GITHUB_EVENT_NAME':'repository_dispatch','GITHUB_EVENT_PATH':str(event)}
            self.assertEqual(c.assert_environment_start(env,NOW),'stage3')
            event.write_text('{"action":"unrecognized"}')
            with self.assertRaises(c.ContractError): c.assert_environment_start(env,NOW)

    def test_proof_file_exclusive_and_no_private_error(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'proof';gate.write_exclusive(p,b'first')
            with self.assertRaises(c.ContractError): gate.write_exclusive(p,b'second')
            self.assertEqual(p.read_bytes(),b'first');self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_sec_native_assist_parity_and_dst(self):
        from stage0_financial_publication_lineage_producer import recurring_collection_key
        from zoneinfo import ZoneInfo
        for timestamp,date in [('2026-10-29T03:40:00Z','2026-10-28'),('2026-10-29T11:40:00Z','2026-10-29'),
                               ('2026-11-03T04:40:00Z','2026-11-02'),('2026-11-03T05:10:00Z','2026-11-03')]:
            window=c.parse_time(timestamp).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            self.assertEqual(window,date);self.assertEqual(recurring_collection_key(window),recurring_collection_key(date))
        text=(ROOT/'.github/workflows/main.yml').read_text()
        self.assertIn('TZ=America/New_York date +%F',text)
        self.assertNotIn('TZ=Asia/Seoul date +%F',text)
        self.assertIn('needs.route.outputs.sec_enabled',text)


if __name__ == '__main__':
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK_FORBIDDEN')):
        unittest.main()
