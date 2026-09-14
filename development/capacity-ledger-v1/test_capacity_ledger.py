"""NEW UNTESTED cloud-targeted tests; no provider or production filesystem."""
import json,sqlite3,tempfile,unittest,multiprocessing
from pathlib import Path
import capacity_ledger as L

def caller(path,policy,pin,request,observation,ready,queue):
    ready.wait(3)
    try:queue.put(('OK',L.reserve(path,policy_raw=policy,policy_sha256=pin,request_raw=request,observation_raw=observation).decode()))
    except Exception as exc:queue.put(('REFUSED',str(exc)))

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path.cwd());self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'ledger.sqlite'
        self.p=dict(filesystem='dev-2064',epoch='fixture-epoch',observer_source_sha256='a'*64,source_manifest_sha256='c'*64,max_age_seconds=2,protected_margin_bytes=0,max_rows=8,max_evidence_bytes=8192,max_database_bytes=1048576,lock_timeout_ms=1000,transaction_seconds=3)
        self.policy=L.encode(self.p);self.pin=L.sha(self.policy);L.create(self.path,policy_raw=self.policy,policy_sha256=self.pin)
        self.obs=L.encode(dict(schema='FREE_SPACE_OBSERVATION_V1',filesystem='dev-2064',available_bytes=L.P.PAGE_PHASE_BYTES,sample_start='2026-09-14T15:00:00Z',sample_end='2026-09-14T15:00:00.1Z',elapsed_ms=100,observer_source_sha256='a'*64,raw_evidence_sha256='b'*64))
    def request(self,**changes):
        d=dict(reservation='r0',run='run0',phase='verify',filesystem='dev-2064',epoch='fixture-epoch',source_manifest_sha256='c'*64,policy_sha256=self.pin,expected_generation=0,pages=1,phases=1,decision_at='2026-09-14T15:00:01Z',observation_sha256=L.sha(self.obs));d.update(changes);return L.encode(d)
    def reserve(self,request=None,**changes):return L.reserve(self.path,policy_raw=self.policy,policy_sha256=self.pin,request_raw=request or self.request(),observation_raw=self.obs,**changes)
    def digest(self,request):return L.sha(L.encode({'request_sha256':L.sha(request),'observation_sha256':L.sha(self.obs)}))
    def read(self,request=None,reservation='r0'):
        return L.reconcile(self.path,policy_raw=self.policy,policy_sha256=self.pin,reservation=reservation,input_sha256=self.digest(request or self.request()))
    def count(self):
        with sqlite3.connect(self.path) as d:return d.execute('SELECT generation FROM head').fetchone()[0],d.execute('SELECT count(*),coalesce(sum(bytes),0) FROM reservations').fetchone()
    def test_exact_fit_reopen_receipt_and_no_authority(self):
        original=self.reserve();r=self.read();self.assertEqual(r['receipt'],original);self.assertFalse(r['launch_authorized']);self.assertFalse(r['redispatch_authorized']);self.assertEqual(self.count(),(1,(1,L.P.PAGE_PHASE_BYTES)))
    def test_duplicate_changed_identity_and_run_phase_refuse(self):
        self.reserve()
        for request,reason in [(self.request(),'DUPLICATE_RESERVATION'),(self.request(pages=2),'IDENTITY_CONFLICT'),(self.request(reservation='other',expected_generation=1),'RUN_PHASE_ALREADY_CONSUMED')]:
            with self.assertRaisesRegex(ValueError,reason):self.reserve(request)
        self.assertEqual(self.count()[0],1)
    def test_commit_ack_loss_readonly_reconcile_never_redispatch(self):
        def fault(where):
            if where=='after_commit_before_ack':raise RuntimeError('ACK_LOST')
        with self.assertRaisesRegex(RuntimeError,'ACK_LOST'):self.reserve(_checkpoint=fault)
        self.assertEqual(self.read()['status'],'EXISTING_RECEIPT_READ_ONLY')
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.reserve()
    def test_before_commit_rollback_absence_is_not_retry_authority(self):
        def fault(where):
            if where=='before_commit':raise RuntimeError('CRASH_BEFORE_COMMIT')
        with self.assertRaises(RuntimeError):self.reserve(_checkpoint=fault)
        self.assertEqual(self.count(),(0,(0,0)));r=self.read();self.assertEqual(r['status'],'ABSENT_NOT_RETRY_AUTHORITY');self.assertFalse(r['redispatch_authorized'])
    def test_full_phase_and_one_byte_short_refuse_without_charge(self):
        for available,pages,phases in [(7395848192,15,2),(L.P.PAGE_PHASE_BYTES-1,1,1)]:
            o=json.loads(self.obs);o['available_bytes']=available;self.obs=L.encode(o)
            with self.assertRaisesRegex(ValueError,'INSUFFICIENT_CAPACITY'):self.reserve(self.request(pages=pages,phases=phases))
        self.assertEqual(self.count()[0],0)
    def test_context_generation_observer_and_source_pin_rejection(self):
        for field,value in [('filesystem','other'),('epoch','other'),('policy_sha256','e'*64),('source_manifest_sha256','BAD'),('expected_generation',1)]:
            with self.assertRaises(ValueError):self.reserve(self.request(**{field:value}))
        o=json.loads(self.obs);o['observer_source_sha256']='f'*64;self.obs=L.encode(o)
        with self.assertRaisesRegex(ValueError,'OBSERVER_CONTEXT'):self.reserve()
        self.assertEqual(self.count()[0],0)
    def test_stale_future_observation_and_boolean_refuse(self):
        for field,value in [('decision_at','2026-09-14T15:00:03Z'),('decision_at','2026-09-14T14:59:59Z'),('pages',True),('expected_generation',False)]:
            with self.assertRaises(ValueError):self.reserve(self.request(**{field:value}))
        self.assertEqual(self.count()[0],0)
    def test_evidence_pin_bytecap_and_database_bytecap_refuse(self):
        with self.assertRaisesRegex(ValueError,'OBSERVATION_PIN'):self.reserve(self.request(observation_sha256='d'*64))
        with self.assertRaisesRegex(ValueError,'EVIDENCE_BYTE_BOUND'):self.reserve(b'x'*8193)
        with self.path.open('ab') as f:f.write(b'\0'*1048576)
        with self.assertRaisesRegex(ValueError,'DATABASE_FILE_BOUND'):self.reserve()
    def test_all_retained_states_charge_and_no_release_api(self):
        self.reserve();self.assertFalse(hasattr(L,'release'))
        for state in ['RESERVED','RUNNING','COMPLETED_CHARGED','AMBIGUOUS']:
            # Direct fixture setup only; public API never refunds/changes state.
            with sqlite3.connect(self.path) as d:d.execute('UPDATE reservations SET state=?',(state,))
            with self.assertRaisesRegex(ValueError,'INSUFFICIENT_CAPACITY'):self.reserve(self.request(reservation='r1',run='run1',expected_generation=1))
        self.assertEqual(self.count()[0],1)
    def test_two_cloud_processes_cannot_overcommit(self):
        context=multiprocessing.get_context('spawn');ready=context.Event();queue=context.Queue()
        children=[context.Process(target=caller,args=(str(self.path),self.policy,self.pin,self.request(reservation='r'+str(i),run='run'+str(i)),self.obs,ready,queue)) for i in range(2)]
        try:
            for c in children:c.start()
            ready.set();results=[queue.get(timeout=8),queue.get(timeout=8)]
            for c in children:c.join(3);self.assertEqual(c.exitcode,0)
            self.assertEqual(sorted(x[0] for x in results),['OK','REFUSED']);self.assertEqual(self.count(),(1,(1,L.P.PAGE_PHASE_BYTES)))
        finally:
            for c in children:
                if c.is_alive():c.terminate();c.join(3)
            queue.close();queue.join_thread()
    def test_row_capacity_preserves_committed_receipt(self):
        self.path=Path(self.tmp.name)/'one-row.sqlite';self.p['max_rows']=1;self.policy=L.encode(self.p);self.pin=L.sha(self.policy)
        L.create(self.path,policy_raw=self.policy,policy_sha256=self.pin);original=self.reserve()
        with self.assertRaisesRegex(ValueError,'LEDGER_ROW_CAPACITY'):self.reserve(self.request(reservation='r1',run='run1',expected_generation=1))
        self.assertEqual(self.read()['receipt'],original)
if __name__=='__main__':unittest.main(verbosity=2)
