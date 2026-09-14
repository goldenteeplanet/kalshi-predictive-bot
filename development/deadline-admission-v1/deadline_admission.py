"""Bounded once-only deadline fixture; caller clocks/custody are not authenticated.

Callbacks must apply the absolute worker-start gate and external hard watchdog.
No provider, daemon, service, process-group or production launcher is provided.
"""
import hashlib,json,math,os,re
from datetime import datetime,timedelta,timezone
from pathlib import Path

def need(value,reason):
    if not value:raise ValueError(reason)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def pin(value):need(type(value) is str and re.fullmatch('[0-9a-f]{64}',value),'SHA256');return value
def utc(value):
    need(type(value) is datetime and value.tzinfo is not None and value.utcoffset()==timedelta(0),'EXPLICIT_UTC')
    return value.astimezone(timezone.utc)
def integer(value,reason,maximum=86400):
    need(type(value) is int and 0<=value<=maximum,reason);return value
def source_check(expected):
    pin(expected);raw=Path(__file__).read_bytes();need(len(raw)<=65536 and sha(raw)==expected,'SOURCE_PIN')
def sync_dir(folder):
    fd=os.open(folder,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)
def save(folder,name,value):
    raw=encode(value);need(len(raw)<=16384,'RECEIPT_BOUND')
    with (folder/name).open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    # Actual POSIX directory durability; unsupported filesystems refuse.
    sync_dir(folder)
    return raw
def remaining(now,cutoff,stop_grace,safety,maximum):
    return min(maximum,math.floor((cutoff-now).total_seconds())-stop_grace-safety)
def effective_runtime(now,cutoff,required_seconds,maximum_seconds,stop_grace_seconds,safety_margin_seconds):
    now,cutoff=map(utc,(now,cutoff))
    required=integer(required_seconds,'REQUIRED_WORK');maximum=integer(maximum_seconds,'MAX_RUNTIME')
    grace=integer(stop_grace_seconds,'STOP_GRACE',60);safety=integer(safety_margin_seconds,'SAFETY',60)
    need(0<required<=maximum and grace>0 and safety>0,'POSITIVE_TIME_BUDGET')
    runtime=remaining(now,cutoff,grace,safety,maximum)
    return dict(status='TIME_AVAILABLE' if runtime>=required else 'INSUFFICIENT_TIME',effective_runtime_seconds=max(0,runtime),required_seconds=required,cutoff=cutoff.isoformat(),clock_authenticated=False,hard_watchdog_enforced=False)

