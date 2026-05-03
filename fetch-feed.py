#!/usr/bin/env python3
"""
Feed fetcher — news, gaming, weird, memes
Runs every 15 minutes via cron.
AI enrichment (summaries, scores, dedup) is handled by ai-enrich.py which runs after.
"""
import json, os, re, time, hashlib, xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from datetime import datetime, timezone

OUT      = '/root/.openclaw/workspace-todo/dashboard/data/feed.json'
UA       = 'Mozilla/5.0 (compatible; FocusDashboard/1.0)'
REDDIT_UA = 'FocusDashboard/1.0 by focus_dashboard_bot'
HEADERS  = {'User-Agent': UA}
MAX_PER_SOURCE = 20   # hard cap — no single source dominates the feed
MAX_AGE_DAYS   = 7    # drop items older than this

# ─── HTTP helpers ──────────────────────────────────────────────────────────────

def fetch(url, timeout=12, ua=None):
    try:
        req = Request(url, headers={'User-Agent': ua or UA})
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'    FAIL {url[:80]}: {e}')
        return None

def make_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:12]

# ─── Text helpers ──────────────────────────────────────────────────────────────

def strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&#32;', ' ').replace('&amp;', '&').replace('&lt;', '<') \
               .replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'") \
               .replace('&nbsp;', ' ').replace('&#8217;', "'").replace('&#8220;', '"') \
               .replace('&#8221;', '"').replace('&#8211;', '–').replace('&#8212;', '—')
    return re.sub(r'\s{2,}', ' ', text).strip()

