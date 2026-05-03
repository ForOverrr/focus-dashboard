#!/usr/bin/env python3
"""
AI enrichment for ideas/learn feed — runs after fetch-ideas.py.

  Tier 1 — Quality scoring: rates every idea 1–10 for insight value
  Tier 2 — Book summary generation: generates new book story summaries
  Tier 3 — Philosophy question generation: generates new thought experiments
  Tier 4 — Daily digest selection: curates 7 best items for today

Pass --daily flag to force fresh generation even if cache is warm.
"""

import json, os, re, sys, time, hashlib
from datetime import datetime, timezone

IDEAS_FILE = '/root/.openclaw/workspace-todo/dashboard/data/ideas.json'
CACHE_FILE = '/root/.openclaw/workspace-todo/dashboard/data/ideas_ai_cache.json'

NIM_KEY    = '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'
NIM_URL    = 'https://integrate.api.nvidia.com/v1/chat/completions'
FAST_MODEL  = 'meta/llama-3.1-8b-instruct'
SMART_MODEL = 'meta/llama-3.3-70b-instruct'

SCORE_BATCH  = 15
API_TIMEOUT  = 25
MAX_AI_CALLS = 30   # hard cap per run

DAILY_FORCE = '--daily' in sys.argv

# Books not yet in the curated list — AI will pick from these
AI_BOOK_POOL = [
    'The Prince by Machiavelli',
    'The Republic by Plato',
    'Walden by Henry David Thoreau',
    'On Liberty by John Stuart Mill',
    'The Communist Manifesto by Marx & Engels',
    'The Wealth of Nations by Adam Smith',
    'Nicomachean Ethics by Aristotle',
    'The Tao Te Ching by Laozi',
    'Thus Spoke Zarathustra by Nietzsche',
    'Beyond Good and Evil by Nietzsche',
    'The Brothers Karamazov by Dostoevsky',
    'Anna Karenina by Tolstoy',
    'The Trial by Kafka',
    'Catch-22 by Joseph Heller',
    'Fahrenheit 451 by Ray Bradbury',
    'Slaughterhouse-Five by Vonnegut',
    'Lord of the Flies by Golding',
    'The Stranger by Camus',
    'Nausea by Sartre',
    'The Myth of Sisyphus by Camus',
    'Antifragile by Nassim Taleb',
    'The 7 Habits of Highly Effective People by Covey',
    'Deep Work by Cal Newport',
    'Mindset by Carol Dweck',
    'The 4-Hour Workweek by Tim Ferriss',
    'Start with Why by Simon Sinek',
    'Thinking in Systems by Donella Meadows',
    'The Design of Everyday Things by Don Norman',
    'The Innovator\'s Dilemma by Clayton Christensen',
    'Good to Great by Jim Collins',
    'Flow by Mihaly Csikszentmihalyi',
    'Grit by Angela Duckworth',
    'The Body Keeps the Score by Bessel van der Kolk',
    'When Breath Becomes Air by Paul Kalanithi',
    'The Checklist Manifesto by Atul Gawande',
    'Surely You\'re Joking Mr. Feynman',
    'A Brief History of Time by Stephen Hawking',
    'The Selfish Gene by Richard Dawkins',
    'Cosmos by Carl Sagan',
    'The Gene by Siddhartha Mukherjee',
]

# ─── NIM client ───────────────────────────────────────────────────────────────

def nim_call(model, system, user, max_tokens=300, temp=0.4, retries=2):
    from urllib.request import urlopen, Request
    import urllib.error
    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user',   'content': user},
        ],
        'max_tokens': max_tokens,
        'temperature': temp,
        'stream': False,
    }).encode()
    for attempt in range(retries + 1):
        try:
            req = Request(NIM_URL, data=payload, headers={
                'Authorization': f'Bearer {NIM_KEY}',
                'Content-Type':  'application/json',
                'Accept':        'application/json',
            })
            from urllib.request import urlopen
            with urlopen(req, timeout=API_TIMEOUT) as r:
                data = json.loads(r.read())
            return data['choices'][0]['message']['content'].strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            print(f'    NIM HTTP {e.code}: {body}')
            if e.code in (429, 503):
                time.sleep(5 * (attempt + 1))
            else:
                return None
        except Exception as e:
            if attempt < retries:
                time.sleep(3)
            else:
                print(f'    NIM error: {e}')
                return None
    return None

# ─── Cache ────────────────────────────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

def make_id(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:12]

# ─── Tier 1: Scoring ──────────────────────────────────────────────────────────

SCORE_SYSTEM = (
    'You score learning content by insight value for a curious adult reader. '
    'Consider originality, depth, and how thought-provoking the idea is.\n'
    '10 — mind-expanding, counterintuitive, or genuinely profound\n'
    ' 8 — very insightful, well-expressed, makes you think differently\n'
    ' 6 — solid, clear, worth reading once\n'
    ' 4 — generic, surface-level, or obvious\n'
    ' 2 — trivial, repetitive, or not insightful\n'
    'Reply with ONLY comma-separated integers in input order. No text.'
)

