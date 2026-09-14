"""Privileged controller for ONE fixed owned development unit, never production."""
import json,subprocess,sys,time
from pathlib import Path
from datetime import datetime,timezone
root=Path('/mnt/kalshi-backup-02/alpha-cloud-dev-20260914/attempt-deadline-v1')
cutoff=datetime.fromisoformat(sys.argv[1]);assert cutoff.tzinfo is not None
unit='alpha-cloud-deadline-v1-20260914.service'
remaining=(cutoff-datetime.now(timezone.utc)).total_seconds();assert 0<remaining<=600
mono=time.monotonic()+remaining
(root/'watchdog.ready').write_text(cutoff.isoformat())
while time.monotonic()<mono and datetime.now(timezone.utc)<cutoff:time.sleep(.1)
# Kill the entire exact owned unit, even if its leader is already gone.
r=subprocess.run(['systemctl','kill','--kill-whom=all','--signal=SIGKILL',unit],capture_output=True,text=True,timeout=5)
(root/'watchdog-result.json').write_text(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),unit=unit,returncode=r.returncode,stdout=r.stdout,stderr=r.stderr)))
