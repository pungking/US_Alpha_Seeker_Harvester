"""Optional wake sender; validation-only unless explicitly invoked with --send."""
from __future__ import annotations

import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping

import harvester_schedule_contract as c
import harvester_schedule_receipts as r


def host_timezone_is_kst() -> bool:
    return dt.datetime.now().astimezone().utcoffset() == dt.timedelta(hours=9)


def validate_directory(path: Path) -> None:
    try:
        c.require(path.is_absolute() and path.resolve(strict=True) == path, 'REAL_ABSOLUTE_DIRECTORY_REQUIRED')
        info=path.lstat()
        c.require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and info.st_mode&0o077==0,
                  'OWNER_ONLY_DIRECTORY_REQUIRED')
    except OSError as exc:
        raise c.ContractError('DIRECTORY_UNAVAILABLE') from exc


def reserve_local_attempt(receipt_dir: Path, slot: c.Slot, payload_sha256: str) -> bool:
    validate_directory(receipt_dir)
    c.require(c.valid_hash(payload_sha256),'INVALID_PAYLOAD_HASH')
    directory=os.open(receipt_dir,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(directory)
        c.require(info.st_uid==os.getuid() and info.st_mode&0o077==0,'OWNER_ONLY_DIRECTORY_REQUIRED')
        fd=os.open(slot.slot_sha256+'.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as stream:
            stream.write(c.encode({'schemaVersion':'harvester-local-dispatch-attempt.v1',
                                   'slot':slot.wire(),'payloadSha256':payload_sha256,'status':'RESERVED_DO_NOT_RETRY'}))
            stream.flush();os.fsync(stream.fileno())
        os.fsync(directory)
    except OSError as exc:
        raise c.ContractError('LOCAL_ATTEMPT_EXISTS_OR_UNAVAILABLE') from exc
    finally:
        os.close(directory)
    return True


def checked_get(transport: r.GitHubTransport,path: str,now: Callable[[],dt.datetime]) -> r.HttpResult:
    start=now();c.stamp(start)
    response=transport.request('GET',path)
    end=now();c.stamp(end)
    c.require(end>=start,'LOCAL_CLOCK_REVERSED')
    try:
        server=parsedate_to_datetime(response.headers.get('date',''))
        c.require(server.utcoffset() is not None,'HTTP_DATE_UNAVAILABLE')
    except (ValueError,TypeError,OverflowError) as exc:
        raise c.ContractError('HTTP_DATE_UNAVAILABLE') from exc
    c.require(response.headers.get('age','0')=='0' and
              int(start.timestamp())<=int(server.timestamp())<=int(end.timestamp()),'LOCAL_CLOCK_NOT_PROVEN')
    return response


def dispatch_due_slot(config: Mapping[str,Any],send: bool,now: Callable[[],dt.datetime],
                      transport: r.GitHubTransport | None,receipt_dir: Path) -> c.GateResult:
    c.exact(config,{'repository','expectedControlSha256','timezone'})
    c.valid_repo(config['repository'])
    c.require(config['timezone']=='Asia/Seoul' and c.valid_hash(config['expectedControlSha256']), 'INVALID_CONFIG')
    validate_directory(receipt_dir)
    if not send:
        return c.GateResult('VALIDATION_ONLY')
    c.require(host_timezone_is_kst(),'HOST_TIMEZONE_MISMATCH')
    slot=c.due_slot(now(),config['repository'])
    if slot is None:
        return c.GateResult('NO_OPEN_SLOT')
    c.require(transport is not None and transport.repository==slot.repository,'CREDENTIAL_OR_REPOSITORY_MISSING')
    c.require(not os.path.lexists(receipt_dir/(slot.slot_sha256+'.json')),'LOCAL_ATTEMPT_PRESERVED')
    raw=r.content_bytes(checked_get(transport,r.content_path(slot.repository,'control.json'),now))
    control=c.validate_control(raw,config['expectedControlSha256'],slot.repository)
    c.require(slot.scheduled_for>=control.activation_at,'PRE_ACTIVATION_SLOT')
    existing=checked_get(transport,r.content_path(slot.repository,slot.attempt_path),now)
    if existing.status==200:
        return c.GateResult('REMOTE_ATTEMPT_PRESERVED')
    c.require(existing.status==404,'REMOTE_ATTEMPT_UNCERTAIN')
    payload={'event_type':c.ASSIST_ACTION,'client_payload':{'schemaVersion':c.REQUEST_SCHEMA,
             'scheduledFor':c.stamp(slot.scheduled_for),'batch':slot.batch}}
    reserve_local_attempt(receipt_dir,slot,c.digest(c.encode(payload)))
    c.require(c.slot_is_open(slot,now()),'SLOT_EXPIRED_ATTEMPT_PRESERVED')
    result=transport.request('POST',f'/repos/{slot.repository}/dispatches',payload)
    c.require(result.status==204,'DISPATCH_UNCONFIRMED_NO_RETRY')
    return c.GateResult('DISPATCH_ACCEPTED_NOT_COLLECTION_PROOF')


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True);parser.add_argument('--receipt-dir',required=True)
    parser.add_argument('--send',action='store_true',default=False)
    args=parser.parse_args();transport=None
    try:
        config=c.read_json_file(args.config,private=True)
        if args.send:
            transport=r.GitHubTransport(config.get('repository',''),os.environ.get('GH_TOKEN',''),'sender')
        result=dispatch_due_slot(config,args.send,lambda:dt.datetime.now(c.UTC),transport,Path(args.receipt_dir))
        print(c.encode({'status':result.status,'requestCounts':transport.counts if transport else {'GET':0,'POST':0,'PUT':0}}).decode())
        return 0
    except (c.ContractError,OSError,ValueError,TypeError,KeyError,ImportError):
        print(c.encode({'status':'MAC_ASSIST_BLOCKED','requestCounts':transport.counts if transport else {'GET':0,'POST':0,'PUT':0}}).decode())
        return 1


if __name__=='__main__':
    raise SystemExit(main())