def score_batch(items_batch):
    lines = '\n'.join(
        f'{i+1}. [{it.get("type","?")}] {it["title"]}: {it.get("body","")[:120]}'
        for i, it in enumerate(items_batch)
    )
    raw = nim_call(FAST_MODEL, SCORE_SYSTEM, lines,
                   max_tokens=len(items_batch) * 4, temp=0.1)
    if not raw:
        return [5] * len(items_batch)
    nums   = re.findall(r'\d+', raw)
    scores = [max(1, min(10, int(n))) for n in nums[:len(items_batch)]]
    while len(scores) < len(items_batch):
        scores.append(5)
    return scores

def run_scoring(items, cache, call_counter):
    to_score = [i for i in items if cache.get(i['id'], {}).get('score') is None]
    if not to_score:
        print(f'  Scoring: all {len(items)} already cached')
    else:
        print(f'  Scoring {len(to_score)} items in batches of {SCORE_BATCH}...')
        t0 = time.time()
        for start in range(0, len(to_score), SCORE_BATCH):
            if call_counter[0] >= MAX_AI_CALLS:
                print('  Hit MAX_AI_CALLS cap — stopping scoring')
                break
            batch  = to_score[start:start + SCORE_BATCH]
            scores = score_batch(batch)
            call_counter[0] += 1
            for item, score in zip(batch, scores):
                cache.setdefault(item['id'], {})['score'] = score
            time.sleep(0.5)
        print(f'  Scoring done in {time.time()-t0:.1f}s ({call_counter[0]} calls used)')

    for item in items:
        s = cache.get(item['id'], {}).get('score')
        if s is not None:
            item['ai_score'] = s

    return items

# ─── Tier 2: AI book summary generation ──────────────────────────────────────

BOOK_SUMM_SYSTEM = (
    'You are a literary guide. Given a book title and author, write a compelling '
    '2-3 paragraph summary (250-400 characters) that covers:\n'
    '1. The core story or central argument\n'
    '2. The key insight or lesson\n'
    '3. Why it matters to a modern reader\n'
    'Write in an engaging, direct style. No spoiler warnings. '
    'Reply as a JSON object: {"title": "Book — Author", "body": "summary text", '
    '"source": "Book Title · Author"}'
)

def generate_book_summaries(cache, call_counter, n=2):
    if call_counter[0] >= MAX_AI_CALLS:
        return []

    # Find books not yet generated
    generated_books = set(cache.get('generated_books', []))
    available = [b for b in AI_BOOK_POOL if b not in generated_books]
    if not available:
        print('  Book gen: all books already generated')
        return []

    import random
    picks = random.sample(available[:20], min(n, len(available)))
    new_items = []

    for book in picks:
        if call_counter[0] >= MAX_AI_CALLS:
            break
        print(f'  Generating summary for: {book}')
        raw = nim_call(SMART_MODEL, BOOK_SUMM_SYSTEM, book,
                       max_tokens=500, temp=0.5)
        call_counter[0] += 1
        time.sleep(1)

        if not raw:
            continue
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
            title  = str(data.get('title', book)).strip()
            body   = str(data.get('body', '')).strip()
            source = str(data.get('source', book)).strip()
            if not body or len(body) < 100:
                continue
            uid = make_id(title[:80])
            new_items.append({
                'id':       uid,
                'title':    title,
                'body':     body,
                'source':   source,
                'category': 'book',
                'type':     'story',
                'url':      '',
                'ts':       datetime.now(timezone.utc).isoformat(),
                'ai_generated': True,
            })
            generated_books.add(book)
        except Exception as e:
            print(f'    Parse error: {e}')

    cache['generated_books'] = list(generated_books)
    print(f'  Generated {len(new_items)} new book summaries')
    return new_items

# ─── Tier 3: AI philosophy question generation ────────────────────────────────

PHIL_Q_SYSTEM = (
    'Generate a thought-provoking philosophical question or ethical dilemma. '
    'Make it specific and concrete, not abstract. '
    'Reply as JSON: {'
    '"title": "short punchy question title (max 60 chars)", '
    '"body": "the scenario or question in 2-3 sentences (120-250 chars)", '
    '"perspective_a": "first perspective, 50-80 words", '
    '"perspective_b": "contrasting perspective, 50-80 words", '
    '"think_about": "a personal reflection prompt connecting it to daily life (30-50 words)"'
    '}'
)

