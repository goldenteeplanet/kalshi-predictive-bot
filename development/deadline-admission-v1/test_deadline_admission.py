"""NEW cloud-only focused cases; no local execution or production targets."""
import json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
import deadline_admission as D

class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path.cwd());self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.now=datetime(2026,9,14,15,tzinfo=timezone.utc);self.pin=D.sha(Path(D.__file__).read_bytes());self.raw=b'synthetic input';self.calls=[]
    def args(self,**changes):
        d=dict(identity='new-fixture',source_sha256=self.pin,input_raw=self.raw,input_sha256=D.sha(self.raw),declared_at=self.now,not_before=self.now,cutoff=self.now+timedelta(seconds=14),required_work_seconds=5,maximum_runtime_seconds=10,stop_grace_seconds=2,safety_seconds=2,clock=lambda:self.now,submit=lambda intent:self.calls.append(intent))
        d.update(changes);return d
    def intent(self):return (self.root/'new-fixture'/'intent.json').read_bytes()
    def worker(self,clock,work):
        raw=self.intent();return D.worker_start(raw,intent_sha256=D.sha(raw),expected_source_sha256=self.pin,expected_input_sha256=D.sha(self.raw),clock=clock,work=work)
    def test_exactfit_runtime_preserved_intent_before_callback(self):
        def callback(intent):
            self.assertEqual(json.loads(self.intent()),intent);self.assertEqual((self.root/'new-fixture'/'input.original').read_bytes(),self.raw);self.calls.append(intent)
        r=D.dispatch_once(self.root,**self.args(submit=callback,required_work_seconds=10));self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0]['effective_runtime_seconds'],10);self.assertFalse(r['retry_authorized'])
    def test_new_delayed_dispatch_refuses_zero_callbacks(self):
        ticks=iter([self.now,self.now+timedelta(seconds=6)])
        with self.assertRaisesRegex(ValueError,'DEADLINE_ADMISSION_REFUSED'):D.dispatch_once(self.root,**self.args(clock=lambda:next(ticks)))
        self.assertEqual(self.calls,[]);self.assertTrue((self.root/'new-fixture'/'refusal.json').exists())
    def test_third_clock_gate_refuses_after_second_pass(self):
        ticks=iter([self.now,self.now,self.now+timedelta(seconds=1)])
        with self.assertRaisesRegex(ValueError,'DEADLINE_ADMISSION_REFUSED'):D.dispatch_once(self.root,**self.args(clock=lambda:next(ticks)))
        self.assertEqual(self.calls,[])
    def test_uncertain_submit_consumes_second_attempt(self):
        def callback(intent):self.calls.append(intent);raise TimeoutError('synthetic uncertain response')
        with self.assertRaises(TimeoutError):D.dispatch_once(self.root,**self.args(submit=callback))
        with self.assertRaisesRegex(ValueError,'IDENTITY_CONSUMED'):D.dispatch_once(self.root,**self.args())
        self.assertEqual(len(self.calls),1);self.assertTrue((self.root/'new-fixture'/'submission-uncertain.json').exists())
    def test_delayed_worker_start_refuses_zero_work(self):
        D.dispatch_once(self.root,**self.args());work=[]
        with self.assertRaisesRegex(ValueError,'WORKER_START_TOO_LATE'):self.worker(lambda:self.now+timedelta(seconds=6),lambda x:work.append(x))
        self.assertEqual(work,[])
    def test_worker_reduces_runtime_on_valid_later_start(self):
        D.dispatch_once(self.root,**self.args());work=[];self.worker(lambda:self.now+timedelta(seconds=2),lambda x:work.append(x))
        self.assertEqual(work[0]['effective_runtime_seconds'],8);self.assertFalse(work[0]['hard_watchdog_enforced'])
    def test_future_naive_and_invalid_bounds_failclosed(self):
        with self.assertRaisesRegex(ValueError,'DEADLINE_ADMISSION_REFUSED'):D.dispatch_once(self.root,**self.args(not_before=self.now+timedelta(seconds=1)))
        for kwargs in ({'now':self.now.replace(tzinfo=None)},{'required_seconds':True},{'maximum_seconds':0},{'stop_grace_seconds':0},{'safety_margin_seconds':61}):
            args=dict(now=self.now,cutoff=self.now+timedelta(seconds=14),required_seconds=5,maximum_seconds=10,stop_grace_seconds=2,safety_margin_seconds=2);args.update(kwargs)
            with self.assertRaises(ValueError):D.effective_runtime(**args)
        self.assertEqual(self.calls,[])
    def test_one_second_short_and_pins_refuse(self):
        r=D.effective_runtime(self.now,self.now+timedelta(seconds=13),10,10,2,2);self.assertEqual(r['status'],'INSUFFICIENT_TIME')
        for changes in ({'source_sha256':'a'*64},{'input_sha256':'b'*64},{'input_raw':b'x'*65537}):
            with self.assertRaises(ValueError):D.dispatch_once(self.root,**self.args(**changes))
        self.assertEqual(self.calls,[])
if __name__=='__main__':unittest.main(verbosity=2)
