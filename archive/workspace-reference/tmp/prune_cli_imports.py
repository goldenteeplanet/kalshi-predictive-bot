import importlib
import re
from pathlib import Path
p=Path('/home/james/projects/kalshi-predictive-bot/src/kalshi_predictor/cli.py')
s=p.read_text()
pattern=re.compile(r'from (kalshi_predictor\.[\w\.]+) import \(\n(.*?)\n\)', re.S)

def repl(m):
    modname=m.group(1)
    body=m.group(2)
    names=[]
    for line in body.splitlines():
        stripped=line.strip().rstrip(',')
        if not stripped or stripped.startswith('#'):
            continue
        if ' as ' in stripped:
            name=stripped.split(' as ',1)[0].strip()
        else:
            name=stripped
        names.append((name,line))
    try:
        mod=importlib.import_module(modname)
    except Exception:
        return m.group(0)
    kept=[line for name,line in names if hasattr(mod,name)]
    if not kept:
        return ''
    return f'from {modname} import (\n' + '\n'.join(kept) + '\n)'
s2=pattern.sub(repl,s)
p.write_text(s2)
