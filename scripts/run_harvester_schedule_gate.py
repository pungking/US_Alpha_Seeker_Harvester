"""Actions admission CLI. No collector import and no provider credentials."""
from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
from typing import Any, Mapping

import harvester_schedule_contract as c
import harvester_schedule_receipts as r


def write_exclusive(path: Path, raw: bytes) -> None:
    try:
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except OSError as exc:
        raise c.ContractError("OUTPUT_EXISTS_OR_UNAVAILABLE") from exc


def environment_route(env: Mapping[str, str], now: dt.datetime, transport: r.GitHubTransport | None = None) -> c.Route:
    mode = env.get('HARVESTER_SCHEDULE_MODE', 'legacy')
    event = c.environment_event(env)
    name = env.get('GITHUB_EVENT_NAME', '')
    created = now
    attempt = env.get('GITHUB_RUN_ATTEMPT', '1')
    c.require(attempt.isdecimal(), 'INVALID_RUN_ATTEMPT')
    if mode == 'hybrid' and not c.is_stage3_event(name, event) and transport is not None:
        data = r.read_run_metadata(transport, env.get('GITHUB_REPOSITORY',''), env.get('GITHUB_RUN_ID',''), env.get('GITHUB_SHA',''))
        c.require(data.get('event') == name, 'RUN_EVENT_MISMATCH')
        created = c.parse_time(data['created_at'])
    return c.resolve_route(name, event, env.get('GITHUB_REF',''), int(attempt), created, now, mode,
                           env.get('GITHUB_REPOSITORY',''))


def proof_from_environment(env: Mapping[str, str], now: dt.datetime, *, finishing: bool = False) -> c.AdmissionProof:
    value = c.decode(env.get('PROOF_JSON','').encode())
    slot = c.slot_from_wire(value.get('slot',{}), env.get('GITHUB_REPOSITORY',''))
    control = c.Control(slot.repository, slot.scheduled_for, env.get('HARVESTER_SCHEDULE_CONTROL_SHA256',''))
    return c.validate_proof(c.encode(value), control, env.get('GITHUB_RUN_ID',''), env.get('GITHUB_SHA',''), now,
                            allow_expired=finishing)


def emit_outputs(values: Mapping[str, Any], env: Mapping[str, str]) -> None:
    path = env.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf8') as stream:
            for key, value in values.items():
                encoded = c.encode(value).decode() if isinstance(value, (dict,list)) else str(value)
                c.require('\n' not in encoded and '\r' not in encoded, 'OUTPUT_NEWLINE_REJECTED')
                stream.write(f'{key}={encoded}\n')


def execute(phase: str, env: Mapping[str, str], now: Any, transport: r.GitHubTransport | None = None) -> dict[str, Any]:
    if phase == 'resolve':
        route = environment_route(env, now(), transport)
        return {'kind':route.kind, 'batch':route.slot.batch if route.slot else '',
                'sec_enabled':str(c.sec_phase_allowed(route, True, env.get('SEC_RECURRING_ENABLED')=='true')).lower()}
    c.require(env.get('HARVESTER_SCHEDULE_MODE') == 'hybrid' and transport is not None, 'HYBRID_REQUIRED')
    if phase == 'reserve':
        route = environment_route(env, now())
        c.require(route.kind == 'hybrid_daily', 'DAILY_ROUTE_REQUIRED')
        result = r.reserve_slot(route.slot, env.get('HARVESTER_SCHEDULE_CONTROL_SHA256',''), env.get('GITHUB_RUN_ID',''),
                                env.get('GITHUB_SHA',''), now, transport)
        return {'admitted':'true', 'proof_json':result.proof.wire(), 'status':result.status}
    proof = proof_from_environment(env, now(), finishing=phase=='finish')
    if phase == 'recheck':
        raw_control = r.verify_attempt(proof, transport, now())
        c.require(c.slot_is_open(proof.slot,now()),'SLOT_EXPIRED')
        path = Path(env.get('HARVESTER_SCHEDULE_PROOF_PATH',''))
        c.require(path.is_absolute(), 'ABSOLUTE_OUTPUT_REQUIRED')
        write_exclusive(path,c.encode({'proof':proof.wire(),'control':raw_control.decode()}))
        c.assert_environment_start(env,now())
        return {'admitted':'true','status':'ADMISSION_RECHECKED'}
    c.require(phase == 'finish','INVALID_PHASE')
    outcome = {key:env.get(variable) or 'unknown' for key,variable in
               [('collectorExit','COLLECTOR_OUTCOME'),('workflowJobResult','COLLECTOR_JOB_RESULT'),
                ('secPhase','SEC_OUTCOME'),('notionPhase','NOTION_OUTCOME')]}
    result = r.finish_slot(proof,outcome,now,transport)
    return {'status':result.status}


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['resolve','reserve','recheck','finish'])
    args=parser.parse_args()
    env=os.environ
    transport=None
    try:
        event = c.environment_event(env)
        requires_network = args.phase != 'resolve' or (env.get('HARVESTER_SCHEDULE_MODE')=='hybrid'
                               and not c.is_stage3_event(env.get('GITHUB_EVENT_NAME',''),event))
        if requires_network:
            transport=r.GitHubTransport(env.get('GITHUB_REPOSITORY',''),env.get('GH_TOKEN',''),args.phase)
        values=execute(args.phase,env,lambda:dt.datetime.now(c.UTC),transport)
        emit_outputs(values,env)
        print(c.encode({'status':values.get('status','ROUTE_RESOLVED'),'kind':values.get('kind'),
                        'requestCounts':transport.counts if transport else {'GET':0,'PUT':0,'POST':0}}).decode())
        return 0
    except (c.ContractError,OSError,TypeError,ValueError,KeyError,ImportError):
        print(c.encode({'status':'SCHEDULE_GATE_BLOCKED','phase':args.phase,
                        'requestCounts':transport.counts if transport else {'GET':0,'PUT':0,'POST':0}}).decode())
        return 1


if __name__=='__main__':
    raise SystemExit(main())
