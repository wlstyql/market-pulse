"""Collect from public sources; publish only verified values to docs/market_data.json.

If the provider blocks access, existing verified values are carried forward with original
as_of timestamps; frontend distinguishes stale output. No fabricated live quotes.
"""
from pathlib import Path
import json, os
from datetime import datetime, timezone, timedelta
import market_pulse_core as core

OUTPUT = Path(__file__).parent / 'docs' / 'market_data.json'
KEEP_DAYS = {'krbuffett':120,'usbuffett':240,'krrate':400,'usrate':30, 'uscorecpi':70,'uscorecpi3m':70,'usunemployment':55,'usunemployment3m':55,'uspayrollchange':55,'uspayroll3m':55}

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
    core.FETCH_DEADLINE_SECONDS=55.0  # Scheduled collector need not obey desktop 6s UI limit.
    old=json.loads(OUTPUT.read_text(encoding='utf8')) if OUTPUT.exists() else {}
    new=core.collect()
    merged=merge(old,new)
    OUTPUT.write_text(json.dumps(merged,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf8')
    print('Collected',len(new.get('values',{})),'new numeric fields;',len(merged['values']),'published fields')
    for key in ('usrate','krrate','krbuffett','usbuffett'):
        m=merged.get('metadata',{}).get(key,{})
        print('STATUS',key,':',merged.get('values',{}).get(key,'MISSING'), 'carried_forward=',m.get('carried_forward',False), 'as_of=',m.get('as_of','unknown'))
    for k,v in new.get('errors',{}).items():print('WARN',k,':',str(v)[:130])

if __name__=='__main__':main()
