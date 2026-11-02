"""No real dispatch, launchctl, keychain or provider access."""
import datetime as dt
from email.utils import format_datetime
import json
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import harvester_schedule_contract as c
import harvester_schedule_receipts as r
import dispatch_harvester_daily_slot as sender
from check_harvester_schedule_admission import MemoryTransport, NOW, REPO, control_bytes

ROOT=Path(__file__).resolve().parents[1]
CONFIG={'repository':REPO,'expectedControlSha256':c.digest(control_bytes()),'timezone':'Asia/Seoul'}


class SenderTransport(MemoryTransport):
    def __init__(self):
        super().__init__(); self.date=format_datetime(NOW,usegmt=True); self.fail_post=False

    def request(self,method,path,body=None):
        if method=='POST':
            self.calls.append((method,path))
            if self.fail_post: raise c.ContractError('TRANSPORT_UNCERTAIN')
            return r.HttpResult(204,{},b'')
        result=super().request(method,path,body)
        return r.HttpResult(result.status,{'date':self.date},result.body_bytes)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.t=SenderTransport()
        self.clock=patch.object(sender,'host_timezone_is_kst',return_value=True);self.clock.start();self.addCleanup(self.clock.stop)

    def run_sender(self,send=True,now=NOW,config=None):
        return sender.dispatch_due_slot(config or CONFIG,send,lambda:now,self.t,self.root)

    def test_default_zero_network(self):
        self.assertEqual(self.run_sender(False).status,'VALIDATION_ONLY')
        self.assertEqual(self.t.calls,[]);self.assertEqual(list(self.root.iterdir()),[])

    def test_mac_due_and_sleep_login_converge(self):
        self.assertEqual(self.run_sender().status,'DISPATCH_ACCEPTED_NOT_COLLECTION_PROOF')
        with self.assertRaises(c.ContractError): self.run_sender()
        self.assertEqual([m for m,_ in self.t.calls].count('POST'),1)
        self.assertEqual(len(list(self.root.iterdir())),1)

    def test_mac_due_at_0800(self):
        when=c.parse_time('2026-10-29T23:00:00Z')
        self.t.date=format_datetime(when,usegmt=True)
        self.assertEqual(self.run_sender(now=when).status,'DISPATCH_ACCEPTED_NOT_COLLECTION_PROOF')
        attempt=c.decode(next(self.root.iterdir()).read_bytes())
        self.assertEqual(attempt['slot']['batch'],'first')

    def test_offline_get_no_post(self):
        with patch.object(self.t,'request',side_effect=c.ContractError('TRANSPORT_UNCERTAIN')):
            with self.assertRaises(c.ContractError): self.run_sender()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_clock_reversal_and_cached_response(self):
        moments=iter([NOW,NOW-dt.timedelta(seconds=1)])
        with self.assertRaises(c.ContractError): sender.checked_get(self.t,'/fixture',lambda:next(moments))
        response=r.HttpResult(200,{'date':format_datetime(NOW,usegmt=True),'age':'1'},b'{}')
        with patch.object(self.t,'request',return_value=response):
            with self.assertRaises(c.ContractError): sender.checked_get(self.t,'/fixture',lambda:NOW)

    def test_late_wake_no_backfill(self):
        for when in ('2026-10-30T01:13:00Z','2026-10-30T06:13:00Z','2026-11-01T00:00:00Z'):
            self.assertEqual(self.run_sender(now=c.parse_time(when)).status,'NO_OPEN_SLOT')
        self.assertEqual(self.t.calls,[])

    def test_offline_and_unknown_post_no_retry(self):
        self.t.fail_post=True
        with self.assertRaises(c.ContractError): self.run_sender()
        with self.assertRaises(c.ContractError): self.run_sender()
        self.assertEqual([m for m,_ in self.t.calls].count('POST'),1)

    def test_timezone_clock_rejected(self):
        for date in ('', 'invalid', format_datetime(NOW+dt.timedelta(seconds=1),usegmt=True),format_datetime(NOW-dt.timedelta(seconds=1),usegmt=True)):
            self.t.date=date
            with self.assertRaises(c.ContractError): self.run_sender()
        self.assertNotIn('POST',[m for m,_ in self.t.calls])
        with patch.object(sender,'host_timezone_is_kst',return_value=False):
            with self.assertRaises(c.ContractError): self.run_sender()

    def test_symlink_receipt_dir_rejected(self):
        link=self.root/'link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(c.ContractError): sender.reserve_local_attempt(link,c.due_slot(NOW,REPO),'a'*64)
        self.root.chmod(0o755)
        with self.assertRaises(c.ContractError): self.run_sender()

    def test_two_processes_one_local_attempt(self):
        code="""
import sys
from pathlib import Path
import harvester_schedule_contract as c
from dispatch_harvester_daily_slot import reserve_local_attempt
try:
    reserve_local_attempt(Path(sys.argv[1]),c.due_slot(c.parse_time(sys.argv[2]),sys.argv[3]),'a'*64)
except c.ContractError:
    sys.exit(1)
"""
        args=[sys.executable,'-c',code,str(self.root),c.stamp(NOW),REPO]
        workers=[subprocess.Popen(args,cwd=ROOT/'scripts',stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        for worker in workers:
            out,err=worker.communicate(timeout=15)
            self.assertEqual(out+err,b'')
        self.assertEqual(sorted(w.returncode for w in workers),[0,1])
        p=next(self.root.iterdir());self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_missing_secret_before_network(self):
        with self.assertRaises(c.ContractError): sender.dispatch_due_slot(CONFIG,True,lambda:NOW,None,self.root)
        self.assertEqual(self.t.calls,[])

    def test_existing_remote_receipt_no_dispatch(self):
        self.t.files[c.due_slot(NOW,REPO).attempt_path]=b'corrupt'
        self.assertEqual(self.run_sender().status,'REMOTE_ATTEMPT_PRESERVED')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_plist_inert_no_keepalive(self):
        p=ROOT/'docs/ops/harvester-schedule-mac-assist.launchagent.plist.example'
        data=plistlib.loads(p.read_bytes())
        self.assertNotIn('--send',data['ProgramArguments']);self.assertNotIn('KeepAlive',data)
        self.assertEqual(len(data['StartCalendarInterval']),10)
        self.assertTrue(data['RunAtLoad'])


if __name__=='__main__':
    with patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK_FORBIDDEN')):
        unittest.main()
