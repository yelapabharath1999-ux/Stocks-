"""Free RSS-to-Telegram stock news monitor. Uses a Git-tracked state file."""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

STATE_PATH = Path(os.getenv('STATE_PATH', 'state.json'))
MAX_AGE_HOURS = int(os.getenv('MAX_AGE_HOURS', '6'))
MAX_ALERTS = int(os.getenv('MAX_ALERTS', '10'))
BOOTSTRAP = os.getenv('BOOTSTRAP', 'true').lower() == 'true'
STOCKS = {
    'RELIANCE': ('"Reliance Industries"', ['reliance industries', 'reliance ind', 'ril']),
    'TCS': ('"Tata Consultancy Services"', ['tata consultancy services', 'tcs']),
    'HDFCBANK': ('"HDFC Bank"', ['hdfc bank']),
}
KEYWORDS = [
    'quarterly results', 'quarterly profit', 'net profit', 'earnings',
    'dividend', 'buyback', 'merger', 'acquisition', 'acquires',
    'order win', 'wins order', 'wins contract', 'major contract',
    'regulatory action', 'rbi penalty', 'sebi penalty', 'fraud',
    'resignation', 'appoints ceo', 'credit rating', 'stake sale',
    'fundraise', 'fund raising', 'rights issue', 'stock split',
    'bonus issue', 'guidance cut', 'guidance raised',
]


def read_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (ValueError, OSError):
            logging.warning('State unreadable; starting fresh')
    return {'initialized': False, 'seen': {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')


def article_time(entry):
    raw = entry.get('published') or entry.get('updated')
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def collect(now):
    result = {}
    for symbol, (query, aliases) in STOCKS.items():
        q = quote(f'{query} (stock OR shares OR NSE) when:1d')
        url = f'https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en'
        try:
            response = requests.get(url, timeout=20, headers={'User-Agent': 'StockNewsAlerts/1.0'})
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if feed.bozo and not feed.entries:
                raise ValueError('Invalid RSS feed')
        except (requests.RequestException, ValueError) as exc:
            logging.warning('RSS fetch failed for %s: %s', symbol, exc)
            continue
        for entry in feed.entries:
            title = re.sub(r'\s+', ' ', entry.get('title', '')).strip()
            link = entry.get('link', '')
            if not title or not link:
                continue
            lower = title.lower()
            if not any(re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', lower) for alias in aliases):
                continue
            matches = [word for word in KEYWORDS if word in lower]
            if not matches:
                continue
            published = article_time(entry)
            if not published or published < now - timedelta(hours=MAX_AGE_HOURS) or published > now + timedelta(minutes=10):
                continue
            key = hashlib.sha256(f'{symbol}|{entry.get("id", link)}'.encode()).hexdigest()
            result[key] = {'symbol': symbol, 'title': title[:250], 'url': link,
                           'published': published.isoformat(), 'reason': matches[0]}
    return dict(sorted(result.items(), key=lambda kv: kv[1]['published']))


def send_telegram(item):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    msg = (f'📈 STOCK NEWS | {item["symbol"]}\n\n'
           f'{item["title"]}\n\n'
           f'Published: {item["published"]}\n'
           f'Source: {item["url"]}\n\n'
           'Automated news alert; verify before trading.')
    r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                      json={'chat_id': chat_id, 'text': msg, 'disable_web_page_preview': True}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get('ok'):
        raise RuntimeError(f'Telegram API error: {data.get("description", "unknown")}')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.getenv('TELEGRAM_BOT_TOKEN') or not os.getenv('TELEGRAM_CHAT_ID'):
        raise RuntimeError('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
    now = datetime.now(timezone.utc)
    state = read_state()
    seen = state.get('seen', {})
    # Retain seven days of IDs to avoid repeated stories.
    cutoff = now - timedelta(days=7)
    seen = {k: v for k, v in seen.items() if _valid_recent(v, cutoff)}
    found = collect(now)
    if not state.get('initialized') and BOOTSTRAP:
        seen.update({key: now.isoformat() for key in found})
        logging.info('First run: marked %d existing stories seen without sending', len(found))
    else:
        new = [(key, item) for key, item in found.items() if key not in seen]
        for key, item in new[:MAX_ALERTS]:
            try:
                send_telegram(item)
            except (requests.RequestException, RuntimeError) as exc:
                logging.error('Failed to send %s: %s', key[:8], exc)
                continue
            seen[key] = now.isoformat()
            logging.info('Sent %s: %s', item['symbol'], item['title'])
    state = {'initialized': True, 'seen': seen}
    save_state(state)
    logging.info('Done; %d matching stories, %d IDs stored', len(found), len(seen))


def _valid_recent(value, cutoff):
    try:
        return datetime.fromisoformat(value) >= cutoff
    except (ValueError, TypeError):
        return False


if __name__ == '__main__':
    main()