def clean_desc(text, source=''):
    text = strip_html(text)
    # Strip Reddit boilerplate
    text = re.sub(r'submitted by\s+.*?(\[link\]|\[comments\]|$)', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[link\]\s*|\[comments\]\s*', '', text)
    # Strip newsletter/subscribe boilerplate
    text = re.sub(r'(this is (today\'?s?|the) (edition|issue|newsletter)|'
                  r'subscribe (to|for)|sign up (to|for)|'
                  r'this subscriber.only|'
                  r'download,? our (weekday|daily|weekly))[^.]*\.?', '',
                  text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text[:400]

def is_valid_image(url):
    """Return False for tiny/broken/placeholder images."""
    if not url:
        return False
    # Reddit default/self thumbnails
    if url in ('self', 'default', 'nsfw', 'spoiler', 'image'):
        return False
    # Tiny images (Reddit often serves width=108 or width=140 previews)
    m = re.search(r'[?&]width=(\d+)', url)
    if m and int(m.group(1)) < 250:
        return False
    # Icon-sized images
    if re.search(r'/(icon|logo|favicon|avatar)[^/]*\.(png|jpg|gif)', url, re.IGNORECASE):
        return False
    return True

def best_image(candidates):
    """Pick the highest-quality image from a list of candidates."""
    for url in candidates:
        if url and is_valid_image(url):
            return url.replace('&amp;', '&')
    return None

# ─── RSS / Atom parser ──────────────────────────────────────────────────────────

def parse_rss(url, source_name, category):
    items = []
    raw = fetch(url)
    if not raw:
        return items
    try:
        root = ET.fromstring(raw)
        is_atom = 'feed' in root.tag or 'Atom' in root.tag

        if is_atom:
            NS = 'http://www.w3.org/2005/Atom'
            MNS = 'http://search.yahoo.com/mrss/'
            entries = root.findall(f'{{{NS}}}entry') or root.findall('entry')
            for e in entries:
                def t(tag, ns=NS): return (e.findtext(f'{{{ns}}}{tag}') or '').strip()
                title = t('title')
                # link
                link_el = e.find(f'{{{NS}}}link[@rel="alternate"]') or e.find(f'{{{NS}}}link')
                link = (link_el.get('href','') if link_el is not None else '').strip()
                if not title or not link:
                    continue
                content = t('content') or t('summary')
                updated = t('updated') or t('published')
                desc = clean_desc(content, source_name)
                # images
                imgs = []
                for src in [content, t('summary')]:
                    m = re.search(r'<img[^>]+src="([^"]+)"', src)
                    if m: imgs.append(m.group(1))
                for mtag in [f'{{{MNS}}}thumbnail', f'{{{MNS}}}content']:
                    mel = e.find(mtag)
                    if mel is not None: imgs.append(mel.get('url',''))
                ts = _parse_ts_iso(updated)
                items.append(_item(make_id(link), title, desc, link,
                                   source_name, category, best_image(imgs), ts))
        else:
            for item in root.iter('item'):
                def t(tag): return (item.findtext(tag) or '').strip()
                title = t('title')
                link  = t('link')
                if not title or not link:
                    continue
                desc_raw = t('description')
                desc = clean_desc(desc_raw, source_name)
                # images
                imgs = []
                for mtag in ['{http://search.yahoo.com/mrss/}thumbnail',
                              '{http://search.yahoo.com/mrss/}content']:
                    mel = item.find(mtag)
                    if mel is not None: imgs.append(mel.get('url',''))
                enc = item.find('enclosure')
                if enc is not None and enc.get('type','').startswith('image'):
                    imgs.append(enc.get('url',''))
                m = re.search(r'<img[^>]+src="([^"]+)"', desc_raw)
                if m: imgs.append(m.group(1))
                ts = _parse_ts_rfc(t('pubDate'))
                items.append(_item(make_id(link), title, desc, link,
                                   source_name, category, best_image(imgs), ts))
    except Exception as e:
        print(f'    RSS parse error {source_name}: {e}')
    return items

def _item(id_, title, desc, url, source, category, image, ts):
    return {'id': id_, 'title': title, 'desc': desc, 'url': url,
            'source': source, 'category': category, 'image': image,
            'score': 0, 'comments': 0,
            'ts': ts or datetime.now(timezone.utc).isoformat()}

def _parse_ts_iso(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace('Z', '+00:00')).isoformat()
    except: return None

def _parse_ts_rfc(s):
    if not s: return None
    for fmt in ['%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z']:
        try: return datetime.strptime(s.strip(), fmt).isoformat()
        except: pass
    return None

# ─── Reddit ────────────────────────────────────────────────────────────────────

def parse_reddit(subreddit, category, limit):
    """Try JSON first, fall back to RSS."""
    url = f'https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}'
    raw = None
    try:
        req = Request(url, headers={'User-Agent': REDDIT_UA})
        with urlopen(req, timeout=10) as r:
            raw = r.read().decode('utf-8', errors='replace')
    except Exception:
        pass

    if raw:
        try:
            data = json.loads(raw)
            posts = data.get('data', {}).get('children', [])
            items = []
            for post in posts:
                d = post.get('data', {})
                if d.get('stickied') or d.get('over_18') or not d.get('title'):
                    continue
                title     = d['title']
                permalink = 'https://reddit.com' + d.get('permalink', '')
                purl      = d.get('url', '')
                # Best image from multiple sources
                imgs = []
                if d.get('post_hint') == 'image':
                    imgs.append(purl)
                try:
                    previews = d.get('preview', {}).get('images', [])
                    if previews:
                        # Pick largest resolution
                        resolutions = previews[0].get('resolutions', [])
                        source_img  = previews[0].get('source', {}).get('url', '')
                        if resolutions:
                            # pick largest that's not tiny
                            for res in reversed(resolutions):
                                if res.get('width', 0) >= 320:
                                    imgs.append(res['url'].replace('&amp;', '&'))
                                    break
                        if source_img:
                            imgs.append(source_img.replace('&amp;', '&'))
                except Exception:
                    pass
                thumb = d.get('thumbnail', '')
                if thumb.startswith('http'):
                    imgs.append(thumb)
                desc = clean_desc(d.get('selftext', '')[:400])
                items.append(_item(
                    make_id(permalink), title, desc, permalink,
                    f'r/{d.get("subreddit", subreddit)}', category,
                    best_image(imgs),
                    datetime.fromtimestamp(d.get('created_utc', 0),
                                           tz=timezone.utc).isoformat()
                ))
                items[-1]['score']    = d.get('score', 0)
                items[-1]['comments'] = d.get('num_comments', 0)
            return items
        except Exception as e:
            print(f'    Reddit JSON parse error r/{subreddit}: {e}')

    # RSS fallback
    return _parse_reddit_rss(subreddit, category, limit)

def _parse_reddit_rss(subreddit, category, limit):
    items = []
    raw = fetch(f'https://www.reddit.com/r/{subreddit}/hot.rss?limit={limit}',
                ua=REDDIT_UA)
    if not raw:
        return items
    try:
        ns  = {'a': 'http://www.w3.org/2005/Atom'}
        root = ET.fromstring(raw)
        for e in root.findall('a:entry', ns):
            title   = (e.findtext('a:title', '', ns) or '').strip()
            link_el = e.find('a:link[@href]', ns)
            link    = link_el.get('href', '') if link_el is not None else ''
            content = e.findtext('a:content', '', ns) or ''
            updated = e.findtext('a:updated', '', ns) or ''
            if not title or not link:
                continue
            imgs = []
            for m in re.finditer(r'<img[^>]+src="([^"]+)"', content):
                u = m.group(1).replace('&amp;', '&')
                if any(x in u for x in ('preview.redd.it', 'i.redd.it',
                                         'imgur.com', 'i.imgur.com')):
                    imgs.append(u)
            desc = clean_desc(content)
            ts   = _parse_ts_iso(updated)
            items.append(_item(make_id(link), title, desc, link,
                               f'r/{subreddit}', category, best_image(imgs), ts))
    except Exception as ex:
        print(f'    Reddit RSS parse error r/{subreddit}: {ex}')
    return items[:limit]

# ─── Hacker News ───────────────────────────────────────────────────────────────

def parse_hackernews(limit=25):
    items = []
    raw = fetch('https://hacker-news.firebaseio.com/v0/topstories.json')
    if not raw:
        return items
    try:
        ids = json.loads(raw)[:limit]
        for sid in ids:
            sraw = fetch(f'https://hacker-news.firebaseio.com/v0/item/{sid}.json')
            if not sraw:
                continue
            s = json.loads(sraw)
            if s.get('type') != 'story' or not s.get('title'):
                continue
            url = s.get('url') or f'https://news.ycombinator.com/item?id={sid}'
            item = _item(make_id(url), s['title'], '', url,
                         'Hacker News', 'tech', None,
                         datetime.fromtimestamp(s.get('time', 0),
                                                tz=timezone.utc).isoformat())
            item['score']    = s.get('score', 0)
            item['comments'] = s.get('descendants', 0)
            items.append(item)
    except Exception as e:
        print(f'    HN parse error: {e}')
    return items

# ─── Quality filter ────────────────────────────────────────────────────────────

# Patterns matched against title (case-insensitive)
_TRASH = re.compile('|'.join([
    # ---- Meta / recurring community threads ----
    r'^/r/', r'\bmegathread\b', r'\bdaily\b.{0,20}\bthread\b',
    r'\bweekly\b.{0,20}\bthread\b', r'\bmonthly\b.{0,20}\bthread\b',
    r'\bfree\s+talk\b', r'\bsimple\s+questions\b', r'\bopen\s+thread\b',
    r'\bindie\s+sunday\b', r'\blive\s+thread\b', r'\bdiscussion\s+thread\b',
    r'\bpinned\b.{0,20}\bthread\b', r'\bwhat\'?s\s+everyone\s+(playing|reading|watching)\b',

    # ---- Personal / low-effort Reddit posts ----
    r'\brate\s+my\b', r'\bcheck\s+out\s+my\b', r'\bmy\s+setup\b',
    r'\bjust\s+(got|bought|finished|beat|started|received)\b',
    r'\banyone\s+else\b', r'\bam\s+i\s+the\s+only\b', r'\bwho\s+else\b',
    r'\bdoes\s+anyone\b', r'\bupvote\s+if\b',
    r'\bi\s+(took|captured|made)\s+(this|a|my)\b',
    r'\bmy\s+first\b', r'\bseen\s+from\s+my\b',
    r'\bself[\s.-]promot',

    # ---- Ads / deals / commercial ----
    r'\bsponsored\s+content\b', r'\bpaid\s+partnership\b',
    r'\bpromo\s+code\b', r'\bcoupon\s+code\b', r'\bdiscount\s+code\b',
    r'\breferral\b.{0,15}\bdeal\b',
    r'\blimited\s+time\s+(offer|deal)\b',
    r'^save\s+\d+\s*%', r'\bnow\s+just\s+\$\d',
    r'\bpower\s+(bank|station)\b.{0,20}\b(review|deal|sale)\b',

    # ---- Product buyer-guides / roundups that aren't news ----
    r'\bbest\b.{1,30}\b(tested|reviewed|ranked)\s+(in|for)\s+20\d\d\b',
    r'\bhow\s+to\s+buy\s+(a|an|the)\b',
    r'\b(tested\s+and\s+reviewed|our\s+top\s+picks)\b',

    # ---- Newsletter / digest posts ----
    r'\b(this\s+week\'?s?|this\s+month\'?s?)\s+(newsletter|digest|roundup|picks)\b',
    r'\bweekly\s+(picks|roundup|digest|newsletter)\b',
    r'\bthe\s+(download|weekly\s+wrap|morning\s+brief)\b',
    r'\bsubscriber.{0,5}only\b',
    r'\btoday\'?s?\s+edition\b',

    # ---- Job postings ----
    r'\b(we\'?re|are)\s+(hiring|looking\s+for)\b',
    r'\bjob\s+opening\b', r'\bjoin\s+our\s+team\b',

    # ---- Non-English (common leakthrough) ----
    r'\bassistenza\s+per\b', r'\bayuda\s+con\b', r'\bpregunta\s+sobre\b',
    r'\balguien\s+(sabe|puede)\b', r'\bquelqu\'un\b', r'\bkann\s+jemand\b',
    r'\bcomment\s+(faire|obtenir)\b',

    # ---- AMA / ELI5 meta ----
    r'\b(ask me anything|ama)\b', r'\beli5\b',
]), re.IGNORECASE)

# Patterns matched against description
_TRASH_DESC = re.compile('|'.join([
    r'\bsubscribe\s+(to|for)\s+(our|this|the)\b',
    r'\bsign\s+up\s+(to|for)\s+(our|this|the)\b',
    r'\bthis\s+(is\s+)?(today\'?s?|the\s+latest)\s+(edition|issue|newsletter)\b',
    r'\bthis\s+subscriber.{0,5}only\b',
    r'\beBook\s+(is\s+)?available\b',
    r'©\s*20\d\d',
]), re.IGNORECASE)

# Sources known to publish newsletters disguised as articles
_NEWSLETTER_SOURCES = {'MIT Tech Review', 'The Verge', 'WIRED'}


def quality_filter(items):
    """
    Multi-layer quality filter. Returns (kept, dropped_count, drop_reasons).
    Layers:
      1. Title pattern blocklist
      2. Title too short
      3. Description is newsletter/subscribe boilerplate
      4. No content at all (no image AND no desc) — except HN which is title-only by design
      5. Tiny/broken image (keep item but clear image field)
      6. Age check — drop items older than MAX_AGE_DAYS
    """
    kept = []
    reasons = {}
    now = datetime.now(timezone.utc)

    for item in items:
        title  = item.get('title', '').strip()
        desc   = item.get('desc', '').strip()
        source = item.get('source', '')
        cat    = item.get('category', '')

        # 1. Title blocklist
        if _TRASH.search(title):
            reasons['title_blocklist'] = reasons.get('title_blocklist', 0) + 1
            continue

        # 2. Title too short — genuine news titles are rarely < 20 chars
        if len(title) < 20:
            reasons['title_too_short'] = reasons.get('title_too_short', 0) + 1
            continue

        # 3. Description is boilerplate
        if _TRASH_DESC.search(desc):
            item['desc'] = ''  # clear it, don't drop the whole item
            reasons['desc_boilerplate_cleared'] = reasons.get('desc_boilerplate_cleared', 0) + 1

        # 4. No useful content at all (skip for HN — intentionally title-only)
        if source != 'Hacker News' and not item.get('image') and len(desc) < 30:
            # Give Reddit posts a pass if they have a score (community validated)
            if not (source.startswith('r/') and item.get('score', 0) > 100):
                reasons['no_content'] = reasons.get('no_content', 0) + 1
                continue

        # 5. Fix bad images — clear rather than drop
        if item.get('image') and not is_valid_image(item['image']):
            item['image'] = None
            reasons['bad_image_cleared'] = reasons.get('bad_image_cleared', 0) + 1

        # 6. Age check
        try:
            age_days = (now - datetime.fromisoformat(
                item['ts'].replace('Z', '+00:00'))).total_seconds() / 86400
            if age_days > MAX_AGE_DAYS:
                reasons['too_old'] = reasons.get('too_old', 0) + 1
                continue
        except Exception:
            pass

        kept.append(item)

    return kept, sum(reasons.values()), reasons


def source_cap(items, cap=MAX_PER_SOURCE):
    """Enforce per-source item cap so no single source dominates."""
    counts = {}
    kept = []
    for item in items:
        src = item['source']
        counts[src] = counts.get(src, 0) + 1
        if counts[src] <= cap:
            kept.append(item)
    dropped = len(items) - len(kept)
    return kept, dropped


def dedup_by_id(items):
    seen = set()
    out  = []
    for item in items:
        if item['id'] not in seen:
            seen.add(item['id'])
            out.append(item)
    return out


# ─── Sources ───────────────────────────────────────────────────────────────────

RSS_SOURCES = [
    # World news
    ('https://feeds.bbci.co.uk/news/world/rss.xml',          'BBC World',         'news'),
    ('https://feeds.bbci.co.uk/news/rss.xml',                 'BBC News',          'news'),
    ('https://rss.nytimes.com/services/xml/rss/nyt/World.xml','NYT World',         'news'),
    ('https://www.theguardian.com/world/rss',                  'The Guardian',      'news'),
    ('https://www.aljazeera.com/xml/rss/all.xml',             'Al Jazeera',        'news'),
    # Tech
    ('https://www.theverge.com/rss/index.xml',                'The Verge',         'tech'),
    ('https://feeds.arstechnica.com/arstechnica/index',       'Ars Technica',      'tech'),
    ('https://feeds.bbci.co.uk/news/technology/rss.xml',      'BBC Tech',          'tech'),
    ('https://techcrunch.com/feed/',                           'TechCrunch',        'tech'),
    ('https://www.wired.com/feed/rss',                        'WIRED',             'tech'),
    ('https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml', 'NYT Tech',   'tech'),
    # Gaming
    ('https://www.gamespot.com/feeds/mashup/',                'GameSpot',          'gaming'),
    ('https://kotaku.com/rss',                                 'Kotaku',            'gaming'),
    ('https://www.pcgamer.com/rss/',                          'PC Gamer',          'gaming'),
    ('https://www.eurogamer.net/feed',                        'Eurogamer',         'gaming'),
    ('https://www.rockpapershotgun.com/feed',                 'Rock Paper Shotgun','gaming'),
    # Weird & Wild
    ('https://feeds.bbci.co.uk/news/science_and_environment/rss.xml', 'BBC Science', 'weird'),
    ('https://www.newscientist.com/feed/home/',               'New Scientist',     'weird'),
    ('https://www.livescience.com/feeds/all',                 'Live Science',      'weird'),
    ('https://rss.nytimes.com/services/xml/rss/nyt/Science.xml', 'NYT Science',   'weird'),
    ('https://www.smithsonianmag.com/rss/latest_articles/',   'Smithsonian',       'weird'),
    ('https://www.atlasobscura.com/feeds/latest',             'Atlas Obscura',     'weird'),
    ('https://futurism.com/feed',                             'Futurism',          'weird'),
    ('https://www.popsci.com/rss.xml',                        'Popular Science',   'weird'),
    ('https://gizmodo.com/rss',                               'Gizmodo',           'weird'),
    ('https://www.odditycentral.com/feed',                    'Oddity Central',    'weird'),
    ('https://www.mentalfloss.com/rss.xml',                   'Mental Floss',      'weird'),
    # Memes / humor (non-Reddit)
    ('https://www.boredpanda.com/feed/',                      'Bored Panda',       'meme'),
    ('https://thechive.com/feed/',                            'The Chive',         'meme'),
    ('https://feeds.feedburner.com/icanhascheezburger',       'Cheezburger',       'meme'),
    ('https://lolsnaps.com/feed/',                            'Lolsnaps',          'meme'),
    ('https://hard-drive.net/feed/',                          'Hard Drive',        'meme'),
    ('https://www.theonion.com/rss',                          'The Onion',         'meme'),
    ('https://babylonbee.com/feed',                           'Babylon Bee',       'meme'),
    ('https://reductress.com/feed/',                          'Reductress',        'meme'),
    ('https://twistedsifter.com/feed/',                       'Twisted Sifter',    'meme'),
]

REDDIT_SUBS = [
    # News
    ('worldnews',             'news',   12),
    ('technology',            'tech',   12),
    # Gaming
    ('pcgaming',              'gaming', 10),
    ('Games',                 'gaming', 10),
    # Weird & Wild
    ('Damnthatsinteresting',  'weird',  15),
    ('interestingasfuck',     'weird',  15),
    ('todayilearned',         'weird',  10),
    ('nottheonion',           'weird',  10),
    ('nextfuckinglevel',      'weird',   8),
    ('mildlyinteresting',     'weird',   8),
    # Memes — image-first subs
    ('memes',                 'meme',   15),
    ('dankmemes',             'meme',   12),
    ('me_irl',                'meme',   12),
    ('meirl',                 'meme',   10),
    ('ProgrammerHumor',       'meme',   12),
    ('GameMemes',             'meme',   10),
    ('gaming_memes',          'meme',    8),
    ('TIHI',                  'meme',   10),
    ('HolUp',                 'meme',   10),
    ('shitposting',           'meme',   10),
    ('surrealmemes',          'meme',    8),
    ('cursedimages',          'meme',    8),
    ('facepalm',              'meme',   10),
    ('BrandNewSentence',      'meme',    8),
    ('WhitePeopleTwitter',    'meme',    8),
    ('BlackPeopleTwitter',    'meme',    8),
    ('funny',                 'meme',   10),
]


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f'[{datetime.now()}] Fetching feed...')
    all_items = []

    for url, name, cat in RSS_SOURCES:
        print(f'  {name}...')
        items = parse_rss(url, name, cat)
        all_items.extend(items)

    for sub, cat, lim in REDDIT_SUBS:
        print(f'  r/{sub}...')
        all_items.extend(parse_reddit(sub, cat, lim))
        time.sleep(0.8)

    print('  Hacker News...')
    all_items.extend(parse_hackernews(25))

    before_filter = len(all_items)

    # Dedup first (remove exact URL duplicates from overlapping sources)
    all_items = dedup_by_id(all_items)

    # Quality filter
    all_items, n_dropped, reasons = quality_filter(all_items)
    print(f'  Quality filter: kept {len(all_items)}, dropped {n_dropped}')
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f'    {reason}: {count}')

    # Per-source cap
    all_items, n_capped = source_cap(all_items)
    print(f'  Source cap ({MAX_PER_SOURCE}/source): removed {n_capped} more')

    # Sort newest first
    all_items.sort(key=lambda x: x.get('ts', ''), reverse=True)

    # Stats
    cats = {}
    for i in all_items:
        c = i.get('category', '?')
        cats[c] = cats.get(c, 0) + 1

    result = {
        'updatedAt': datetime.now(timezone.utc).isoformat(),
        'count': len(all_items),
        'items': all_items,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as f:
        json.dump(result, f, indent=2)

    print(f'  Done: {len(all_items)} items '
          f'(from {before_filter} fetched) — '
          + ', '.join(f'{k}:{v}' for k, v in sorted(cats.items())))


if __name__ == '__main__':
    main()
