#!/usr/bin/env python3
"""
AI enrichment for the feed — runs after fetch-feed.py completes.
Three tasks using meta/llama-3.3-70b-instruct via NVIDIA NIM:

  Tier 1 — Interest scoring: rates every item 1–10
  Tier 2 — Summarization: writes a clean 1-sentence summary
  Tier 3 — Deduplication: clusters near-identical stories, keeps the best one

Uses TF-IDF cosine similarity for fast dedup pre-filtering before calling AI.
Skips items already enriched (stable id) so incremental runs are cheap.
"""
import json, os, re, time, math
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from collections import defaultdict

FEED_FILE  = '/root/.openclaw/workspace-todo/dashboard/data/feed.json'
CACHE_FILE = '/root/.openclaw/workspace-todo/dashboard/data/ai_cache.json'

NIM_KEY   = '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'
NIM_URL   = 'https://integrate.api.nvidia.com/v1/chat/completions'
FAST_MODEL  = 'meta/llama-3.1-8b-instruct'    # scoring, dedup
SMART_MODEL = 'meta/llama-3.3-70b-instruct'   # summarization

SCORE_BATCH  = 20   # headlines per scoring call
SUMM_BATCH   = 8    # articles per summarization call
DEDUP_SIM    = 0.72 # cosine similarity threshold for potential duplicate
API_TIMEOUT  = 25   # seconds per call

# Categories where summaries matter most (skip memes — titles are the content)
SUMMARIZE_CATS = {'news', 'tech', 'weird'}

# ─── NVIDIA NIM client ─────────────────────────────────────────────────────────

def nim_call(model, system, user, max_tokens=200, temp=0.2, retries=2):
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

# ─── TF-IDF cosine similarity (pure stdlib) ───────────────────────────────────

def _tokenize(text):
    return re.findall(r'\b[a-z]{3,}\b', text.lower())

def _tfidf_vectors(docs):
    """Return list of {word: tf-idf} dicts for each doc."""
    tf_lists = [defaultdict(float) for _ in docs]
    for i, doc in enumerate(docs):
        tokens = _tokenize(doc)
        if not tokens:
            continue
        for t in tokens:
            tf_lists[i][t] += 1.0 / len(tokens)

    df = defaultdict(int)
    N  = len(docs)
    for tf in tf_lists:
        for word in tf:
            df[word] += 1

    vecs = []
    for tf in tf_lists:
        vec = {w: tf[w] * math.log((N + 1) / (df[w] + 1)) for w in tf}
        vecs.append(vec)
    return vecs

def _cosine(v1, v2):
    if not v1 or not v2:
        return 0.0
    common = set(v1) & set(v2)
    if not common:
        return 0.0
    dot  = sum(v1[w] * v2[w] for w in common)
    mag1 = math.sqrt(sum(x * x for x in v1.values()))
    mag2 = math.sqrt(sum(x * x for x in v2.values()))
    return dot / (mag1 * mag2) if mag1 and mag2 else 0.0

def find_duplicate_pairs(items, threshold=DEDUP_SIM):
    """Return list of (i, j) index pairs that are likely duplicates."""
    titles = [i['title'] + ' ' + i.get('desc', '')[:100] for i in items]
    vecs   = _tfidf_vectors(titles)
    pairs  = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            sim = _cosine(vecs[i], vecs[j])
            if sim >= threshold:
                pairs.append((i, j, sim))
    return pairs

# ─── Cache helpers ────────────────────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

# ─── Tier 1: Interest scoring ─────────────────────────────────────────────────

SCORE_SYSTEM = (
    'You score news headlines by how interesting they are to a general reader '
    'who likes tech, science, world events, unusual stories, and gaming. '
    'Scoring guide:\n'
    '10 — shocking, record-breaking, historic, or genuinely bizarre\n'
    ' 8 — very interesting: major development, surprising discovery, or funny\n'
    ' 6 — worth reading: solid news with clear significance\n'
    ' 4 — routine: expected update, minor release, standard market move\n'
    ' 2 — filler: opinion piece with no news, repeat of known info\n'
    ' 1 — spam, clickbait with no substance, or not in English\n'
    'Reply with ONLY comma-separated integers matching the input order. '
    'No text, no explanation.'
)

