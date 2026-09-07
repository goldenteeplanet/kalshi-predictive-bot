from pathlib import Path
p=Path('/home/james/projects/kalshi-predictive-bot/src/kalshi_predictor/cli.py')
s=p.read_text()
s=s.replace('        "phase3aw-dashboard-truth",\n', '')
start=s.find('    if command == "phase3aw-dashboard-truth":\n')
if start != -1:
    end=s.find('    if command == "phase3bc-r5-status":\n', start)
    if end == -1:
        raise SystemExit('fast path end not found')
    s=s[:start]+s[end:]
import_block='from kalshi_predictor.phase3ah_r3 import (\n    write_phase3ah_r3_bounded_scan_expansion_report,\n    write_phase3ah_r3_sports_provenance_repair_report,\n)\n'
s=s.replace(import_block, '')
for marker in ['@app.command("phase3ah-r3-bounded-scan-expansion")\n', '@app.command("phase3ah-r3-sports-provenance-repair")\n']:
    start=s.find(marker)
    if start != -1:
        end=s.find('\n\n@app.command(', start + len(marker))
        if end == -1:
            raise SystemExit(f'end not found for {marker}')
        s=s[:start]+s[end+2:]
p.write_text(s)
