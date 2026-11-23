"""Conservative parsers for Investing.com economic-calendar pages.
Only completed ACTUAL results are accepted, never forecasts. Financial HTML may block bots.
"""
import re
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from market_sources import visible_text

URLS = {
 'corecpi': 'https://kr.investing.com/economic-calendar/core-cpi-56',
 'unemployment': 'https://kr.investing.com/economic-calendar/unemployment-rate-300',
 'payrolls': 'https://kr.investing.com/economic-calendar/nonfarm-payrolls-227',
}

class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows=[];self.row=None;self.cell=None;self.depth=0
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,s):
        if self.cell is not None:self.cell.append(s)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(' '.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None

def event_rows(raw,kind,now=None):
    now=now or datetime.now(timezone.utc)
    p=Tables();p.feed(raw)
    result=[]
    for row in p.rows:
        if len(row)<3:continue
        d=re.search(r'(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일',row[0])
        if not d:continue
        try:dt=datetime(*map(int,d.groups()),tzinfo=timezone.utc)
        except ValueError:continue
        if dt>now+timedelta(days=1):continue
        # Table columns: 발표일, 시간, 결과, 예측, 이전. Never pull forecast when result empty.
        if len(row)<5:continue
        actual=row[2].strip().replace(',','')
        unit='K' if kind=='payrolls' else '%'
        m=re.fullmatch(r'([+-]?\d+(?:\.\d+)?)\s*'+re.escape(unit),actual,re.I)
        if not m:continue
        value=float(m.group(1))
        if kind=='payrolls' and abs(value)>1500:continue
        if kind!='payrolls' and not (-5<=value<=35):continue
        result.append((dt,value))
    result.sort(key=lambda x:x[0])
    if result:return result
    # Fallback ONLY explicit 'recent release + actual' headline, no forecast inference.
    txt=visible_text(raw)
    m=re.search(r'최근\s*발표\s*(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일\s*결과\s*([+-]?\d+(?:\.\d+)?)\s*(K|%)',txt,re.I)
    if not m:raise ValueError('Investing 공개 페이지에서 실제 발표값을 찾지 못함')
    yy,mm,dd,rawval,unit=m.groups()
    if unit.upper()!=('K' if kind=='payrolls' else '%'):raise ValueError('단위 불일치')
    dt=datetime(int(yy),int(mm),int(dd),tzinfo=timezone.utc)
    if dt>now+timedelta(days=1):raise ValueError('미래 발표값 사용 금지')
    return [(dt,float(rawval))]

def investing_macro(fetcher):
    """Published UI keys; CPI m/m is NOT CPI y/y. Dates are the release date."""
    now=datetime.now(timezone.utc); out={}; dates={}; errors={}
    for key,url in URLS.items():
        try:
            rows=event_rows(fetcher(url,6),key,now)
            date=rows[-1][0]
            if now-date>timedelta(days=75):raise ValueError('오래된 발표: '+date.date().isoformat())
            dates[key]=date.date().isoformat()
            if key=='corecpi':
                out['uscorecpimom']=round(rows[-1][1],2)
                if len(rows)>=3:
                    factors=[1+v/100 for _,v in rows[-3:]]
                    annualized=((factors[0]*factors[1]*factors[2])**4-1)*100
                    out['uscorecpi3m']=round(annualized,2)
            elif key=='unemployment':
                out['usunemployment']=round(rows[-1][1],2)
                if len(rows)>=4:out['usunemployment3m']=round(rows[-1][1]-rows[-4][1],2)
            else:
                out['uspayrollchange']=round(rows[-1][1],1)
                if len(rows)>=3:out['uspayroll3m']=round(sum(v for _,v in rows[-3:])/3,1)
        except Exception as exc:errors[key]=str(exc)[:150]
    return out,dates,errors
