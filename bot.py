import os, json, time, logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
CHAT_IDS=[x.strip() for x in os.getenv('TELEGRAM_CHAT_ID','').split(',') if x.strip()]
SYMBOLS=[x.strip().upper() for x in os.getenv('MEXC_SYMBOLS','ETH_USDT,BTC_USDT').split(',') if x.strip()]
POLL=int(os.getenv('POLL_SECONDS','15'))
STATE_FILE=os.getenv('STATE_FILE','state.json')
URL='https://api.mexc.com/api/v1/contract/kline/{symbol}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('eth-bot')

def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo('Europe/Kyiv')).strftime('%Y-%m-%d %H:%M')

def color(c):
    return 'GREEN' if c['close'] > c['open'] else 'RED' if c['close'] < c['open'] else 'DOJI'

def load_state(path=None):
    path=path or STATE_FILE
    try:
        with open(path, encoding='utf8') as f:
            return json.load(f)
    except Exception:
        return {'last_processed_10m': None, 'pending': []}

def save_state(s, path=None):
    path=path or STATE_FILE
    with open(path + '.tmp', 'w', encoding='utf8') as f:
        json.dump(s, f, indent=2)
    os.replace(path + '.tmp', path)

def tg(text, group_text=None):
    if not TOKEN or not CHAT_IDS:
        log.error('TELEGRAM NOT CONFIGURED')
        return False
    sent=0
    for chat_id in CHAT_IDS:
        try:
            send_text = group_text if (group_text is not None and chat_id.startswith('-')) else text
            if not send_text or not send_text.strip():
                continue
            r=requests.post(
                f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': send_text},
                timeout=15
            )
            if r.ok and r.json().get('ok'):
                sent += 1
                log.info('TELEGRAM SENT OK | chat=%s', chat_id)
            else:
                log.error('TELEGRAM ERROR | chat=%s | status=%s body=%s', chat_id, r.status_code, r.text[:300])
        except Exception as e:
            log.exception('TELEGRAM EXCEPTION | chat=%s: %s', chat_id, e)
    return sent == len(CHAT_IDS)

def fetch(symbol):
    r=requests.get(
        URL.format(symbol=symbol),
        params={'interval':'Min1', 'limit':300, '_ts':int(time.time()*1000)},
        headers={'Cache-Control':'no-cache', 'Pragma':'no-cache', 'User-Agent':'ETHUSDT-10m-Signal-Bot/4.0'},
        timeout=15
    )
    r.raise_for_status()
    p=r.json()
    data=p.get('data')
    if not data:
        raise RuntimeError(f'MEXC empty response: {p}')
    out=[]
    if isinstance(data, dict) and isinstance(data.get('time'), list):
        times=data['time']; opens=data.get('open',[]); closes=data.get('close',[])
        for i,t in enumerate(times):
            out.append({'ts':int(t), 'open':float(opens[i]), 'close':float(closes[i])})
    elif isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                out.append({'ts':int(row.get('time',row.get('t'))), 'open':float(row.get('open',row.get('o'))), 'close':float(row.get('close',row.get('c')))})
            else:
                out.append({'ts':int(row[0]), 'open':float(row[1]), 'close':float(row[2])})
    else:
        raise RuntimeError(f'Unknown MEXC data format: {type(data).__name__}')
    out.sort(key=lambda x:x['ts'])
    return out

