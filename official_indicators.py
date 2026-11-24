"""MARKET PULSE: Investing.com actual US rate and independent KRX/BOK KR Buffett.

Requires: KRX_API_KEY (approved stock daily feeds) + BOK_ECOS_API_KEY.
All monetary amounts internally in KRW; never substitute fabricated live values.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.request import Request, urlopen

INVESTING_URL = 'https://kr.investing.com/economic-calendar/fed-interest-rate-decision-168'
KRX_ROOT = 'https://data-dbg.krx.co.kr/svc/apis/sto/'
ECOS_ROOT = 'https://ecos.bok.or.kr/api/'

class _Visible(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'): self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'): self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def _get_json(url, headers=None, timeout=8):
    req=Request(url,headers={'User-Agent':'MARKET-PULSE/1.0',**(headers or {})})
    with urlopen(req,timeout=timeout) as response:
        return json.loads(response.read(8000000).decode('utf-8-sig'))


def parse_investing_actual(raw):
    """Read *latest release actual*, never forecast or previous. Return (percent, date)."""
    parser=_Visible();parser.feed(raw)
    s=re.sub(r'\s+',' ',' '.join(parser.parts))
    m=re.search(r'최근\s*발표\s*(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일\s*결과\s*([\d.,]+)\s*%',s)
    if not m:
        raise ValueError('Investing.com recent announcement ACTUAL field absent or blocked')
    year, month, day, value=m.groups()
    d=datetime(int(year),int(month),int(day),tzinfo=timezone.utc)
    if not datetime.now(timezone.utc)-timedelta(days=180) <= d <= datetime.now(timezone.utc)+timedelta(days=2):
        raise ValueError('Investing.com decision date is not recent')
    rate=float(value.replace(',',''))
    if not 0 <= rate <= 25:raise ValueError('US policy rate out of range')
    return rate,d.date().isoformat()


def investing_us_rate(fetch_html=None):
    if fetch_html is None:
        def fetch_html(url):
            with urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0','Accept-Language':'ko-KR,ko;q=0.9'}),timeout=8) as r:
                return r.read(2000000).decode('utf-8',errors='replace')
    return parse_investing_actual(fetch_html(INVESTING_URL))


def parse_krx_market_cap(payload):
    """KRX OutBlock_1 MKTCAP is KRW, not thousands; sum one market only."""
    items=payload.get('OutBlock_1')
    if not isinstance(items,list) or not items:
        raise ValueError('KRX OutBlock_1 missing / empty: confirm approved API and business day')
    dates={row.get('BAS_DD') for row in items}
    if len(dates)!=1:raise ValueError('Multiple KRX dates in one response')
    day=next(iter(dates))
    if not re.fullmatch(r'\d{8}',str(day)):
        raise ValueError('Invalid KRX trade date')
    total=0
    for row in items:
        amount=int(str(row['MKTCAP']).replace(',',''))
        if amount < 0:raise ValueError('Negative market capitalization')
        total+=amount
    if total<=0:raise ValueError('Zero market capitalization')
    return total,day


def krx_market_cap(key, today=None, request_json=None):
    """KOSPI + KOSDAQ; exclude KRX funds/ETFs and KONEX intentionally."""
    if not key:raise ValueError('KRX_API_KEY not configured')
    if request_json is None:request_json=_get_json
    today=today or datetime.now(timezone(timedelta(hours=9))).date()
    last_error=None
    for offset in range(0,8):
        day=(today-timedelta(days=offset)).strftime('%Y%m%d')
        try:
            totals=[];actual_dates=[]
            for service in ('stk_bydd_trd','ksq_bydd_trd'):
                url=KRX_ROOT+service+'?'+urlencode({'basDd':day})
                data=request_json(url,headers={'AUTH_KEY':key},timeout=8)
                value,stamp=parse_krx_market_cap(data)
                totals.append(value);actual_dates.append(stamp)
            if actual_dates[0]!=actual_dates[1]:raise ValueError('KOSPI and KOSDAQ dates differ')
            return sum(totals),day
        except Exception as e:last_error=e
    raise RuntimeError('KRX API failed for last 8 calendar days: '+str(last_error))


def _ecos_rows(obj,service):
    table=obj.get(service,{})
    if 'row' not in table:
        err=obj.get('RESULT') or table.get('RESULT') or obj
        raise ValueError(f'ECOS {service} no rows: {str(err)[:250]}')
    return table['row']


def parse_ecos_gdp(rows):
    """Parse annual nominal GDP (STAT_CODE 200Y105), unit '십억원' -> KRW.

    The data-item selection is verified by the item metadata, not hardcoded guesses.
    """
    annual=[]
    for row in rows:
        year=str(row.get('TIME',''))
        if not re.fullmatch(r'20\d{2}',year):continue
        value=float(str(row.get('DATA_VALUE','')).replace(',',''))
        if value<=0:continue
        annual.append((year,round(value*1_000_000_000)))
    if not annual:raise ValueError('No annual nominal GDP observations')
    return max(annual,key=lambda pair:pair[0])


def _select_ecos_nominal_gdp_item(items):
    """Fail closed if BOK item layout is ambiguous; don't silently take real GDP."""
    choices=[]
    for row in items:
        cycle=str(row.get('CYCLE',''))
        label=' '.join(str(row.get(k,'')) for k in ('ITEM_NAME','ITEM_NAME1','ITEM_NAME2','ITEM_NAME3','ITEM_NAME4'))
        if cycle not in ('A',''):continue
        if '국내총생산' not in label and 'GDP' not in label.upper():continue
        if any(t in label for t in ('실질','성장률','기여도','전년','비중','디플레이터','1인당')):continue
        if '명목' not in label and '국내총생산' not in label:continue
        choices.append(row)
    # The ECOS hierarchy can produce multiple candidate paths; require explicit item code if so.
    codes={str(v.get('ITEM_CODE','')) for v in choices if v.get('ITEM_CODE')}
    if len(codes)!=1:
        raise ValueError('Ambiguous BOK nominal GDP item; set BOK_GDP_ITEM_CODE after reviewing ECOS StatisticItemList: '+str(sorted(codes)[:12]))
    return codes.pop()