def score_batch(items_batch):
    """Score a batch of items. Returns list of ints, same length as input."""
    lines = '\n'.join(
        f'{i+1}. {it["title"]}'
        for i, it in enumerate(items_batch)
    )
    raw = nim_call(FAST_MODEL, SCORE_SYSTEM, lines,
                   max_tokens=len(items_batch) * 4, temp=0.1)
    if not raw:
        return [5] * len(items_batch)
    nums = re.findall(r'\d+', raw)
    scores = []
    for n in nums[:len(items_batch)]:
        scores.append(max(1, min(10, int(n))))
    # Pad if model returned fewer numbers
    while len(scores) < len(items_batch):
        scores.append(5)
    return scores

def run_scoring(items, cache):
    """Score all unscored items; returns updated items."""
    to_score = [i for i in items if cache.get(i['id'], {}).get('score') is None]
    if not to_score:
        print(f'  Scoring: all {len(items)} already cached')
        return items

    print(f'  Scoring {len(to_score)} items in batches of {SCORE_BATCH}...')
    t0 = time.time()
    total_calls = 0

    for start in range(0, len(to_score), SCORE_BATCH):
        batch = to_score[start:start + SCORE_BATCH]
        scores = score_batch(batch)
        for item, score in zip(batch, scores):
            if item['id'] not in cache:
                cache[item['id']] = {}
            cache[item['id']]['score'] = score
        total_calls += 1
        time.sleep(0.3)

    # Apply scores to items
    for item in items:
        cached_score = cache.get(item['id'], {}).get('score')
        if cached_score is not None:
            item['ai_score'] = cached_score

    elapsed = time.time() - t0
    print(f'  Scoring done: {total_calls} API calls in {elapsed:.1f}s')
    return items

# ─── Tier 2: Summarization ────────────────────────────────────────────────────

SUMM_SYSTEM = (
    'You write one-sentence news summaries. Rules:\n'
    '- Maximum 25 words\n'
    '- Plain English, no jargon\n'
    '- State the key fact, not meta-commentary\n'
    '- Never start with "The article", "This piece", "According to"\n'
    '- If the input is already short and clear, return it unchanged\n'
    'Reply with a JSON array of strings, one per input item, same order.'
)

def summarize_batch(items_batch):
    """Summarize a batch. Returns list of strings."""
    payload = json.dumps([
        {'id': i + 1,
         'title': it['title'],
         'text': (it.get('desc') or '')[:300]}
        for i, it in enumerate(items_batch)
    ])
    raw = nim_call(SMART_MODEL, SUMM_SYSTEM, payload,
                   max_tokens=len(items_batch) * 60, temp=0.3)
    if not raw:
        return [it['title'] for it in items_batch]

    # Extract JSON array from response
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if not m:
        return [it['title'] for it in items_batch]
    try:
        summaries = json.loads(m.group(0))
        result = []
        for i, it in enumerate(items_batch):
            s = summaries[i] if i < len(summaries) else it['title']
            result.append(str(s).strip())
        return result
    except Exception:
        return [it['title'] for it in items_batch]

def run_summarization(items, cache):
    """Summarize all unsummarized news/tech/weird items."""
    to_summ = [
        i for i in items
        if i.get('category') in SUMMARIZE_CATS
        and cache.get(i['id'], {}).get('summary') is None
        and (i.get('desc') or len(i['title']) > 60)
    ]
    if not to_summ:
        print(f'  Summaries: all already cached')
        return items

    print(f'  Summarizing {len(to_summ)} items in batches of {SUMM_BATCH}...')
    t0 = time.time()
    total_calls = 0

    for start in range(0, len(to_summ), SUMM_BATCH):
        batch = to_summ[start:start + SUMM_BATCH]
        summaries = summarize_batch(batch)
        for item, summary in zip(batch, summaries):
            if item['id'] not in cache:
                cache[item['id']] = {}
            cache[item['id']]['summary'] = summary
        total_calls += 1
        time.sleep(0.5)

    # Apply to items
    for item in items:
        cached_summary = cache.get(item['id'], {}).get('summary')
        if cached_summary:
            item['ai_summary'] = cached_summary

    elapsed = time.time() - t0
    print(f'  Summarization done: {total_calls} API calls in {elapsed:.1f}s')
    return items

# ─── Tier 3: Deduplication ────────────────────────────────────────────────────

DEDUP_SYSTEM = (
    'Decide if two news headlines are about the exact same event. '
    'Reply YES if they describe the same specific occurrence. '
    'Reply NO if they are different events, different angles, or only share a topic. '
    'Reply with only YES or NO.'
)

