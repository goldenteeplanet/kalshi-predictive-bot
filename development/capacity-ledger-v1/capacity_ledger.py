"""Cooperating-caller SQLite accounting only: no launcher, quota, or release."""
import hashlib,json,sqlite3,time,stat
from pathlib import Path
from contextlib import contextmanager
import capacity_plan as P

PLANNER_PIN='b3f233b37b13433ae8bee010dcf8d05dfe630614d24caa392556926c12868c8a'
if hashlib.sha256(Path(P.__file__).read_bytes()).hexdigest()!=PLANNER_PIN:raise ValueError('PLANNER_SOURCE_PIN')
POLICY_FIELDS={'filesystem','epoch','observer_source_sha256','source_manifest_sha256','max_age_seconds','protected_margin_bytes','max_rows','max_evidence_bytes','max_database_bytes','lock_timeout_ms','transaction_seconds'}
REQUEST_FIELDS={'reservation','run','phase','filesystem','epoch','source_manifest_sha256','policy_sha256','expected_generation','pages','phases','decision_at','observation_sha256'}
OBS_FIELDS={'schema','filesystem','available_bytes','sample_start','sample_end','elapsed_ms','observer_source_sha256','raw_evidence_sha256'}
def need(ok,why):
    if not ok:raise ValueError(why)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def pin(value):need(type(value) is str and len(value)==64 and all(x in '0123456789abcdef' for x in value),'SHA256');return value
def parse(raw,cap):
    need(type(raw) is bytes and 0<len(raw)<=cap,'EVIDENCE_BYTE_BOUND')
    def unique(items):
        result={}
        for k,v in items:need(k not in result,'DUPLICATE_JSON');result[k]=v
        return result
    value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('NONFINITE_JSON')))
    need(type(value) is dict,'JSON_OBJECT');return value
def policy(raw,expected):
    need(type(raw) is bytes and 0<len(raw)<=8192,'POLICY_BYTE_BOUND')
    pin(expected);need(sha(raw)==expected,'POLICY_PIN');p=parse(raw,8192);need(set(p)==POLICY_FIELDS,'POLICY_FIELDS')
    P.identity(p['filesystem']);P.identity(p['epoch']);pin(p['observer_source_sha256']);pin(p['source_manifest_sha256'])
    for key,maximum in [('max_age_seconds',60),('max_rows',128),('max_evidence_bytes',16384),('max_database_bytes',4194304),('lock_timeout_ms',1000),('transaction_seconds',5)]:
        P.integer(p[key],key,maximum);need(p[key]>0,'POSITIVE_POLICY_BOUND')
    need(p['max_database_bytes']>=65536 and p['max_database_bytes']%4096==0,'DATABASE_PAGE_BOUND')
    P.integer(p['protected_margin_bytes'],'MARGIN');return p
def filecheck(path,maximum):
    path=Path(path).absolute();need(not any(x.is_symlink() for x in [path,*path.parents]),'SYMLINK')
    s=path.stat();need(stat.S_ISREG(s.st_mode) and s.st_size<=maximum,'DATABASE_FILE_BOUND');return path

def create(path,*,policy_raw,policy_sha256):
    p=policy(policy_raw,policy_sha256);path=Path(path).absolute()
    need(not any(x.is_symlink() for x in [path,*path.parents]),'SYMLINK')
    # Exclusive marker consumes failed creation too; never reuse it after error.
    with path.open('xb'):pass
    db=sqlite3.connect(path,isolation_level=None)
    try:
        db.execute('PRAGMA page_size=4096');db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA max_page_count='+str(p['max_database_bytes']//4096))
        db.executescript('BEGIN IMMEDIATE; CREATE TABLE config(policy BLOB NOT NULL,pin TEXT NOT NULL); CREATE TABLE head(generation INTEGER NOT NULL); CREATE TABLE reservations(id TEXT PRIMARY KEY,run TEXT NOT NULL,phase TEXT NOT NULL,input_pin TEXT NOT NULL,request BLOB NOT NULL,observation BLOB NOT NULL,receipt BLOB NOT NULL,bytes INTEGER NOT NULL,state TEXT NOT NULL,UNIQUE(run,phase));')
        db.execute('INSERT INTO config VALUES(?,?)',(policy_raw,policy_sha256));db.execute('INSERT INTO head VALUES(0)');db.commit()
    finally:db.close()

@contextmanager
def connection(path,policy_raw,policy_sha256,write):
    p=policy(policy_raw,policy_sha256);path=filecheck(path,p['max_database_bytes'])
    db=sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=p['lock_timeout_ms']/1000,isolation_level=None)
    started=time.monotonic();db.set_progress_handler(lambda:int(time.monotonic()-started>=p['transaction_seconds']),100)
    try:
        need(db.execute('PRAGMA page_size').fetchone()[0]==4096,'PAGE_SIZE')
        need(db.execute('PRAGMA journal_mode').fetchone()[0]=='delete','DELETE_JOURNAL')
        if write:
            db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA max_page_count='+str(p['max_database_bytes']//4096))
        else:db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        need(db.execute('SELECT policy,pin FROM config LIMIT 2').fetchall()==[(policy_raw,policy_sha256)],'LEDGER_POLICY_IDENTITY')
        head=db.execute('SELECT generation FROM head LIMIT 2').fetchall();need(len(head)==1,'HEAD_SCHEMA');P.integer(head[0][0],'GENERATION',p['max_rows'])
        rows=db.execute('SELECT id,bytes,state FROM reservations LIMIT 129').fetchall();need(len(rows)<=p['max_rows'] and len(rows)==head[0][0],'ROW_GENERATION_BOUND')
        yield db,p,head[0][0],rows
        if db.in_transaction:db.rollback()
    finally:
        if db.in_transaction:db.rollback()
        db.close()