def dispatch_once(root,*,identity,source_sha256,input_raw,input_sha256,
                  declared_at,not_before,cutoff,required_work_seconds,
                  maximum_runtime_seconds,stop_grace_seconds,safety_seconds,
                  clock,submit):
    need(type(identity) is str and re.fullmatch('[a-z0-9][a-z0-9_-]{0,79}',identity),'IDENTITY')
    source_check(source_sha256);pin(input_sha256)
    need(type(input_raw) is bytes and len(input_raw)<=65536 and sha(input_raw)==input_sha256,'INPUT_PIN_OR_BOUND')
    declared_at,not_before,cutoff=map(utc,(declared_at,not_before,cutoff))
    need(declared_at<=not_before<cutoff,'DECLARED_WINDOW')
    required=integer(required_work_seconds,'REQUIRED_WORK');maximum=integer(maximum_runtime_seconds,'MAX_RUNTIME')
    grace=integer(stop_grace_seconds,'STOP_GRACE',60);safety=integer(safety_seconds,'SAFETY',60)
    need(0<required<=maximum and grace>0 and safety>0,'POSITIVE_TIME_BUDGET')
    root=Path(root).absolute();need(root.is_dir() and not any(p.is_symlink() for p in [root,*root.parents]),'CUSTODY_ROOT')
    folder=root/identity
    try:folder.mkdir()
    except FileExistsError:raise ValueError('IDENTITY_CONSUMED') from None
    sync_dir(root)
    # Exclusive namespace creation consumes even refusal or interrupted fsync.
    now=utc(clock())
    runtime=remaining(now,cutoff,grace,safety,maximum)
    intent=dict(schema='DEADLINE_DISPATCH_INTENT_V1',identity=identity,source_sha256=source_sha256,input_sha256=input_sha256,input_bytes=len(input_raw),declared_at=declared_at.isoformat(),not_before=not_before.isoformat(),cutoff=cutoff.isoformat(),observed_at=now.isoformat(),required_work_seconds=required,maximum_runtime_seconds=maximum,effective_runtime_seconds=runtime,stop_grace_seconds=grace,safety_seconds=safety,clock_authenticated=False,custody_authenticated=False)
    intent_raw=save(folder,'intent.json',intent)
    with (folder/'input.original').open('xb') as stream:stream.write(input_raw);stream.flush();os.fsync(stream.fileno())
    sync_dir(folder)
    second=utc(clock())
    allowed=(declared_at<=not_before<=now<=second<cutoff and runtime>=required and remaining(second,cutoff,grace,safety,maximum)>=runtime)
    if not allowed:
        save(folder,'refusal.json',dict(status='DEADLINE_ADMISSION_REFUSED',observed_at=second.isoformat(),callback_attempted=False,intent_sha256=sha(intent_raw)))
        raise ValueError('DEADLINE_ADMISSION_REFUSED')
    # Recheck source and retained originals immediately before the callback.
    source_check(source_sha256)
    need((folder/'intent.json').read_bytes()==intent_raw and (folder/'input.original').read_bytes()==input_raw,'PERSISTED_INTENT_CHANGED')
    third=utc(clock())
    if third<second or remaining(third,cutoff,grace,safety,maximum)<runtime:
        save(folder,'refusal.json',dict(status='DEADLINE_ADMISSION_REFUSED',observed_at=third.isoformat(),callback_attempted=False,intent_sha256=sha(intent_raw)))
        raise ValueError('DEADLINE_ADMISSION_REFUSED')
    try:result=submit(json.loads(intent_raw))
    except BaseException as error:
        save(folder,'submission-uncertain.json',dict(status='SUBMISSION_UNCERTAIN_IDENTITY_CONSUMED',error_type=type(error).__name__,intent_sha256=sha(intent_raw),retry_authorized=False))
        raise
    save(folder,'submission-returned.json',dict(status='CALLBACK_RETURNED_NOT_WORKER_COMPLETION',intent_sha256=sha(intent_raw),retry_authorized=False))
    return dict(status='CALLBACK_RETURNED_NOT_WORKER_COMPLETION',result=result,intent_sha256=sha(intent_raw),retry_authorized=False)

def worker_start(intent_raw,*,intent_sha256,expected_source_sha256,expected_input_sha256,clock,work):
    """SECOND absolute gate; never treats daemon submission time as start time."""
    need(type(intent_raw) is bytes and len(intent_raw)<=16384 and sha(intent_raw)==pin(intent_sha256),'INTENT_PIN')
    source_check(expected_source_sha256);pin(expected_input_sha256)
    def unique(items):
        result={}
        for k,v in items:need(k not in result,'DUPLICATE_JSON');result[k]=v
        return result
    i=json.loads(intent_raw,object_pairs_hook=unique)
    need(i['schema']=='DEADLINE_DISPATCH_INTENT_V1' and i['source_sha256']==expected_source_sha256 and i['input_sha256']==expected_input_sha256,'WORKER_CONTEXT')
    start=utc(clock());cutoff=utc(datetime.fromisoformat(i['cutoff']));earliest=utc(datetime.fromisoformat(i['not_before']));submitted=utc(datetime.fromisoformat(i['observed_at']))
    required=integer(i['required_work_seconds'],'REQUIRED_WORK');runtime=integer(i['effective_runtime_seconds'],'EFFECTIVE_RUNTIME');grace=integer(i['stop_grace_seconds'],'STOP_GRACE',60);safety=integer(i['safety_seconds'],'SAFETY',60)
    need(required>0 and runtime>=required and grace>0 and safety>0,'WORKER_TIME_BOUNDS')
    actual=remaining(start,cutoff,grace,safety,runtime)
    need(earliest<=submitted<=start<cutoff and actual>=required,'WORKER_START_TOO_LATE')
    # Work must be wrapped in a separately reviewed absolute hard watchdog.
    return work(dict(started_at=start.isoformat(),cutoff=cutoff.isoformat(),effective_runtime_seconds=actual,hard_watchdog_enforced=False))