def ai_confirm_duplicate(a, b):
    """Use AI to confirm if two headlines are the same story."""
    raw = nim_call(
        FAST_MODEL, DEDUP_SYSTEM,
        f'A: {a["title"]}\nB: {b["title"]}',
        max_tokens=5, temp=0.0
    )
    if not raw:
        return False
    return raw.strip().upper().startswith('YES')

def run_deduplication(items, cache):
    """
    Remove duplicate stories.
    Step 1: TF-IDF cosine similarity to find candidates.
    Step 2: AI confirmation on borderline pairs.
    When duplicates found, keep the one with the better source tier
    (RSS > Reddit) or higher ai_score.
    """
    # Source quality tier — higher = prefer to keep
    SOURCE_TIER = {
        'BBC World': 10, 'BBC News': 10, 'NYT World': 10, 'The Guardian': 10,
        'Al Jazeera': 9, 'Reuters': 9, 'BBC Tech': 9,
        'Ars Technica': 8, 'The Verge': 8, 'NYT Tech': 8,
        'TechCrunch': 8, 'WIRED': 8, 'Hacker News': 7,
        'Futurism': 7, 'New Scientist': 7, 'Smithsonian': 7,
    }
    def tier(item):
        return SOURCE_TIER.get(item['source'], 5 if not item['source'].startswith('r/') else 3)

    candidates = find_duplicate_pairs(items)
    if not candidates:
        print('  Dedup: no candidate pairs found')
        return items

    print(f'  Dedup: {len(candidates)} candidate pairs (sim >= {DEDUP_SIM})')
    to_remove = set()

    for i, j, sim in candidates:
        if i in to_remove or j in to_remove:
            continue
        a, b = items[i], items[j]

        # High confidence — remove without AI confirmation
        if sim >= 0.88:
            is_dup = True
        else:
            # Check cache
            pair_key = f'{a["id"]}:{b["id"]}'
            cached = cache.get(f'dedup:{pair_key}')
            if cached is None:
                is_dup = ai_confirm_duplicate(a, b)
                cache[f'dedup:{pair_key}'] = is_dup
                time.sleep(0.2)
            else:
                is_dup = cached

        if is_dup:
            # Keep the higher-quality one
            score_a = a.get('ai_score', 5) + tier(a)
            score_b = b.get('ai_score', 5) + tier(b)
            drop_idx = j if score_a >= score_b else i
            to_remove.add(drop_idx)

    kept = [item for idx, item in enumerate(items) if idx not in to_remove]
    print(f'  Dedup: removed {len(to_remove)} duplicates, {len(kept)} remain')
    return kept

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f'[{datetime.now()}] AI enrichment starting...')

    with open(FEED_FILE) as f:
        feed = json.load(f)
    items = feed['items']
    print(f'  Loaded {len(items)} items')

    cache = load_cache()
    original_cache_size = len(cache)

    # Run all three tiers
    items = run_scoring(items, cache)
    items = run_summarization(items, cache)
    items = run_deduplication(items, cache)

    # Re-sort: primarily by time, but surface high-scoring fresh items
    # Items from last 3h sorted purely by time; older items boosted by score
    now_ts = datetime.now(timezone.utc).timestamp()
    def sort_key(item):
        try:
            age_h = (now_ts - datetime.fromisoformat(
                item['ts'].replace('Z', '+00:00')).timestamp()) / 3600
        except Exception:
            age_h = 24
        score = item.get('ai_score', 5)
        if age_h < 3:
            return (0, -age_h, 0)           # fresh: pure chronological
        return (1, -(score + 10 / (age_h + 1)), 0)  # older: score-boosted

    items.sort(key=sort_key)

    # Update feed file
    feed['items'] = items
    feed['count'] = len(items)
    feed['enrichedAt'] = datetime.now(timezone.utc).isoformat()

    with open(FEED_FILE, 'w') as f:
        json.dump(feed, f, indent=2)

    # Save cache only if it grew
    if len(cache) > original_cache_size:
        save_cache(cache)

    # Print score distribution
    scores = [i.get('ai_score', 0) for i in items if i.get('ai_score')]
    if scores:
        avg = sum(scores) / len(scores)
        hi  = sum(1 for s in scores if s >= 8)
        lo  = sum(1 for s in scores if s <= 3)
        print(f'  Score stats: avg={avg:.1f}, high(>=8)={hi}, low(<=3)={lo}')

    print(f'  Done: {len(items)} items enriched')


if __name__ == '__main__':
    main()