def reserve(path,*,policy_raw,policy_sha256,request_raw,observation_raw,_checkpoint=None):
    p=policy(policy_raw,policy_sha256);r=parse(request_raw,p['max_evidence_bytes']);o=parse(observation_raw,p['max_evidence_bytes'])
    need(set(r)==REQUEST_FIELDS and set(o)==OBS_FIELDS,'INPUT_FIELDS')
    for key in ('reservation','run','phase'):P.identity(r[key])
    for key in ('source_manifest_sha256','policy_sha256','observation_sha256'):pin(r[key])
    need(r['policy_sha256']==policy_sha256 and r['filesystem']==p['filesystem']==o['filesystem'] and r['epoch']==p['epoch'] and r['source_manifest_sha256']==p['source_manifest_sha256'],'REQUEST_CONTEXT')
    need(o['schema']=='FREE_SPACE_OBSERVATION_V1' and o['observer_source_sha256']==p['observer_source_sha256'],'OBSERVER_CONTEXT');pin(o['raw_evidence_sha256'])
    need(sha(observation_raw)==r['observation_sha256'],'OBSERVATION_PIN')
    P.integer(o['elapsed_ms'],'MONOTONIC_DURATION',5000)
    start,end=P.utc(o['sample_start']),P.utc(o['sample_end']);need(start<=end,'SAMPLE_ORDER')
    P.integer(r['expected_generation'],'EXPECTED_GENERATION',p['max_rows'])
    input_pin=sha(encode({'request_sha256':sha(request_raw),'observation_sha256':sha(observation_raw)}))
    began=time.monotonic()
    with connection(path,policy_raw,policy_sha256,True) as (db,p,generation,rows):
        existing=db.execute('SELECT input_pin FROM reservations WHERE id=?',(r['reservation'],)).fetchone()
        if existing:raise ValueError('DUPLICATE_RESERVATION' if existing[0]==input_pin else 'IDENTITY_CONFLICT')
        need(not db.execute('SELECT 1 FROM reservations WHERE run=? AND phase=?',(r['run'],r['phase'])).fetchone(),'RUN_PHASE_ALREADY_CONSUMED')
        need(generation==r['expected_generation'],'GENERATION_CONFLICT');need(len(rows)<p['max_rows'],'LEDGER_ROW_CAPACITY')
        charges=tuple(P.Charge(i,p['filesystem'],p['epoch'],state,amount) for i,amount,state in rows)
        result=P.plan(filesystem=p['filesystem'],epoch=p['epoch'],available_bytes=o['available_bytes'],observed_at=o['sample_start'],decision_at=r['decision_at'],max_age_seconds=p['max_age_seconds'],protected_margin_bytes=p['protected_margin_bytes'],pages=r['pages'],phases=r['phases'],charges=charges)
        need(end<=P.utc(r['decision_at']),'OBSERVATION_NOT_AVAILABLE')
        need(result['status']=='POSSIBLE_ARITHMETIC','INSUFFICIENT_CAPACITY')
        receipt=encode(dict(schema='CAPACITY_RESERVATION_RECEIPT_V1',reservation=r['reservation'],run=r['run'],phase=r['phase'],filesystem=p['filesystem'],epoch=p['epoch'],source_manifest_sha256=r['source_manifest_sha256'],policy_sha256=policy_sha256,input_sha256=input_pin,observation_sha256=sha(observation_raw),previous_generation=generation,generation=generation+1,state='RESERVED',decision_at=r['decision_at'],pages=r['pages'],phases=r['phases'],requested_bytes=result['requested_bytes'],charged_before_bytes=result['charged_bytes'],protected_margin_bytes=result['protected_margin_bytes'],observed_available_bytes=result['available_bytes'],remaining_bytes=result['remaining_bytes'],writer_authority=False,allocation_enforced=False,input_authenticated=False))
        need(len(receipt)<=p['max_evidence_bytes'],'RECEIPT_BYTE_BOUND')
        db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)',(r['reservation'],r['run'],r['phase'],input_pin,request_raw,observation_raw,receipt,result['requested_bytes'],'RESERVED'))
        need(db.execute('UPDATE head SET generation=? WHERE generation=?',(generation+1,generation)).rowcount==1,'GENERATION_CAS')
        if _checkpoint:_checkpoint('before_commit')
        need(time.monotonic()-began<p['transaction_seconds'],'COOPERATIVE_TRANSACTION_DEADLINE')
        db.commit()
        if _checkpoint:_checkpoint('after_commit_before_ack')
        return receipt

def reconcile(path,*,policy_raw,policy_sha256,reservation,input_sha256):
    P.identity(reservation);pin(input_sha256)
    with connection(path,policy_raw,policy_sha256,False) as (db,p,generation,rows):
        row=db.execute('SELECT input_pin,receipt FROM reservations WHERE id=?',(reservation,)).fetchone()
        if row is None:return dict(status='ABSENT_NOT_RETRY_AUTHORITY',launch_authorized=False,redispatch_authorized=False,generation=generation)
        need(row[0]==input_sha256,'IDENTITY_CONFLICT');need(type(row[1]) is bytes and len(row[1])<=p['max_evidence_bytes'],'RECEIPT_BOUND')
        return dict(status='EXISTING_RECEIPT_READ_ONLY',receipt=row[1],launch_authorized=False,redispatch_authorized=False,generation=generation)
