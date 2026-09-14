"""Actual systemd worker entry: gate then monitored owned test process."""
import hashlib,json,sys
from datetime import datetime,timezone
from pathlib import Path
import deadline_admission as D
import owned_runtime as R
config=json.loads(Path('/work/job.input.json').read_bytes())
for name,pin in config['sources'].items():
    assert hashlib.sha256((Path('/work')/name).read_bytes()).hexdigest()==pin
cutoff=datetime.fromisoformat(config['cutoff'])
result=R.run_owned((sys.executable,'-m','unittest','-v','test_deadline_admission','test_owned_runtime'),
                  output=Path('/output/suite'),cutoff=cutoff,required_seconds=20,maximum_seconds=60,
                  stop_grace_seconds=2,safety_margin_seconds=5)
Path('/output/job-result.json').write_bytes(D.encode(result))
sys.exit(0 if result['status']=='COMPLETED' else 1)
