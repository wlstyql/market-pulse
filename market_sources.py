"""Public market data parsers for MARKET PULSE.

No authentication bypass, no fabricated values. Best effort HTML fetches from publicly
viewable pages. Page layouts and access policies may change at any time.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml,application/json,text/csv,*/*', 'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8'}


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script', 'noscript', 'svg'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('style', 'script', 'noscript', 'svg'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def visible_text(raw):
    p = VisibleText()
    p.feed(raw)
    return re.sub(r'\s+', ' ', html.unescape(' '.join(p.parts)))


def fetch_text(url, timeout=8):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as rsp:
        raw = rsp.read(2_000_000)
        charset = rsp.headers.get_content_charset() or 'utf-8'
        return raw.decode(charset, errors='replace')


def find_float(pattern, text, flags=re.I | re.S):
    m = re.search(pattern, text, flags)
    if m is None:
        raise ValueError('공개 페이지에서 수치를 찾지 못함 (HTML 구조 변경 또는 접근 제한)')
    return float(m.group(1).replace(',', ''))


def valid(num, lo, hi):
    if not lo <= num <= hi:
        raise ValueError(f'범위 밖 데이터 ({num})')
    return round(num, 4)


def check_age(dt, days, future_hours=28):
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt > now + timedelta(hours=future_hours) or dt < now - timedelta(days=days):
        raise ValueError(f'자료가 오래되었거나 날짜가 잘못됨: {dt.date()}')
    return dt.date().isoformat()


def date_ymd(s):
    return datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc)


def date_us_month(name, day, year):
    return datetime.strptime(f'{name[:3]} {int(day)} {year}', '%b %d %Y').replace(tzinfo=timezone.utc)


def parse_us_fed(raw):
    text = visible_text(raw)
    # Official Fed rate table shows decisions as 'September 17 25 0 3.75-4.00'.
    dates = []
    for m in re.finditer(r'\b(20\d{2})\b', text):
        if 2020 <= int(m.group(1)) <= datetime.now().year:
            dates.append((m.start(), int(m.group(1))))
    # Restrict to 2026 and earlier rate-history entries after the table heading.
    table = re.search(r"FOMC.{0,180}?target federal funds rate or range", text, re.I)
    section = text[table.start():] if table else text
    match = re.search(r'\b(20\d{2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+(\d{1,3})\s+(\d{1,3})\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)', section, re.I)
    if match:
        year, month, day, inc, dec, low, high = match.groups()
    else:
        year_match = re.search(r'\b(20\d{2})\b', section)
        if year_match is None:
            raise ValueError('연준 표에서 연도를 찾을 수 없음')
        year = year_match.group(1)
        row = re.search(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+(\d{1,3})\s+(\d{1,3})\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)', section[year_match.end():], re.I)
        if row is None: raise ValueError('연준 금리 표를 파싱할 수 없음')
        month, day, inc, dec, low, high = row.groups()
    dt = date_us_month(month, day, year)
    stamp = check_age(dt, 210)  # FOMC decisions may be several months apart
    low, high = valid(float(low), 0, 30), valid(float(high), 0, 30)
    if high < low or high - low > 1: raise ValueError('연준 금리 범위가 비정상적임')
    age = (datetime.now(timezone.utc) - dt).days
    direction = (1 if int(inc) > 0 else -1 if int(dec) > 0 else 0) if age <= 40 else 0
    return {'usrate': round((low + high) / 2, 4), 'usdir': direction}, stamp


def parse_kr_bok(raw):
    text = visible_text(raw)
    marker = text.find('한국은행 기준금리 추이')
    if marker < 0: raise ValueError('한국은행 기준금리 추이 표를 찾을 수 없음')
    section = text[marker:marker + 9000]
    rows = []
    for m in re.finditer(r'\b(20\d{2})\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(-?\d+(?:\.\d+)?)', section):
        year, month, day, rate = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
            rows.append((dt, valid(float(rate), 0, 20)))
        except ValueError:
            pass
    if not rows: raise ValueError('한국은행 금리 변경 표를 파싱할 수 없음')
    rows.sort(reverse=True)
    dt, rate = rows[0]
    stamp = check_age(dt, 450)
    change = rate - rows[1][1] if len(rows) > 1 else 0
    age = (datetime.now(timezone.utc)-dt).days
    direction = (1 if change > .001 else -1 if change < -.001 else 0) if age <= 40 else 0
    return {'krrate': rate, 'krdir': direction}, stamp


def parse_stockhub(raw):
    text = visible_text(raw)
    result = {}
    for key, pattern in (
        ('krfear', r'KOSPI\s+FEAR\s*&\s*GREED'),
        ('usfear', r'CNN\s+(?:공포[·\s]*탐욕\s*지수|Fear\s*&\s*Greed)')
    ):
        m = re.search(pattern, text, re.I)
        if not m: continue
        # The heading is followed by a score (possibly after a short Live label).
        next_text = text[m.end():m.end()+160]
        score_match = re.search(r'^\s*(?:(?:Live|Provided by CNN|CNN 제공|Index|지수)\s*)*([0-9]{1,3})(?!\d)', next_text, re.I)
        if not score_match: continue
        result[key] = valid(float(score_match.group(1)), 0, 100)
    if not result: raise ValueError('StockHub에서 지수 점수를 찾지 못함')
    # webpage displays updated time; score is accepted as recent ONLY if the page carries current date.
    dates = re.findall(r'(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})', text)
    if not dates: raise ValueError('공포탐욕 기준일을 확인하지 못함')
    stamps=[]
    for y,m,d in dates[:8]:
        try: stamps.append(datetime(int(y),int(m),int(d),tzinfo=timezone.utc))
        except ValueError: pass
    if not stamps: raise ValueError('공포탐욕 기준일 오류')
    stamp=check_age(max(stamps), 5)
    return result, stamp



def parse_feargreed_kospi(raw):
    """Extract only the overall Korea/KOSPI market score, never the US/sector scores.

    feargreed.co.kr initially serves placeholders and populates values with JS.
    Call this against rendered DOM, not the initial HTML skeleton.
    """
    content = visible_text(raw)
    # The section immediately below the Korean market heading contains the headline score.
    # Require the site's own score classification to avoid accidentally parsing a price.
    match = re.search(
        r'한국\s*시장\s*KOSPI\s*[·\u00b7/|&]\s*KOSDAQ\s*'
        r'([0-9]{1,3})(?![0-9.,])\s*'
        r'(극단적\s*공포|극단적\s*탐욕|공포|중립|탐욕)',
        content, re.I,
    )
    if not match:
        raise ValueError('한국 시장 KOSPI 공포탐욕 점수가 아직 표시되지 않음 (사이트 JS 실행 필요)')
    score = valid(float(match.group(1)), 0, 100)
    label = re.sub(r'\s+', '', match.group(2))
    if ((score <= 24 and label not in ('극단적공포', '공포')) or
        (25 <= score <= 44 and label != '공포') or
        (45 <= score <= 55 and label != '중립') or
        (56 <= score <= 74 and label != '탐욕') or
        (score >= 75 and label not in ('극단적탐욕', '탐욕'))):
        raise ValueError('한국 점수와 심리 분류가 일치하지 않아 값을 폐기함')
    # Data provider reports time-of-day but not necessarily an absolute as-of date.
    # Use collection time explicitly, not a fabricated provider publication date.
    return {'krfear': score}, datetime.now(timezone.utc).isoformat(timespec='seconds')


def browser_executable():
    """Find an installed Chromium browser; no downloaded driver is needed."""
    for name in ('chrome.exe', 'msedge.exe', 'chrome', 'chromium', 'chromium-browser', 'microsoft-edge'):
        found = shutil.which(name)
        if found:
            return found
    if os.name == 'nt':
        roots = [os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'),
                 os.environ.get('LOCALAPPDATA')]
        for root in filter(None, roots):
            for suffix in ('Google/Chrome/Application/chrome.exe',
                           'Microsoft/Edge/Application/msedge.exe'):
                path = Path(root) / suffix
                if path.is_file():
                    return str(path)
    raise RuntimeError('Chrome/Edge 브라우저가 설치되어 있지 않음')


def fetch_rendered_feargreed(timeout=24):
    """Run the site's JavaScript with installed Chrome/Edge and capture the rendered DOM.

    Runs with its own temporary user profile and never touches the user's browser session.
    """
    browser = browser_executable()
    with tempfile.TemporaryDirectory(prefix='marketpulse_fg_') as profile:
        cmd = [browser, '--headless', '--disable-gpu', '--no-first-run',
               '--no-default-browser-check', '--disable-extensions',
               '--disable-background-networking', '--disable-dev-shm-usage',
               '--hide-scrollbars', '--window-size=1280,900',
               '--virtual-time-budget=12000', '--user-data-dir=' + profile,
               '--dump-dom', 'https://feargreed.co.kr/']
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=timeout,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError('feargreed.co.kr 브라우저 렌더링 제한 시간 초과') from exc
        if proc.returncode != 0:
            raise RuntimeError('브라우저 렌더링 실패: ' + (proc.stderr or str(proc.returncode))[-180:])
        if '<html' not in proc.stdout.lower():
            raise ValueError('브라우저에서 유효한 HTML을 받지 못함')
        return proc.stdout


def parse_dramexchange(raw):
    text = visible_text(raw)
    section = re.search(r'DRAM Spot Price\s+Last Update\s*:\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*(20\d{2})', text, re.I)
    flash = re.search(r'(?:NAND )?Flash Spot Price\s+Last Update\s*:\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*(20\d{2})', text, re.I)
    if section is None: raise ValueError('DRAM 현물가격 기준일을 찾지 못함')
    dram_stamp = check_age(date_us_month(*section.groups()), 15)
    # Match row 5 price columns: high, low, session high, session low, average.
    def row_after(start, pattern, limit=900):
        rest = text[start:start+limit]
        row = re.search(pattern, rest, re.I)
        if not row: raise ValueError('현물가격 행을 찾을 수 없음')
        tail = rest[row.end():]
        nums = re.match(r'\s*((?:\d+(?:\.\d+)?\s+){4}\d+(?:\.\d+)?)', tail)
        if not nums: raise ValueError('현물가격 행이 5개 숫자를 갖고 있지 않음')
        return valid(float(nums.group(1).split()[4]), .01, 5000)
    ddr = row_after(section.end(), r'DDR4\s+16Gb\s*\(2Gx8\)\s*3200')
    values={'ddrprice': ddr}
    stamp={'ddrprice':dram_stamp}
    if flash:
        flash_date=check_age(date_us_month(*flash.groups()), 40)
        # NAND MLC 64Gb reference (not interchangeable with DRAM; used only for its own change).
        try:
            nand=row_after(flash.end(), r'MLC\s+64Gb\s+8GBx8')
            values['nandprice']=nand
            stamp['nandprice']=flash_date
        except ValueError: pass
    return values, stamp


def parse_us_buffett(raw):
    """Conservative extraction restricted to explicitly labeled Buffett ratios."""
    text=visible_text(raw)
    date_match=re.search(r'(?:Updated on|As of|Last Updated)\s+([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})',text,re.I)
    if not date_match: raise ValueError('미국 버핏지수 발표 날짜 미확인')
    stamp=check_age(date_us_month(*date_match.groups()),230)
    patterns=[
        r'Buffett Indicator\s*=\s*\$[\d.,]+T\s*/\s*\$[\d.,]+T\s*=\s*([\d.,]+)\s*%',
        r'(?:Current )?Buffett Indicator\s*(?:is|:|=|at)\s*([\d.,]+)\s*%',
        r'Current (?:Buffett Indicator|Ratio of Total Market Cap to GDP)\s*(?::|is|at)\s*([\d.,]+)\s*%'
    ]
    for pat in patterns:
        try:
            ratio=find_float(pat,text)
            return {'usbuffett':valid(ratio,20,500)},stamp
        except ValueError: pass
    raise ValueError('미국 버핏지수 표시 형식을 찾지 못함')


def parse_kr_buffett(raw):
    text=visible_text(raw)
    m=re.search(r'(?:As of|Updated on)\s+(20\d{2}-\d{2}-\d{2})',text,re.I)
    if m: dt=date_ymd(m.group(1))
    else:
        m=re.search(r'Updated at\s+\w+,\s*(\d{1,2})\s+(\w{3})\s+(20\d{2})',text,re.I)
        if not m: raise ValueError('한국 버핏지수 기준일을 찾지 못함')
        dt=datetime.strptime(' '.join(m.groups()),'%d %b %Y').replace(tzinfo=timezone.utc)
    stamp=check_age(dt,120)
    for pat in (r'(?:current\s*[-:]\s*|currently\s+)([\d.,]+)\s*%',
                r'(?:Korea|South Korea).{0,90}?(?:Market Cap to GDP|Buffett Indicator).{0,75}?([\d.,]+)\s*%'):
        try:
            ratio=find_float(pat,text)
            return {'krbuffett':valid(ratio,20,500)},stamp
        except ValueError: pass
    raise ValueError('한국 버핏지수 페이지에서 비율을 찾지 못함')


class LocalPriceHistory:
    """Persist daily DRAMeXchange prices; derive ONLY observed 7d/30d returns."""
    def __init__(self, path=None):
        base = Path(os.getenv('LOCALAPPDATA') or (Path.home() / '.market_pulse')) / 'MarketPulse'
        self.path=Path(path) if path else base/'spot_history.json'
        self.lock=threading.Lock()

    def changes(self, field, price, date_stamp):
        if field not in ('ddrprice','nandprice'): return {}
        with self.lock:
            try: data=json.loads(self.path.read_text(encoding='utf-8'))
            except (FileNotFoundError, OSError, ValueError): data={}
            hist=data.get(field,{})
            hist[date_stamp]=price
            today=date_ymd(date_stamp)
            hist={d:v for d,v in hist.items() if isinstance(v,(int,float)) and 0<v<5000 and re.fullmatch(r'\d{4}-\d\d-\d\d',d) and abs((today-date_ymd(d)).days)<=100}
            data[field]=hist
            self.path.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            tmp.replace(self.path)
            changes={}
            for days, suffix in [(7,'7change'),(30,'change')]:
                target=today-timedelta(days=days)
                candidates=[(date_ymd(d),v) for d,v in hist.items() if (target-timedelta(days=3))<=date_ymd(d)<=target]
                if candidates:
                    previous=max(candidates)[1]
                    changes[('ddr' if field=='ddrprice' else 'nand')+suffix]=round((price/previous-1)*100,2)
            return changes
