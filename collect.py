"""Collect from public sources; publish only verified values to docs/market_data.json.

If the provider blocks access, existing verified values are carried forward with original
as_of timestamps; frontend distinguishes stale output. No fabricated live quotes.
"""
from pathlib import Path
import json, os
from datetime import datetime, timezone, timedelta
import market_pulse_core as core

OUTPUT = Path(__file__).parent / 'docs' / 'market_data.json'
KEEP_DAYS = {'krbuffett':120,'usbuffett':120,'krrate':60,'usrate':60}

def merge(old, new):
    now=datetime.now(timezone.utc)
    values=dict(new.get('values',{})); meta=dict(new.get('metadata',{}))
    for key, value in old.get('values',{}).items():
        if key in values: continue
        m=old.get('metadata',{}).get(key,{})
        try:
            dt=datetime.fromisoformat(str(m['as_of']).replace('Z','+00:00'))
            maxdays=KEEP_DAYS.get(key,8)
            if dt.tzinfo is not None and timedelta(0) <= now-dt <= timedelta(days=maxdays):
                values[key]=value
                meta[key]=dict(m, carried_forward=True)
        except (KeyError,ValueError,TypeError):
            pass
    return dict(new, values=values,metadata=meta)

def main():
    core.FETCH_DEADLINE_SECONDS=25.0  # Scheduled collector need not obey desktop 6s UI limit.
    old=json.loads(OUTPUT.read_text(encoding='utf8')) if OUTPUT.exists() else {}
    new=core.collect()
    merged=merge(old,new)
    OUTPUT.write_text(json.dumps(merged,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf8')
    print('Collected',len(new.get('values',{})),'new numeric fields;',len(merged['values']),'published fields')
    for k,v in new.get('errors',{}).items():print('WARN',k,':',str(v)[:130])

if __name__=='__main__':main()
