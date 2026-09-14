"""Three NEW cloud-only worker execution cases; no provider/production access."""
import json,os,signal,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from datetime import timedelta
import owned_runtime as R

class OwnedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path.cwd());self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name)/'attempt'
    def args(self,seconds):
        return dict(output=self.output,cutoff=R.clock()+timedelta(seconds=seconds),required_seconds=1,
                    maximum_seconds=10,stop_grace_seconds=1,safety_margin_seconds=1)
    def test_actual_short_child_finishes_before_absolute_cutoff(self):
        r=R.run_owned((sys.executable,'-c','pass'),**self.args(6))
        self.assertEqual(r['status'],'COMPLETED');self.assertEqual(r['child_returncode'],0)
        self.assertLess(r['finished_at'],r['cutoff'])
    def test_actual_persistence_delay_refuses_without_child_marker(self):
        args=self.args(4)
        with self.assertRaisesRegex(ValueError,'LATE_START_REFUSED'):
            R.run_owned((sys.executable,'-c',"open('UNAUTHORIZED_CHILD','w').write('started')"),
                        **args,_after_intent=lambda:time.sleep(2.1))
        self.assertFalse((self.output/'UNAUTHORIZED_CHILD').exists())
        self.assertFalse(json.loads((self.output/'result.json').read_bytes())['workload_started'])
    def test_actual_overrun_stops_only_owned_child_before_cutoff(self):
        r=R.run_owned((sys.executable,'-c','import time; time.sleep(10)'),**self.args(4))
        self.assertEqual(r['status'],'DEADLINE_STOP');self.assertLess(r['finished_at'],r['cutoff'])
        self.assertLess(r['elapsed_seconds'],4)
    def test_leader_exit_first_still_reaps_owned_descendant(self):
        code="import os,time; p=os.fork(); (time.sleep(20) if p==0 else os._exit(0))"
        r=R.run_owned((sys.executable,'-c',code),**self.args(8))
        self.assertEqual(r['status'],'DESCENDANTS_TERMINATED');self.assertEqual(len(r['reaped_descendant_pids']),1)
        for pid in r['reaped_descendant_pids']:
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
    def test_term_resistant_descendant_is_killed_when_leader_exits(self):
        code="import os,signal,time; p=os.fork(); (signal.signal(signal.SIGTERM,signal.SIG_IGN),open('ready','w').close(),time.sleep(20)) if p==0 else ([time.sleep(.01) for _ in range(100) if not os.path.exists('ready')],os._exit(0))"
        r=R.run_owned((sys.executable,'-c',code),**self.args(8))
        self.assertEqual(r['status'],'DESCENDANTS_TERMINATED');self.assertEqual(len(r['reaped_descendant_pids']),1)
        for pid in r['reaped_descendant_pids']:
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
    def test_actual_child_entry_delay_refuses_before_substantive_work(self):
        cutoff=R.clock()+timedelta(seconds=4)
        entry=str(Path(R.__file__).with_name('worker_entry.py'))
        result=subprocess.run((sys.executable,entry,cutoff.isoformat(),'1','10','1','1','2.1',sys.executable,'-c',"open('UNAUTHORIZED_CHILD','w').close()"),cwd=self.tmp.name,timeout=5)
        self.assertEqual(result.returncode,78);self.assertFalse((Path(self.tmp.name)/'UNAUTHORIZED_CHILD').exists())
    def test_zz_new_session_descendant_for_outer_cgroup_cleanup(self):
        # Intentional new session: same-group cleanup cannot cover this child.
        # The actual outer systemd unit must remove it when its main exits.
        marker=Path('/output/escaped-descendant.json')
        pid=os.fork()
        if pid==0:
            os.setsid()
            marker.write_text(json.dumps(dict(pid=os.getpid(),cgroup=Path('/proc/self/cgroup').read_text())))
            time.sleep(20);os._exit(0)
        until=time.monotonic()+2
        while not marker.exists() and time.monotonic()<until:time.sleep(.01)
        self.assertEqual(json.loads(marker.read_bytes())['pid'],pid)

if __name__=='__main__':unittest.main(verbosity=2)