def current_10m(mins):
    """Build the currently forming 10m candle from 1m data."""
    if not mins:
        return None
    b=(mins[-1]['ts']//600)*600
    rows=[x for x in mins if (x['ts']//600)*600==b]
    if not rows:
        return None
    rows.sort(key=lambda x:x['ts'])
    return {'ts':b, 'open':rows[0]['open'], 'close':rows[-1]['close'], 'count':len(rows)}

def agg(mins):
    buckets=OrderedDict()
    for c in mins:
        b=(c['ts']//600)*600
        buckets.setdefault(b,[]).append(c)
    now=int(time.time())
    out=[]
    for b,rows in buckets.items():
        if b+600<=now and len(rows)>=9:
            out.append({'ts':b, 'open':rows[0]['open'], 'close':rows[-1]['close'], 'count':len(rows)})
    return out

class Engine:
    def __init__(self, state, state_file, symbol):
        self.state=state
        self.state_file=state_file
        self.symbol=symbol
        self.c=OrderedDict()
        self.pending=[]
        self.initialized=False
        self.pre_alerted=set()

    def seed(self, closed):
        """Load current history without generating historical signals/results."""
        self.c=OrderedDict((x['ts'], x) for x in closed[-150:])
        self.c=OrderedDict(sorted(self.c.items()))
        self.pending=[]
        self.state['pending']=[]
        self.state['last_processed_10m']=next(reversed(self.c)) if self.c else None
        save_state(self.state, self.state_file)
        self.initialized=True
        if self.c:
            latest=next(reversed(self.c.values()))
            log.info('INITIALIZED | history=%d | latest=%s %s | waiting for NEW 10m candle', len(self.c), utc(latest['ts']), color(latest))

    def maybe_pre_alert(self, live10):
        """Send PRE-ALERT only in the final minute of forming candle #8."""
        if not self.initialized or not live10:
            return

        now = time.time()
        elapsed = now - live10['ts']
        # Candle #8: last 60 seconds only (9:00-10:00 elapsed).
        if elapsed < 540 or elapsed >= 600:
            return

        target_ts = live10['ts']
        if target_ts in self.c:
            return

        # START is separate. Candle #8 is eight 10m intervals after START.
        start_ts = target_ts - 8 * 600
        if start_ts not in self.c:
            return

        keys = list(self.c)
        start_idx = keys.index(start_ts)
        start = self.c[start_ts]
        start_color = color(start)
        if start_color not in ('GREEN', 'RED'):
            return

        # START must be the last candle in its same-color run.
        if start_idx + 1 < len(keys) and color(self.c[keys[start_idx + 1]]) == start_color:
            return

        # #6 and #7 are already closed; #8 is still forming.
        c6_ts = target_ts - 2 * 600
        c7_ts = target_ts - 1 * 600
        if c6_ts not in self.c or c7_ts not in self.c:
            return

        required = 'RED' if start_color == 'GREEN' else 'GREEN'
        if color(self.c[c6_ts]) != required or color(self.c[c7_ts]) != required:
            return
        if color(live10) != required:
            return
        if target_ts in self.pre_alerted:
            return

        direction = 'LONG' if start_color == 'GREEN' else 'SHORT'
        log.info('PRE-ALERT | %s | start=%s %s | forming #8=%s', direction, utc(start['ts']), start_color, required)
        group_text = (
            '⚠️ УВАГА! СКОРО СИГНАЛ\n\n'
            f'{self.symbol.replace("_USDT", "USDT")} Futures\n'
            'Timeframe: 10m\n\n'
            'Свічки #6 і #7 відповідають умовам. '
            'Очікуємо повного закриття свічки #8 для підтвердження сигналу.'
        )
        # Send only to group chats; tg() ignores blank private text safely.
        tg('', group_text)
        self.pre_alerted.add(target_ts)

    def ingest_new(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        known=set(self.c)
        new=[x for x in closed if x['ts'] not in known]
        for x in new:
            self.c[x['ts']]=x
        self.c=OrderedDict(sorted(self.c.items()))
        while len(self.c)>150:
            self.c.popitem(last=False)
        for x in new:
            self.evaluate(x['ts'])
        if new:
            self.state['pending']=self.pending
            self.state['last_processed_10m']=new[-1]['ts']
            save_state(self.state, self.state_file)
        return len(new)

    def evaluate(self, ts):
        keys = list(self.c)
        idx = keys.index(ts)
        cur = self.c[ts]

        # Confirm setup on #7 and #8; final signal only after #8 closes.
        keep = []
        for p in self.pending:
            rel = idx - p['trigger_idx']  # rel=0 is #6, rel=1 is #7, rel=2 is #8

            if rel in (1, 2):
                required = 'RED' if p['start_color'] == 'GREEN' else 'GREEN'
                if color(cur) != required:
                    log.info('CANCEL SETUP | start=%s | candle #%d failed color condition',
                             utc(p['start_ts']), rel + 6)
                else:
                    if rel == 2 and not p.get('signal_sent', False):
                        signal_text = (
                            f'SIGNAL {p["direction"]}\\n\\n'
                            f'{self.symbol.replace("_USDT", "USDT")} Futures\\n'
                            'Timeframe: 10m\\n'
                            f'Start: {kyiv(p["start_ts"])} Kyiv time\\n'
                            'Confirmed: candles #6, #7, #8\\n\\n'
                            'Signal only - no automatic trading.\\n\\n'
                            'Трейдер Василь Павлів\\n@vasylpavliv\\nhttps://t.me/vasylpavliv'
                        )
                        tg(signal_text, signal_text)
                        p['signal_sent'] = True
                    keep.append(p)
                continue

            if rel < 3:
                keep.append(p)
                continue

            # Result candles #9..#15. WIN on first candle matching START color.
            # LOSS only if all seven are the opposite color; doji prevents LOSS.
            outcome_color = color(cur)
            p.setdefault('result_colors', []).append(outcome_color)
            if outcome_color == p['start_color']:
                log.info('RESULT WIN | %s | start=%s | result candle=%d',
                         p['direction'], utc(p['start_ts']), rel + 6)
                tg(
                    f'WIN\\n{self.symbol.replace("_USDT", "USDT")} Futures\\n'
                    f'Direction: {p["direction"]}\\nResult candle: {rel + 6}/15\\n\\n'
                    'Трейдер Василь Павлів\\n@vasylpavliv\\nhttps://t.me/vasylpavliv'
                )
                continue

            if rel == 9:
                result_colors = p['result_colors']
                opposite = 'RED' if p['start_color'] == 'GREEN' else 'GREEN'
                if len(result_colors) == 7 and all(c == opposite for c in result_colors):
                    log.info('RESULT LOSS | %s | start=%s | all candles #9-#15 opposite START',
                             p['direction'], utc(p['start_ts']))
                    tg(
                        f'LOSS\\n{self.symbol.replace("_USDT", "USDT")} Futures\\n'
                        f'Direction: {p["direction"]}\\n'
                        'All candles #9-#15 were opposite to START\\n\\n'
                        'Трейдер Василь Павлів\\n@vasylpavliv\\nhttps://t.me/vasylpavliv'
                    )
                else:
                    log.info('NO WIN/LOSS | %s | start=%s | doji/other result in candles #9-#15',
                             p['direction'], utc(p['start_ts']))
                continue

            keep.append(p)

        self.pending = keep

        # Candle #6 is exactly six intervals after START; START is not #1.
        if idx < 6:
            return

        sidx = idx - 6
        start = self.c[keys[sidx]]
        start_color = color(start)
        if start_color not in ('GREEN', 'RED'):
            return

        # START must be the last candle in its same-color run.
        if sidx + 1 < len(keys) and color(self.c[keys[sidx + 1]]) == start_color:
            return

        required = 'RED' if start_color == 'GREEN' else 'GREEN'
        if color(cur) != required:
            return

        if any(p['start_ts'] == start['ts'] for p in self.pending):
            return

        direction = 'LONG' if start_color == 'GREEN' else 'SHORT'
        log.info('SETUP CANDIDATE %s | start=%s %s | candle #6=%s; waiting for #7 and #8',
                 direction, utc(start['ts']), start_color, required)
        self.pending.append({
            'start_ts': start['ts'],
            'trigger_idx': idx,
            'start_color': start_color,
            'trigger_color': required,
            'direction': direction,
            'signal_sent': False,
            'result_colors': []
        })

engines={}
for sym in SYMBOLS:
    sf=os.getenv(f'STATE_FILE_{sym}', f'state_{sym}.json')
    engines[sym]=Engine(load_state(sf), sf, sym)

def main():
    log.info('Started MEXC 10m signal bot (REST polling)')
    log.info('Config: poll=%ss, chats=%d, token_configured=%s', POLL, len(CHAT_IDS), bool(TOKEN))
    log.info('Symbols: %s', ', '.join(SYMBOLS))
    log.info('Rule: START separate; #1-#5 any color; #6-#8 opposite START; #9-#15 result')
    if TOKEN and CHAT_IDS:
        tg('BOT ONLINE\nMEXC Futures 10m\nSignal bot is active.')
    last_log={}
    while True:
        for symbol,engine in engines.items():
            try:
                mins=fetch(symbol)
                live10=current_10m(mins)
                if live10:
                    engine.maybe_pre_alert(live10)
                closed=agg(mins)
                if not closed:
                    log.warning('%s | MEXC OK but no closed 10m candles yet', symbol)
                else:
                    latest=closed[-1]
                    n=engine.ingest_new(closed)
                    if n:
                        log.info('NEW DATA | %s | MEXC 1m=%d | closed_10m=%d | added=%d | latest=%s %s | O=%.4f C=%.4f | pending=%d', symbol, len(mins), len(closed), n, utc(latest['ts']), color(latest), latest['open'], latest['close'], len(engine.pending))
                    elif time.time()-last_log.get(symbol,0)>=60:
                        age=int(time.time()-(latest['ts']+600))
                        log.info('HEARTBEAT OK | %s | MEXC 1m=%d | closed_10m=%d | latest=%s %s | age=%ss | pending=%d', symbol, len(mins), len(closed), utc(latest['ts']), color(latest), max(age,0), len(engine.pending))
                        last_log[symbol]=time.time()
            except Exception as e:
                log.exception('LOOP ERROR %s: %s', symbol, e)
        time.sleep(POLL)

if __name__=='__main__':
    main()
