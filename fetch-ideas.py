#!/usr/bin/env python3
"""
Fetch bite-sized ideas/knowledge from Reddit, Wikipedia, and curated sources.
Outputs to data/ideas.json.

Sources:
  Reddit: TIL, YSK, LPT, Showerthoughts, ELI5, Stoicism, philosophy,
          Damnthatsinteresting, interestingasfuck, AskPhilosophy
  Wikipedia: Featured articles + On This Day
  Quotable API: quotes
  Curated: book stories, psychology/bias cards, philosophy questions
"""

import json
import os
import re
import time
import hashlib
import html as html_mod
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.request import urlopen, Request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SCRIPT_DIR, 'data', 'ideas.json')

REDDIT_UA = 'FocusDashboard/1.0 by focus_dashboard_bot'
OTHER_UA  = 'Mozilla/5.0 (Focus Dashboard/1.0)'

ATOM_NS = {'atom': 'http://www.w3.org/2005/Atom'}

SKIP_TITLE_PATTERNS = re.compile(
    r'megathread|weekly|daily|discussion\s+thread|meta|moderator',
    re.IGNORECASE,
)


def make_id(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:12]


def fetch_url(url, user_agent=OTHER_UA, timeout=10):
    try:
        req = Request(url, headers={'User-Agent': user_agent})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'  FAIL {url}: {e}')
        return None


def strip_html(text):
    cleaned = re.sub(r'<[^>]+>', '', text)
    return html_mod.unescape(cleaned)


def truncate(text, max_len=400):
    text = text.strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(' ')
    if last_space > max_len * 0.6:
        truncated = truncated[:last_space]
    return truncated.rstrip('.,;:!? ') + '...'