def generate_philosophy_questions(cache, call_counter, n=1):
    if call_counter[0] >= MAX_AI_CALLS:
        return []

    today = datetime.now().strftime('%Y-%m-%d')
    cache_key = f'phil_q_date:{today}'
    if not DAILY_FORCE and cache.get(cache_key):
        print('  Philosophy Q: already generated today')
        return []

    new_items = []
    themes = [
        'free will and determinism',
        'the nature of consciousness',
        'personal identity over time',
        'moral luck',
        'the ethics of technology',
        'what we owe to future generations',
        'the limits of knowledge',
        'the ethics of artificial intelligence',
        'whether objective morality exists',
        'the meaning of suffering',
    ]
    import random
    theme = random.choice(themes)

    print(f'  Generating philosophy question on: {theme}')
    raw = nim_call(SMART_MODEL, PHIL_Q_SYSTEM,
                   f'Theme: {theme}. Make it fresh and specific.',
                   max_tokens=600, temp=0.7)
    call_counter[0] += 1
    time.sleep(1)

    if raw:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                title   = str(data.get('title', '')).strip()
                body    = str(data.get('body', '')).strip()
                pa      = str(data.get('perspective_a', '')).strip()
                pb      = str(data.get('perspective_b', '')).strip()
                think   = str(data.get('think_about', '')).strip()

                if title and body and pa and pb:
                    uid = make_id(title[:80])
                    new_items.append({
                        'id':           uid,
                        'title':        title,
                        'body':         body,
                        'source':       'AI Philosophy',
                        'category':     'philosophy',
                        'type':         'question',
                        'perspectives': {'a': pa, 'b': pb},
                        'think_about':  think,
                        'url':          '',
                        'ts':           datetime.now(timezone.utc).isoformat(),
                        'ai_generated': True,
                    })
                    cache[cache_key] = uid
                    print(f'  Generated philosophy question: {title[:60]}')
            except Exception as e:
                print(f'    Parse error: {e}')

    return new_items

# ─── Tier 4: Daily digest selection ──────────────────────────────────────────

DIGEST_TYPES_PREFERRED = {'story', 'question', 'history_event', 'bias'}

def build_daily_digest(items):
    today = datetime.now().strftime('%Y-%m-%d')

    # Score-based filtering: prefer high scorers, but include type diversity
    scored = sorted(items, key=lambda i: i.get('ai_score', 5), reverse=True)

    digest = []
    cat_count = {}
    type_seen = set()

    # First pass: pick rich content types with score >= 6
    for item in scored:
        if len(digest) >= 7:
            break
        if item.get('type') not in DIGEST_TYPES_PREFERRED:
            continue
        if item.get('ai_score', 5) < 6:
            continue
        cat = item.get('category', 'other')
        if cat_count.get(cat, 0) >= 2:
            continue
        t = item.get('type')
        if t in type_seen and t != 'story':
            continue
        digest.append(item)
        cat_count[cat] = cat_count.get(cat, 0) + 1
        type_seen.add(t)

    # Second pass: fill remaining slots with any high-scoring items
    for item in scored:
        if len(digest) >= 7:
            break
        if item in digest:
            continue
        if item.get('ai_score', 5) < 7:
            continue
        cat = item.get('category', 'other')
        if cat_count.get(cat, 0) >= 2:
            continue
        digest.append(item)
        cat_count[cat] = cat_count.get(cat, 0) + 1

    # Mark digest items
    digest_ids = {i['id'] for i in digest}
    for item in items:
        item['digest']     = item['id'] in digest_ids
        item['digestDate'] = today

    print(f'  Digest: {len(digest)} items selected for {today}')
    return items

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f'[{datetime.now()}] AI enrichment (ideas) starting...')

    with open(IDEAS_FILE) as f:
        feed = json.load(f)
    items = feed['items']
    print(f'  Loaded {len(items)} ideas')

    cache = load_cache()
    original_cache_size = len(cache)
    call_counter = [0]  # mutable counter passed by reference

    # Tier 1: Score all items
    items = run_scoring(items, cache, call_counter)

    # Tier 2: Generate new book summaries
    new_books = generate_book_summaries(cache, call_counter, n=2)
    items.extend(new_books)

    # Tier 3: Generate new philosophy question
    new_questions = generate_philosophy_questions(cache, call_counter, n=1)
    items.extend(new_questions)

    # Tier 4: Build daily digest
    items = build_daily_digest(items)

    # Write output
    feed['items']      = items
    feed['count']      = len(items)
    feed['enrichedAt'] = datetime.now(timezone.utc).isoformat()

    with open(IDEAS_FILE, 'w') as f:
        json.dump(feed, f, indent=2)

    if len(cache) > original_cache_size:
        save_cache(cache)

    # Stats
    scores = [i.get('ai_score', 0) for i in items if i.get('ai_score')]
    if scores:
        avg = sum(scores) / len(scores)
        hi  = sum(1 for s in scores if s >= 8)
        print(f'  Scores: avg={avg:.1f}, high(>=8)={hi}/{len(scores)}')

    digest_count = sum(1 for i in items if i.get('digest'))
    print(f'  Digest: {digest_count} items | Total API calls: {call_counter[0]}')
    print(f'  Done: {len(items)} ideas enriched')


if __name__ == '__main__':
    main()
