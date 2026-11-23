"""MARKET PULSE local desktop launcher. Python standard library only.
Run python market_pulse.py or bundle on Windows using PyInstaller.
This app never exposes its HTTP service outside 127.0.0.1.
"""
import csv
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, wait
import io
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from market_sources import (fetch_text, parse_us_fed, parse_kr_bok, parse_stockhub,
                            parse_dramexchange, parse_us_buffett, parse_kr_buffett,
                            parse_feargreed_kospi, fetch_rendered_feargreed,
                            LocalPriceHistory)

APP_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
HTML = b''  # public deployment serves docs/index.html via GitHub Pages
CACHE_SECONDS = 3600
FETCH_DEADLINE_SECONDS = 6.0
CACHE = {'ts': 0.0, 'body': None}
LOCK = threading.Lock()
ACTIVE = {'running': False, 'body': None}

PRICE_HISTORY = LocalPriceHistory()
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36', 'Accept': 'application/json,text/csv,text/html,*/*', 'Accept-Language':'en-US,en;q=0.9'}


def get_bytes(url, timeout=9):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000)


def get_json(url):
    return json.loads(get_bytes(url))


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def current_date(value, max_age_days=12):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        if not (-timedelta(hours=1) <= datetime.now(timezone.utc)-date <= timedelta(days=max_age_days)):
            raise ValueError('data timestamp outside accepted range')
        return date.isoformat()
    except ValueError:
        raise ValueError('invalid or stale timestamp')


def yahoo(symbol):
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe='') + '?range=3mo&interval=1d'
    obj = get_json(url)['chart']['result'][0]
    quote_list = obj['indicators']['quote'][0]['close']
    pts = [(datetime.fromtimestamp(ts, timezone.utc), float(v)) for ts, v in zip(obj['timestamp'], quote_list) if v is not None and float(v)>0]
    if len(pts) < 5:
        raise ValueError('insufficient history')
    dt, val = pts[-1]
    current_date(dt.isoformat(), 8)
    def pct(days):
        target = dt - timedelta(days=days)
        prev = [p for p in pts[:-1] if p[0] <= target]
        if not prev:
            return None
        return round((val / prev[-1][1] - 1) * 100, 2)
    return round(val, 4), pct(30), pct(7), dt.isoformat()


def kospi_sentiment():
    """Independent KOSPI sentiment proxy, NOT the CNN or feargreed.co.kr index.

    Components: 20-day momentum (35%), distance to 20/60d moving averages
    (25%), 14-day RSI (20%), 20-day annualized volatility (20%).
    Requires actual index history; never derive a score from the US CNN value.
    """
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EKS11?range=6mo&interval=1d'
    obj = get_json(url)['chart']['result'][0]
    close_raw = obj['indicators']['quote'][0]['close']
    points = [(datetime.fromtimestamp(ts, timezone.utc), float(px))
              for ts, px in zip(obj['timestamp'], close_raw)
              if px is not None and math.isfinite(float(px)) and float(px) > 0]
    if len(points) < 65:
        raise ValueError('코스피 지수 이력 65거래일 미만')
    dt, price = points[-1]
    stamp = current_date(dt.isoformat(), 8)
    closes = [x[1] for x in points]
    def clamp(x): return max(0., min(100., x))
    ma20 = statistics.mean(closes[-20:])
    ma60 = statistics.mean(closes[-60:])
    momentum20 = price / closes[-21] - 1
    momentum = clamp(50 + momentum20 * 500)  # +10% over 20 sessions -> 100
    distance = clamp(50 + ((price/ma20-1)*0.6 + (price/ma60-1)*0.4)*500)
    differences = [b-a for a,b in zip(closes[-15:-1],closes[-14:])]
    gain = sum(max(x,0) for x in differences)/14
    loss = sum(max(-x,0) for x in differences)/14
    rsi = 100 if loss == 0 and gain > 0 else (50 if gain == loss == 0 else 100-100/(1+gain/loss))
    returns = [math.log(b/a) for a,b in zip(closes[-21:-1],closes[-20:])]
    volatility = statistics.stdev(returns)*math.sqrt(252)*100
    vol_score = clamp(100 - (volatility-12)*2.5)  # 12% annualized ->100; 52% ->0
    score = round(0.35*momentum + 0.25*distance + 0.2*rsi + 0.2*vol_score, 1)
    return {'krfear': score}, stamp


def fred(identifier):
    start=(datetime.now(timezone.utc)-timedelta(days=100)).strftime('%Y-%m-%d')
    url='https://fred.stlouisfed.org/graph/fredgraph.csv?' + urlencode({'id':identifier,'cosd':start})
    raw = get_bytes(url, timeout=7).decode('utf-8-sig')
    rows = []
    for row in csv.DictReader(io.StringIO(raw)):
        val = row.get(identifier)
        if val and val != '.':
            try: rows.append((datetime.strptime(row['DATE'], '%Y-%m-%d').replace(tzinfo=timezone.utc), float(val)))
            except (ValueError, KeyError): pass
    if not rows: raise ValueError('no data')
    current_date(rows[-1][0].isoformat(), 14)
    return rows


def put(values, metadata, key, val, source, stamp):
    if isinstance(val, (float, int)) and not isinstance(val, bool) and abs(val) < 1e8:
        values[key] = val
        metadata[key] = {'source': source, 'as_of': stamp}


def collect(progress=None):
    """Independent source checks; any partial success is preserved with its source date."""
    values, metadata, errors = {}, {}, {}
    write_lock = threading.Lock()
    pending = set()
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS

    def emit():
        if time.monotonic() > deadline: return
        if progress is not None:
            progress({'as_of': iso_now(), 'values': dict(values),
                      'metadata': dict(metadata), 'errors': dict(errors),
                      'pending': sorted(pending), 'complete': False})

    def task(name, fn):
        try:
            fn()
        except Exception as exc:
            with write_lock:
                if time.monotonic() > deadline: return
                errors[name] = f'{type(exc).__name__}: {str(exc)[:190]}'
                emit()
        finally:
            with write_lock:
                pending.discard(name)
                emit()

    def record(field_values, source, stamp):
        with write_lock:
            if time.monotonic() > deadline: return
            for key, val in field_values.items():
                put(values, metadata, key, val, source, stamp)
            emit()

    def commodity(symbols, price_field, change_field):
        last_error = None
        for symbol in symbols:
            try:
                px, d30, _, stamp = yahoo(symbol)
                record({price_field: px}, 'Yahoo Finance ' + symbol, stamp)
                if d30 is not None:
                    record({change_field: d30}, 'Yahoo Finance ' + symbol, stamp)
                return
            except Exception as exc:
                last_error = exc
        raise last_error or ValueError('가격 조회 실패')

    def gold():
        try:
            raw = get_json('https://api.gold-api.com/price/XAU')
            price = float(raw['price'])
            stamp = current_date(raw.get('updatedAt') or raw.get('updated_at') or raw.get('timestamp'), 7)
            record({'goldprice': price}, 'Gold API XAU spot', stamp)
        except Exception:
            commodity(['GC=F'], 'goldprice', 'goldchange')

    def us_rate():
        # Avoid the FRED CSV timeout first. Fed publishes an official history without API key.
        try:
            out, stamp = parse_us_fed(fetch_text('https://www.federalreserve.gov/monetarypolicy/openmarket.htm', 8))
            record(out, 'Federal Reserve FOMC official rate history (midpoint)', stamp)
        except Exception as primary:
            try:
                rows = fred('DFEDTARU')
                stamp, upper = rows[-1]
                check = current_date(stamp.isoformat(), 14)
                # A federal funds target range midpoint cannot be inferred from upper bound alone.
                # Lower bound from independent FRED series required.
                lower_rows = fred('DFEDTARL')
                lower = lower_rows[-1][1]
                if not 0 <= lower <= upper <= 30 or upper-lower>1: raise ValueError('미국 금리 범위 오류')
                record({'usrate': round((upper+lower)/2,4)}, 'FRED DFEDTARU/DFEDTARL', check)
            except Exception as fallback:
                raise ValueError('Fed: %s; FRED: %s' % (str(primary)[:70], str(fallback)[:70]))

    def kr_rate():
        try:
            out,stamp=parse_kr_bok(fetch_text('https://www.bok.or.kr/portal/singl/baseRate/list.do?menuNo=200761',8))
            record(out,'한국은행 공식 기준금리 추이',stamp)
        except Exception as official:
            # ECOS requires user's key; no fake/sample endpoints.
            key=os.environ.get('BOK_ECOS_API_KEY','').strip()
            if not key: raise ValueError('한국은행 공식 페이지: '+str(official)[:95]+'; ECOS 키 미설정')
            today=datetime.now(timezone.utc)
            start=(today-timedelta(days=120)).strftime('%Y%m%d')
            end=today.strftime('%Y%m%d')
            url=f'https://ecos.bok.or.kr/api/StatisticSearch/{quote(key)}/json/kr/1/100/722Y001/D/{start}/{end}/0101000'
            raw=get_json(url)
            rows=raw.get('StatisticSearch',{}).get('row',[])
            pts=[]
            for r in rows:
                try: pts.append((datetime.strptime(r['TIME'],'%Y%m%d').replace(tzinfo=timezone.utc),float(r['DATA_VALUE'])))
                except (KeyError, ValueError): pass
            if not pts: raise ValueError('ECOS에서 기준금리 데이터가 조회되지 않음')
            pts.sort()
            dt, rate=pts[-1]
            stamp=current_date(dt.isoformat(), 45)
            record({'krrate':rate},'한국은행 ECOS',stamp)

    def fear():
        try:
            out,stamp=parse_stockhub(fetch_text('https://www.stockhub.kr/fear-greed',3.0))
            out={k:v for k,v in out.items() if k=='usfear'}
            if not out: raise ValueError('미국 지수 없음')
            record(out,'StockHub CNN US as republished',stamp)
        except Exception:
            us_fear_backup()

    def kr_fear_kospi():
        out, stamp = kospi_sentiment()
        record(out, '자체 산출 KOSPI 심리지수 (20일 모멘텀·이평선·RSI·변동성)', stamp)

    def us_fear_backup():
        if 'usfear' in values: return
        data=get_json('https://production.dataviz.cnn.io/index/fearandgreed/graphdata')
        item=data['fear_and_greed']
        score=float(item['score'])
        if not 0 <= score <= 100: raise ValueError('CNN 지수 범위 오류')
        stamp=current_date(item['timestamp'],7)
        record({'usfear':round(score,2)},'CNN unofficial endpoint',stamp)

    def semiconductor():
        prices, stamps=parse_dramexchange(fetch_text('https://www.dramexchange.com/',9))
        for field,price in prices.items():
            stamp=stamps[field]
            # NAND price is retained only in history: the existing GUI does not display it.
            if field=='ddrprice':record({field:price},'DRAMeXchange DDR4 16Gb 3200 session average',stamp)
            changes=PRICE_HISTORY.changes(field, price, stamp)
            if changes: record(changes,'DRAMeXchange local observed-price history',stamp)
        if not prices: raise ValueError('DRAM/NAND 행이 없음')

    def us_buffett():
        out,stamp=parse_us_buffett(fetch_text('https://www.currentmarketvaluation.com/models/buffett-indicator.php',9))
        record(out,'CurrentMarketValuation (quarterly public value)',stamp)

    def kr_buffett():
        urls=['https://www.gurufocus.com/economic_indicators/4333/korea-ratio-of-total-market-cap-over-gdp',
              'https://www.gurufocus.com/global-market-valuation.php?country=KOR']
        failure=None
        for url in urls:
            try:
                out,stamp=parse_kr_buffett(fetch_text(url,8))
                record(out,'GuruFocus Korea TMC/GDP',stamp)
                return
            except Exception as exc: failure=exc
        raise failure or ValueError('한국 버핏지수 조회 실패')

    tasks=[
        ('gold',gold),('copper',lambda:commodity(['HG=F'],'copperprice','copperchange')),
        ('wti',lambda:commodity(['CL=F'],'wtiprice','wtichange')),
        ('usRate',us_rate),('krRate',kr_rate),('krFear',kr_fear_kospi),('usFear',fear),
        ('semiconductor',semiconductor),('usBuffett',us_buffett),('krBuffett',kr_buffett)
    ]
    pending.update(name for name, _ in tasks)
    emit()
    pool = ThreadPoolExecutor(max_workers=len(tasks))
    futures = {pool.submit(task, name, fn): name for name,fn in tasks}
    try:
        done, unfinished = wait(futures, timeout=max(0, deadline-time.monotonic()))
        for future in unfinished:
            name = futures[future]
            errors.setdefault(name, '6초 수집 제한 초과 · 이전 입력값 유지')
            pending.discard(name)
            future.cancel()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Remaining running workers cannot alter the published snapshot after deadline.
    if 'krfear' not in values:
        errors['krFear'] = errors.get('krFear','KOSPI 자체 산출 심리지수 수신 실패')
    if 'usfear' not in values:
        errors['usFear'] = errors.get('usFear') or errors.get('fear','미국 공포탐욕 조회 실패')
    if 'krbuffett' not in values:
        errors['buffettKR'] = errors.get('krBuffett','한국 버핏 수신 실패')
    if 'usbuffett' not in values:
        errors['buffettUS'] = errors.get('usBuffett','미국 버핏 수신 실패')
    result = {'as_of':iso_now(),'values':values,'metadata':metadata,'errors':errors,'pending':[],'complete':True}
    if progress is not None:
        progress(result)
    return result

def _run_collection():
    def progress(snapshot):
        with LOCK:
            ACTIVE['body'] = snapshot
    try:
        result = collect(progress=progress)
    except Exception as exc:
        result = {'as_of': iso_now(), 'values': {}, 'metadata': {},
                  'errors': {'server': f'{type(exc).__name__}: {exc}'},
                  'pending': [], 'complete': True}
    with LOCK:
        CACHE.update(ts=time.time(), body=result)
        ACTIVE.update(running=False, body=result)


def market_payload(force=False):
    # Return immediately. A slow website must never block the local HTTP response.
    with LOCK:
        if ACTIVE['running']:
            return dict(ACTIVE['body'] or {'as_of': iso_now(), 'values': {},
                         'metadata': {}, 'errors': {}, 'pending': [], 'complete': False})
        if not force and CACHE['body'] is not None and time.time()-CACHE['ts'] < CACHE_SECONDS:
            return CACHE['body']
        ACTIVE['running'] = True
        ACTIVE['body'] = {'as_of': iso_now(), 'values': {}, 'metadata': {},
                          'errors': {}, 'pending': ['initializing'], 'complete': False}
        threading.Thread(target=_run_collection, name='market-fetch', daemon=True).start()
        return dict(ACTIVE['body'])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.path.split('?')[0]
        if route in ('/', '/index.html'):
            data, mimetype = HTML, 'text/html; charset=utf-8'
        elif route == '/api/market-data':
            data, mimetype = json.dumps(market_payload(force='force=1' in self.path), ensure_ascii=False).encode(), 'application/json; charset=utf-8'
        elif route == '/health':
            data, mimetype = b'OK', 'text/plain; charset=utf-8'
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header('Content-Type', mimetype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    address = f'http://127.0.0.1:{port}/'
    print('MARKET PULSE: '+address, flush=True)
    print('Close this window or press Ctrl+C to stop.', flush=True)
    threading.Timer(0.8, lambda: webbrowser.open(address)).start()
    try: server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