def clean_reddit_title(title, subreddit):
    cleaned = title.strip()
    if subreddit == 'todayilearned':
        cleaned = re.sub(r'^TIL\s+that\s+', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^TIL\s+', '', cleaned, flags=re.IGNORECASE)
    elif subreddit == 'YouShouldKnow':
        cleaned = re.sub(r'^YSK:\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^YSK\s+', '', cleaned, flags=re.IGNORECASE)
    elif subreddit == 'LifeProTips':
        cleaned = re.sub(r'^LPT:\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'^LPT\s+', '', cleaned, flags=re.IGNORECASE)
    elif subreddit == 'explainlikeimfive':
        cleaned = re.sub(r'^ELI5[:\s\-]+', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def passes_quality_filter(title, body, subreddit, strict=False):
    if len(title) < 20:
        return False
    if SKIP_TITLE_PATTERNS.search(title):
        return False
    if strict and len(body.strip()) < 60:
        return False
    if not body.strip() and subreddit != 'Showerthoughts':
        return False
    return True


def categorize_til(title):
    history_keywords = re.compile(
        r'war|century|ancient|king|queen|president|empire|medieval|roman|'
        r'dynasty|colonial|revolution|civil war|battle|historical|'
        r'year[s]?\s+(ago|old)|in\s+\d{3,4}|founded|invented\s+in',
        re.IGNORECASE,
    )
    return 'history' if history_keywords.search(title) else 'science'


def parse_reddit_rss(subreddit, category, limit=20, item_type='fact', strict=False):
    url = f'https://www.reddit.com/r/{subreddit}/hot.rss?limit={limit}'
    raw = fetch_url(url, user_agent=REDDIT_UA)
    if not raw:
        return []

    items = []
    try:
        root = ET.fromstring(raw)
        for entry in root.findall('atom:entry', ATOM_NS):
            raw_title = (entry.findtext('atom:title', '', ATOM_NS) or '').strip()
            link_el   = entry.find('atom:link[@href]', ATOM_NS)
            link      = link_el.get('href', '') if link_el is not None else ''
            content_raw = entry.findtext('atom:content', '', ATOM_NS) or ''
            updated   = entry.findtext('atom:updated', '', ATOM_NS) or ''

            if not raw_title or not link:
                continue

            title = clean_reddit_title(raw_title, subreddit)
            body_text = strip_html(content_raw).strip()
            body_text = re.sub(r'\s*submitted\s+by\s+/?u/\S+.*$', '', body_text,
                               flags=re.IGNORECASE | re.DOTALL).strip()
            body_text = re.sub(r'\[link\]|\[comments\]', '', body_text).strip()
            body_text = re.sub(r'\s{2,}', ' ', body_text).strip()
            body_text = truncate(body_text)

            if subreddit == 'Showerthoughts':
                body_text = title
                item_type = 'thought'

            if not body_text.strip() and subreddit in (
                'todayilearned', 'explainlikeimfive', 'YouShouldKnow', 'LifeProTips',
            ):
                body_text = title

            item_category = category
            if subreddit == 'todayilearned':
                item_category = categorize_til(title)

            if not passes_quality_filter(title, body_text, subreddit, strict=strict):
                continue

            ts = None
            if updated:
                try:
                    ts = datetime.fromisoformat(updated.replace('Z', '+00:00')).isoformat()
                except Exception:
                    pass

            items.append({
                'id':       make_id(link),
                'title':    title,
                'body':     body_text,
                'source':   f'r/{subreddit}',
                'category': item_category,
                'type':     item_type,
                'url':      link,
                'ts':       ts or datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f'  RSS parse error r/{subreddit}: {e}')

    return items


def fetch_wikipedia_featured():
    url = (
        'https://en.wikipedia.org/w/api.php'
        '?action=featuredfeed&feed=featured&feedformat=atom'
    )
    raw = fetch_url(url, user_agent=OTHER_UA)
    if not raw:
        return []

    items = []
    try:
        root = ET.fromstring(raw)
        for entry in root.findall('atom:entry', ATOM_NS):
            title       = (entry.findtext('atom:title', '', ATOM_NS) or '').strip()
            link_el     = entry.find('atom:link[@href]', ATOM_NS)
            link        = link_el.get('href', '') if link_el is not None else ''
            content_raw = entry.findtext('atom:content', '', ATOM_NS) or ''
            summary_raw = entry.findtext('atom:summary', '', ATOM_NS) or ''
            updated     = entry.findtext('atom:updated', '', ATOM_NS) or ''

            if not title or not link:
                continue

            body_text = strip_html(summary_raw or content_raw).strip()
            body_text = truncate(body_text)

            science_kw = re.compile(
                r'species|molecule|element|physics|chemical|biological|'
                r'planet|star|galaxy|quantum|gene|protein|cell|organism|'
                r'math|theorem|algorithm|computer|software|engine',
                re.IGNORECASE,
            )
            category = 'science' if science_kw.search(title + ' ' + body_text) else 'history'

            ts = None
            if updated:
                try:
                    ts = datetime.fromisoformat(updated.replace('Z', '+00:00')).isoformat()
                except Exception:
                    pass

            if not passes_quality_filter(title, body_text, 'wikipedia'):
                continue

            items.append({
                'id':       make_id(link),
                'title':    title,
                'body':     body_text,
                'source':   'Wikipedia',
                'category': category,
                'type':     'article',
                'url':      link,
                'ts':       ts or datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f'  Wikipedia parse error: {e}')

    return items


def fetch_today_in_history(max_items=10):
    now  = datetime.now()
    mm   = now.strftime('%m')
    dd   = now.strftime('%d')
    items = []

    for kind in ('events', 'births'):
        url = f'https://en.wikipedia.org/api/rest_v1/feed/onthisday/{kind}/{mm}/{dd}'
        raw = fetch_url(url, user_agent=OTHER_UA, timeout=12)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            entries = data.get(kind, [])
            for e in entries[:max_items]:
                year  = e.get('year')
                text  = (e.get('text') or '').strip()
                pages = e.get('pages', [])
                link  = pages[0].get('content_urls', {}).get('desktop', {}).get('page', '') if pages else ''

                if not text or not year:
                    continue

                suffix = ' AD' if year > 0 else ' BC'
                display_year = str(abs(year)) + suffix

                items.append({
                    'id':       make_id(f'otd-{year}-{text[:60]}'),
                    'title':    text,
                    'body':     f'On this day in {display_year}.',
                    'source':   'On This Day',
                    'category': 'history',
                    'type':     'history_event',
                    'year':     year,
                    'url':      link,
                    'ts':       datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f'  On This Day parse error ({kind}): {e}')

        if len(items) >= max_items:
            break

    return items[:max_items]


def fetch_quotes_api(limit=30):
    items = []
    url = f'https://api.quotable.io/quotes/random?limit={limit}&minLength=60&maxLength=300'
    raw = fetch_url(url, user_agent=OTHER_UA, timeout=12)
    if not raw:
        raw = fetch_url('https://zenquotes.io/api/quotes', user_agent=OTHER_UA, timeout=12)
        if not raw:
            return items
        try:
            quotes = json.loads(raw)
            for q in quotes[:limit]:
                text   = (q.get('q') or '').strip()
                author = (q.get('a') or '').strip()
                if not text or len(text) < 40:
                    continue
                uid = make_id(text[:80])
                items.append({
                    'id': uid, 'title': f'"{text}"',
                    'body': f'— {author}' if author else '',
                    'source': 'Quote', 'category': 'book', 'type': 'quote',
                    'url': '', 'ts': datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f'  ZenQuotes parse error: {e}')
        return items

    try:
        quotes = json.loads(raw)
        for q in quotes:
            content = (q.get('content') or '').strip()
            author  = (q.get('author') or '').strip()
            tags    = q.get('tags', [])
            if not content or len(content) < 40:
                continue
            phil_tags = {'philosophy', 'stoicism', 'wisdom', 'ethics', 'logic', 'metaphysics'}
            category  = 'philosophy' if any(t.lower() in phil_tags for t in tags) else 'book'
            uid = make_id(content[:80])
            items.append({
                'id': uid, 'title': f'"{content}"',
                'body': f'— {author}' if author else '',
                'source': 'Quote' if category == 'book' else 'Philosophy',
                'category': category, 'type': 'quote',
                'url': '', 'ts': datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f'  Quotable parse error: {e}')

    return items


def fetch_philosophy_reddit():
    items = []
    sources = [
        ('Stoicism',      'philosophy', 'fact'),
        ('philosophy',    'philosophy', 'fact'),
        ('AskPhilosophy', 'philosophy', 'question'),
    ]
    for sub, category, item_type in sources:
        raw = fetch_url(
            f'https://www.reddit.com/r/{sub}/top.rss?limit=12&t=week',
            user_agent=REDDIT_UA,
        )
        if not raw:
            time.sleep(1)
            continue
        try:
            root = ET.fromstring(raw)
            for entry in root.findall('atom:entry', ATOM_NS):
                raw_title   = (entry.findtext('atom:title', '', ATOM_NS) or '').strip()
                link_el     = entry.find('atom:link[@href]', ATOM_NS)
                link        = link_el.get('href', '') if link_el is not None else ''
                content_raw = entry.findtext('atom:content', '', ATOM_NS) or ''
                updated     = entry.findtext('atom:updated', '', ATOM_NS) or ''

                if not raw_title or not link or len(raw_title) < 20:
                    continue

                body_text = strip_html(content_raw).strip()
                body_text = re.sub(r'\s*submitted\s+by\s+/?u/\S+.*$', '', body_text,
                                   flags=re.IGNORECASE | re.DOTALL).strip()
                body_text = re.sub(r'\[link\]|\[comments\]', '', body_text).strip()
                body_text = re.sub(r'\s{2,}', ' ', body_text).strip()
                body_text = truncate(body_text)
                if not body_text:
                    body_text = raw_title

                ts = None
                if updated:
                    try:
                        ts = datetime.fromisoformat(updated.replace('Z', '+00:00')).isoformat()
                    except Exception:
                        pass

                items.append({
                    'id':       make_id(link),
                    'title':    raw_title,
                    'body':     body_text,
                    'source':   f'r/{sub}',
                    'category': category,
                    'type':     item_type,
                    'url':      link,
                    'ts':       ts or datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f'  Reddit RSS parse error r/{sub}: {e}')
        time.sleep(1)

    return items


# ─── Curated Book Stories ─────────────────────────────────────────────────────

BOOK_STORIES = [
    {
        'title': 'Meditations — Marcus Aurelius',
        'body': (
            'A Roman emperor wrote private notes to himself about staying rational under pressure, '
            'never intending them for publication. The core idea: you control nothing except your '
            'own reactions. Wealth, fame, other people\'s opinions — all outside your control. '
            'What you think, how you respond, whether you act with virtue — that\'s yours alone. '
            'Seventeen centuries later, these notes remain the clearest guide to keeping your head '
            'when everything around you is chaos.'
        ),
        'source': 'Meditations · Marcus Aurelius', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Atomic Habits — James Clear',
        'body': (
            'You don\'t rise to the level of your goals — you fall to the level of your systems. '
            'Clear argues that a 1% improvement every day compounds to 37x better in a year, while '
            'a 1% decline compounds to nearly zero. The trick isn\'t motivation; it\'s environment '
            'design. Make good habits obvious and easy, bad habits invisible and hard. '
            'Your identity is the sum of your habits: every time you act, you cast a vote for the '
            'person you want to become.'
        ),
        'source': 'Atomic Habits · James Clear', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Sapiens — Yuval Noah Harari',
        'body': (
            'How did one unremarkable ape come to dominate the entire planet? Harari\'s answer: '
            'fiction. Humans uniquely cooperate in massive numbers because we share myths — money, '
            'nations, religions, companies. None of these exist in nature. A dollar bill is worthless '
            'unless millions agree it isn\'t. The Agricultural Revolution, often called progress, '
            'may have made the average human\'s life worse (harder work, worse diet, more disease) '
            'while making the species more powerful. History is not the story of inevitable progress.'
        ),
        'source': 'Sapiens · Yuval Noah Harari', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Thinking, Fast and Slow — Daniel Kahneman',
        'body': (
            'Your brain runs two systems: System 1 is fast, intuitive, emotional — it answers '
            '"2+2" instantly and jumps to conclusions. System 2 is slow, deliberate, effortful — '
            'it does long division and considers evidence. The problem: System 1 runs almost '
            'everything, including decisions you think System 2 is making. Anchoring, loss aversion, '
            'overconfidence — these aren\'t bugs in stupid people. They\'re features of the human '
            'brain that even experts can\'t escape. Awareness is the first step to better judgment.'
        ),
        'source': 'Thinking, Fast and Slow · Daniel Kahneman', 'category': 'book', 'type': 'story',
    },
    {
        'title': '1984 — George Orwell',
        'body': (
            'In Oceania, the Party doesn\'t just control actions — it controls thought. '
            'The protagonist Winston keeps a secret diary, an act of rebellion. '
            'The genius of the novel isn\'t the surveillance or the violence — it\'s the insight '
            'that totalitarianism requires willing participants. Doublethink (holding two '
            'contradictory beliefs simultaneously) isn\'t imposed from outside; it grows from '
            'inside. Written in 1949, it remains the most precise map of how propaganda, '
            'language manipulation, and manufactured fear work on a population.'
        ),
        'source': '1984 · George Orwell', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'The Alchemist — Paulo Coelho',
        'body': (
            'A young shepherd leaves Spain to follow a recurring dream about treasure near the '
            'Egyptian pyramids. Every person he meets tries to dissuade him. He\'s robbed, '
            'enslaved, nearly killed. The treasure, when found, turns out to have been near his '
            'home all along. The point isn\'t the destination — it\'s that the journey taught him '
            'to read omens, trust himself, and understand that the universe conspires to help those '
            'who pursue their Personal Legend. The soul of the world speaks to those who listen.'
        ),
        'source': 'The Alchemist · Paulo Coelho', 'category': 'book', 'type': 'story',
    },
    {
        'title': "Man's Search for Meaning — Viktor Frankl",
        'body': (
            'A psychiatrist survives Auschwitz and notices that prisoners who kept a reason to '
            'live — a person waiting for them, a book to write, a goal to achieve — survived longer '
            'than those who lost hope. His conclusion: the last human freedom is the ability to '
            'choose your attitude toward any circumstance. He developed logotherapy from this: '
            'the primary human drive isn\'t pleasure (Freud) or power (Adler) but meaning. '
            'Suffering without meaning is unbearable; suffering with meaning is endurable.'
        ),
        'source': "Man's Search for Meaning · Viktor Frankl", 'category': 'book', 'type': 'story',
    },
    {
        'title': 'The Art of War — Sun Tzu',
        'body': (
            'Written 2,500 years ago for Chinese military commanders, this 13-chapter text is '
            'still taught in business schools. The central insight: winning without fighting is '
            'the highest skill. Know yourself and your enemy — in a hundred battles you will '
            'never be in peril. Speed, deception, and flexibility beat brute strength. '
            'The best general appears to do nothing while everything happens. '
            'Every chapter has been applied to negotiation, sports, politics, and startups — '
            'because the underlying game theory is universal.'
        ),
        'source': 'The Art of War · Sun Tzu', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Brave New World — Aldous Huxley',
        'body': (
            'In 2540 AD, humanity is perfectly stable — and perfectly empty. People are engineered '
            'before birth into castes, conditioned to love their role, kept happy with soma '
            '(a pleasure drug with no side effects). Nobody suffers, nobody rebels, nobody thinks '
            'deeply. The horror Huxley depicts isn\'t tyranny through pain — it\'s control through '
            'comfort and pleasure. The question the book asks: would you trade meaning, struggle, '
            'and authentic choice for guaranteed happiness? The Savage chooses suffering. '
            'Most characters don\'t understand why he would.'
        ),
        'source': 'Brave New World · Aldous Huxley', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Siddhartha — Hermann Hesse',
        'body': (
            'A young Brahmin leaves a perfect life to find enlightenment. He tries asceticism — '
            'nearly starves. He tries sensual pleasure — grows empty. He tries business and '
            'wealth — feels hollow. Eventually he becomes a ferryman, listening to the river. '
            'The river teaches him: all time exists simultaneously, the end is the beginning, '
            'and the journey is the destination. Enlightenment can\'t be taught — only experienced. '
            'The novel argues that every path, including wrong ones, is the right path '
            'if you pay attention to what it teaches you.'
        ),
        'source': 'Siddhartha · Hermann Hesse', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'The Power of Now — Eckhart Tolle',
        'body': (
            'Most human suffering comes from living in the past (regret, resentment) or the '
            'future (anxiety, worry). The present moment — right now — is the only place where '
            'life actually happens, yet we spend almost no time there. Tolle argues the '
            '"pain body" (accumulated emotional pain) feeds on past and future thinking. '
            'The practice: notice when you\'re thinking obsessively, observe the thinker, '
            'and return to the present sensation. Simple to understand, surprisingly difficult '
            'to do, transformative when it sticks.'
        ),
        'source': 'The Power of Now · Eckhart Tolle', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Crime and Punishment — Dostoevsky',
        'body': (
            'A brilliant but impoverished student murders a pawnbroker, convinced that '
            'extraordinary people are above moral law — and that the money will fund good deeds. '
            'The murder takes two pages. The remaining 500 are about the psychological collapse '
            'that follows. Dostoevsky\'s insight: guilt doesn\'t come from God or society — '
            'it comes from the rupture of your own nature. You can rationalize almost any act '
            'beforehand. Afterward, the rationalization dissolves and you\'re left with what '
            'you actually did. The book is the most thorough portrait of a guilty conscience '
            'ever written.'
        ),
        'source': 'Crime and Punishment · Dostoevsky', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Dune — Frank Herbert',
        'body': (
            'On a desert planet that produces the most valuable substance in the universe, '
            'a young nobleman becomes the messianic leader of the indigenous people — '
            'and Herbert spends 600 pages questioning whether that\'s actually good. '
            'Dune is a warning about charismatic leaders and savior narratives. '
            'Paul achieves everything the prophecy promised and sets in motion a holy war '
            'that will kill billions. The ecology of Arrakis mirrors human civilization\'s '
            'relationship with oil. Every political, religious, and environmental theme '
            'written in 1965 became more relevant with time.'
        ),
        'source': 'Dune · Frank Herbert', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'The Subtle Art of Not Giving a F*ck — Mark Manson',
        'body': (
            'Self-help usually tells you to think more positively. Manson argues the opposite: '
            'life involves suffering and struggle by definition, so the question isn\'t how to '
            'avoid problems but which problems are worth having. You have a limited number of '
            '"f*cks" to give — choose them deliberately. Happiness comes not from achieving '
            'everything but from choosing to care about things that align with your values. '
            'The desire to have no problems is itself a problem. Growth is about choosing '
            'better problems, not eliminating them.'
        ),
        'source': 'The Subtle Art · Mark Manson', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Influence — Robert Cialdini',
        'body': (
            'Six principles govern almost every persuasion in human society: reciprocity '
            '(we repay what we receive), commitment (we act consistently with prior commitments), '
            'social proof (we follow the crowd), authority (we obey experts), liking '
            '(we say yes to people we like), and scarcity (we want what\'s rare). '
            'Salespeople, politicians, marketers, and cults use these automatically. '
            'The book was written so you could recognize them being used on you — '
            'and decide whether to comply consciously rather than reflexively.'
        ),
        'source': 'Influence · Robert Cialdini', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'The 48 Laws of Power — Robert Greene',
        'body': (
            'Greene studied the most powerful and powerless people in history and extracted '
            '48 laws from their patterns. Some are counterintuitive: never outshine the master, '
            'use absence to increase respect, keep others dependent on you. '
            'The book is controversial because it\'s amoral — it describes how power actually '
            'works, not how we wish it did. The argument for reading it: these dynamics operate '
            'whether you acknowledge them or not. Understanding them protects you '
            'from being on the wrong side. Court politics never really ended — '
            'they moved to offices.'
        ),
        'source': 'The 48 Laws of Power · Robert Greene', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'Zero to One — Peter Thiel',
        'body': (
            'Going from 0 to 1 means creating something genuinely new. Going from 1 to n '
            'means copying what already works. Thiel argues that competition is for losers — '
            'monopolies drive real profit and real innovation. The best startups find a secret: '
            'something true that almost nobody believes. Great companies build a cult-like '
            'culture, solve a specific problem in a small market first, then expand. '
            'The most contrarian question you can ask: what important truth do very few people '
            'agree with you on?'
        ),
        'source': 'Zero to One · Peter Thiel', 'category': 'book', 'type': 'story',
    },
    {
        'title': 'The Black Swan — Nassim Taleb',
        'body': (
            'Before 1697, Europeans assumed all swans were white — until they found black ones '
            'in Australia. Taleb uses this to describe high-impact, hard-to-predict events that '
            'are rationalized in hindsight as if they were predictable. The 2008 financial crisis, '
            '9/11, the internet — all Black Swans. Most human planning ignores these outliers, '
            'yet they drive most of history. The lesson: don\'t try to predict Black Swans. '
            'Instead, build systems that are antifragile — that gain from disorder '
            'rather than merely surviving it.'
        ),
        'source': 'The Black Swan · Nassim Taleb', 'category': 'book', 'type': 'story',
    },
]


def get_book_stories():
    items = []
    for card in BOOK_STORIES:
        uid = make_id(card['title'][:80])
        items.append({
            'id':       uid,
            'title':    card['title'],
            'body':     card['body'],
            'source':   card['source'],
            'category': card['category'],
            'type':     card.get('type', 'story'),
            'url':      '',
            'ts':       datetime.now(timezone.utc).isoformat(),
        })
    return items


# ─── Curated Philosophy Questions ─────────────────────────────────────────────

PHILOSOPHY_QUESTIONS = [
    {
        'title': 'The Trolley Problem: Would you pull the lever?',
        'body': 'A runaway trolley is heading toward five people tied to the tracks. You can pull a lever to divert it — but it will kill one person on the other track. You do nothing and five die. You act and one dies by your hand. What do you do?',
        'perspectives': {
            'a': 'Utilitarian: Pull the lever. Five lives outweigh one. Morality is about outcomes — the right action produces the most good for the most people. Inaction that allows greater harm is itself a moral choice.',
            'b': 'Deontological: Do not pull. You have no right to use one person as a means to save others. There is a moral difference between killing and letting die. Your hands must stay clean of the direct act.',
        },
        'think_about': 'When did you last make a decision where inaction was itself a choice? What was the difference between acting and not acting?',
        'source': 'Philippa Foot, 1967', 'category': 'philosophy',
    },
    {
        'title': "Ship of Theseus: Are you still you?",
        'body': "If every plank of Theseus's ship is replaced over time, is it still the same ship? Now apply it to yourself: every cell in your body is replaced over years, your beliefs and personality shift, your memories reconstruct each time you recall them. Are you the same person you were at age 10?",
        'perspectives': {
            'a': 'Continuity theory: Identity persists through unbroken psychological continuity — memories, personality, the narrative thread. The ship is the same ship if it maintained continuous existence and function, not because of its matter.',
            'b': 'Materialist view: Identity is the configuration of matter at a specific moment. Once the planks are replaced, it\'s a different ship with a shared history. "You" from ten years ago is someone else who shares your memories.',
        },
        'think_about': 'Think of a major belief you\'ve changed. Was the version of you that held the old belief a different person, or the same one in an earlier state?',
        'source': 'Plutarch, 1st century AD', 'category': 'philosophy',
    },
    {
        'title': "Simulation Hypothesis: Does it matter if this is real?",
        'body': "Nick Bostrom argues that at least one of three things is true: civilizations go extinct before creating realistic simulations; advanced civilizations have no interest in running them; or we almost certainly live in one. If computing power grows indefinitely, simulated minds will vastly outnumber biological ones. How would you live differently if you knew this was a simulation?",
        'perspectives': {
            'a': 'It changes nothing. The experiences, relationships, and pain are real to you regardless of the substrate. A simulated sunset is still beautiful. Meaning comes from within the system, not from its ultimate foundation.',
            'b': 'It changes everything. If this is a simulation, there may be an outside — an escape, a purpose, a programmer with intentions. The question of what the simulation is for becomes the most important question possible.',
        },
        'think_about': 'Is there anything you would do differently if you knew with certainty this was or wasn\'t a simulation? Why does that difference exist?',
        'source': 'Nick Bostrom, 2003', 'category': 'philosophy',
    },
    {
        'title': "The Experience Machine: Would you plug in?",
        'body': "Robert Nozick asks: imagine a machine that gives you any experiences you want — a perfect career, love, adventure, everything feeling real. You can plug in for life and never know the difference. Most people refuse. Why? This reveals that humans value truth and genuine connection over pure pleasure.",
        'perspectives': {
            'a': 'Plug in. If subjective experience is all you ever have access to anyway, why would the "realness" of the source matter? A life of full joy and meaning in the machine is objectively better than a life of struggle outside it.',
            'b': "Don't plug in. We care about actually doing things, not just having the experience of doing them. We want to really love someone, really create something, really matter. The simulation would be a beautiful lie you can never share.",
        },
        'think_about': 'Is there something in your life you value not for how it feels but because it\'s real? What would be lost if you discovered it was a performance?',
        'source': 'Robert Nozick, Anarchy, State, and Utopia, 1974', 'category': 'philosophy',
    },
    {
        'title': "Schrödinger's Ethics: Is a secret crime still wrong?",
        'body': "If you could commit a crime with absolute certainty of never being caught — no consequences, no guilt, no witnesses, perfect anonymity forever — would it still be wrong? This question separates people who believe morality is external (rules, consequences, God) from those who believe it is internal (character, virtue, integrity).",
        'perspectives': {
            'a': "Yes, absolutely. Morality isn't about consequences — it's about who you are. Every act shapes your character whether witnessed or not. The person who would steal in secret is already a thief; they just haven't acted yet.",
            'b': 'Morality is a social contract. Without society to harm, without consequences to weigh, the framework breaks down. The question is unanswerable because morality only exists in relation to others — alone, it\'s meaningless.',
        },
        'think_about': 'Have you ever done the right thing when nobody was watching and it cost you something? What motivated that choice?',
        'source': 'Classic ethical thought experiment', 'category': 'philosophy',
    },
]


def get_philosophy_questions():
    items = []
    for q in PHILOSOPHY_QUESTIONS:
        uid = make_id(q['title'][:80])
        items.append({
            'id':           uid,
            'title':        q['title'],
            'body':         q['body'],
            'source':       q['source'],
            'category':     q['category'],
            'type':         'question',
            'perspectives': q['perspectives'],
            'think_about':  q['think_about'],
            'url':          '',
            'ts':           datetime.now(timezone.utc).isoformat(),
        })
    return items


# ─── Curated Psychology / Cognitive Bias Cards ────────────────────────────────

PSYCHOLOGY_CARDS = [
    {
        'title': 'The Dunning-Kruger Effect',
        'body': 'People with limited knowledge in a domain overestimate their competence, while experts underestimate theirs. The less you know, the less you know you don\'t know. The cure: actively seek out what you\'re wrong about. Expertise feels like uncertainty, not confidence.',
        'source': 'Kruger & Dunning, 1999',
    },
    {
        'title': 'Sunk Cost Fallacy',
        'body': 'We continue investing in failing projects because of what we\'ve already put in — not because of future value. The money, time, or effort already spent is gone regardless of what you do next. Every decision should be made on future costs and benefits only. The past is irrelevant.',
        'source': 'Behavioral Economics',
    },
    {
        'title': 'Confirmation Bias',
        'body': 'We seek, interpret, and remember information that confirms what we already believe. We\'re not rational agents evaluating evidence — we\'re lawyers building a case for our prior beliefs. The antidote: actively steel-man opposing views. Seek disconfirmation, not confirmation.',
        'source': 'Peter Wason, 1960',
    },
    {
        'title': 'The Spotlight Effect',
        'body': 'You vastly overestimate how much other people notice your mistakes, appearance, and behavior. In reality, everyone is too busy worrying about their own spotlight. This bias causes social anxiety, self-consciousness, and paralysis. Most people aren\'t watching you nearly as much as you think.',
        'source': 'Gilovich, Medvec & Savitsky, 2000',
    },
    {
        'title': 'Anchoring Bias',
        'body': 'The first number you hear disproportionately influences your judgment. If a salesperson says "this used to cost $1,000" before offering it at $400, the $1,000 anchor makes $400 feel like a deal — even if it\'s worth $150. Awareness doesn\'t fully protect you; the anchor still pulls.',
        'source': 'Tversky & Kahneman, 1974',
    },
    {
        'title': 'Loss Aversion',
        'body': 'Losing $100 feels roughly twice as bad as gaining $100 feels good. This asymmetry explains why people hold losing investments too long, avoid necessary risks, and cling to bad situations. Evolution wired us to weight losses heavily — starvation killed; missing a feast didn\'t. Modern life has different stakes.',
        'source': 'Kahneman & Tversky, 1979',
    },
    {
        'title': 'The Paradox of Choice',
        'body': 'More options make decisions harder and satisfaction lower. When you choose from 6 jams, you\'re satisfied. From 24 jams, you\'re paralyzed — and even if you choose, you second-guess. Every unchosen option haunts you. The solution isn\'t more choice; it\'s constraints, defaults, and satisficing instead of maximizing.',
        'source': 'Barry Schwartz, 2004',
    },
    {
        'title': 'Fundamental Attribution Error',
        'body': 'When others make mistakes, we attribute them to character flaws. When we make the same mistakes, we blame circumstances. The aggressive driver who cut you off is a jerk; when you do it, you were running late and had good reason. This asymmetry poisons relationships and prevents honest self-assessment.',
        'source': 'Lee Ross, 1977',
    },
    {
        'title': 'The Planning Fallacy',
        'body': 'Humans consistently underestimate how long tasks take — even when they have experience with similar tasks. The fix: use "reference class forecasting." Instead of asking "how long will this take me?" ask "how long did this type of task take others?" Outside-view estimates beat inside-view every time.',
        'source': 'Kahneman & Tversky, 1979',
    },
    {
        'title': 'Social Proof',
        'body': 'When uncertain, we look to others to determine correct behavior. This is usually useful — it aggregates social wisdom. But it creates cascades: everyone acts based on what everyone else is doing, and nobody is acting on actual information. Financial bubbles, fashion, panic — all social proof amplifying noise.',
        'source': 'Robert Cialdini, Influence',
    },
    {
        'title': 'The Availability Heuristic',
        'body': 'We judge the probability of events by how easily examples come to mind. Plane crashes are memorable — so we fear flying more than driving, even though driving is far more dangerous. Vivid, recent, emotional events feel more likely than they are. Statistics rarely override a compelling story.',
        'source': 'Tversky & Kahneman, 1973',
    },
    {
        'title': 'Cognitive Dissonance',
        'body': 'Holding two contradictory beliefs creates mental discomfort — and we resolve it by changing one belief, usually the less entrenched one. After buying a car, you unconsciously notice more positive reviews. After an embarrassing initiation, group membership feels more valuable. Behavior shapes belief as much as belief shapes behavior.',
        'source': 'Leon Festinger, 1957',
    },
]


def get_psychology_cards():
    items = []
    for card in PSYCHOLOGY_CARDS:
        uid = make_id(card['title'][:80])
        items.append({
            'id':       uid,
            'title':    card['title'],
            'body':     card['body'],
            'source':   card['source'],
            'category': 'psychology',
            'type':     'bias',
            'url':      '',
            'ts':       datetime.now(timezone.utc).isoformat(),
        })
    return items


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f'[{datetime.now().isoformat()}] Fetching ideas...')
    all_items = []

    # Reddit sources — strict=True for LPT/YSK to require real body text
    reddit_sources = [
        ('todayilearned',        'science',    'fact',    False, 20),
        ('YouShouldKnow',        'life',        'fact',    True,   8),
        ('LifeProTips',          'life',        'fact',    True,   8),
        ('Showerthoughts',       'psychology',  'thought', False, 15),
        ('explainlikeimfive',    'science',     'fact',    False, 15),
        ('Damnthatsinteresting', 'science',     'fact',    False, 15),
        ('interestingasfuck',    'science',     'fact',    False, 15),
    ]

    for subreddit, category, item_type, strict, limit in reddit_sources:
        print(f'  Fetching r/{subreddit}...')
        items = parse_reddit_rss(subreddit, category, limit=limit,
                                 item_type=item_type, strict=strict)
        print(f'    Got {len(items)} ideas')
        all_items.extend(items)
        time.sleep(1)

    # Wikipedia featured articles
    print('  Fetching Wikipedia featured articles...')
    wiki_items = fetch_wikipedia_featured()
    print(f'    Got {len(wiki_items)} ideas')
    all_items.extend(wiki_items)

    # Today in history
    print('  Fetching Today in History...')
    history_items = fetch_today_in_history(max_items=10)
    print(f'    Got {len(history_items)} history events')
    all_items.extend(history_items)

    # Book quotes (Quotable API)
    print('  Fetching book quotes...')
    quote_items = fetch_quotes_api(limit=30)
    print(f'    Got {len(quote_items)} quotes')
    all_items.extend(quote_items)

    # Philosophy & quotes from Reddit
    print('  Fetching philosophy/quotes from Reddit...')
    phil_items = fetch_philosophy_reddit()
    print(f'    Got {len(phil_items)} philosophy ideas')
    all_items.extend(phil_items)

    # Curated content
    print('  Adding curated book stories...')
    all_items.extend(get_book_stories())

    print('  Adding curated philosophy questions...')
    all_items.extend(get_philosophy_questions())

    print('  Adding curated psychology/bias cards...')
    all_items.extend(get_psychology_cards())

    # Deduplicate by id
    seen   = set()
    unique = []
    for item in all_items:
        if item['id'] not in seen:
            seen.add(item['id'])
            unique.append(item)

    # Sort by timestamp (newest first)
    unique.sort(key=lambda x: x.get('ts', ''), reverse=True)

    # Write output
    result = {
        'updatedAt': datetime.now(timezone.utc).isoformat(),
        'date':      datetime.now().strftime('%Y-%m-%d'),
        'count':     len(unique),
        'items':     unique,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as f:
        json.dump(result, f, indent=2)

    print(f'\nDone: {len(unique)} ideas saved to {OUT}')

    cats = {}
    types = {}
    for item in unique:
        cats[item['category']]      = cats.get(item['category'], 0) + 1
        types[item.get('type','?')] = types.get(item.get('type','?'), 0) + 1
    print('Categories:', ', '.join(f'{k}: {v}' for k, v in sorted(cats.items())))
    print('Types:     ', ', '.join(f'{k}: {v}' for k, v in sorted(types.items())))


if __name__ == '__main__':
    main()