def bok_annual_gdp(key, request_json=None, now_year=None, item_code=None):
    if not key:raise ValueError('BOK_ECOS_API_KEY not configured')
    if request_json is None:request_json=_get_json
    item_code=item_code or os.getenv('BOK_GDP_ITEM_CODE','').strip()
    if not item_code:
        items=_ecos_rows(request_json(f'{ECOS_ROOT}StatisticItemList/{key}/json/kr/1/1000/200Y105'), 'StatisticItemList')
        item_code=_select_ecos_nominal_gdp_item(items)
    now_year=now_year or datetime.now(timezone.utc).year
    url=f'{ECOS_ROOT}StatisticSearch/{key}/json/kr/1/100/200Y105/A/{now_year-4}/{now_year}/{item_code}'
    rows=_ecos_rows(request_json(url),'StatisticSearch')
    year,gdp=parse_ecos_gdp(rows)
    # safeguard: an annual GDP estimate of 500-8000 trillion won.
    if not 500e12<gdp<8000e12:raise ValueError('BOK GDP units or item selection appear wrong')
    return gdp,year,item_code


def calculated_kr_buffett(krx_key=None,bok_key=None,request_krx=None,request_ecos=None,today=None):
    """Returns calculated ratio; it is NOT the same series as GuruFocus."""
    krx_key=krx_key or os.environ.get('KRX_API_KEY','')
    bok_key=bok_key or os.environ.get('BOK_ECOS_API_KEY','')
    cap,market_day=krx_market_cap(krx_key,today=today,request_json=request_krx)
    gdp,gdp_year,item=bok_annual_gdp(bok_key,request_json=request_ecos)
    ratio=round(cap/gdp*100,3)
    if not 1<ratio<1000:raise ValueError('Calculated Buffett ratio outside plausible range')
    return {'krbuffett':ratio,'market_cap_krw':cap,'nominal_gdp_krw':gdp,'market_as_of':f'{market_day[:4]}-{market_day[4:6]}-{market_day[6:]}','gdp_year':gdp_year,'gdp_ecos_item':item,'method':'KRX KOSPI+KOSDAQ listed equities / BOK latest annual nominal GDP'}
