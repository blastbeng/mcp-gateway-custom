'use strict';

/**
 * Search orchestration: lane order, TTL cache, per-lane circuit breaker.
 *
 * Lanes are tried in order and results are merged until `count` is reached.
 * A lane that fails or times out is recorded; after BREAKER_THRESHOLD strikes
 * it is skipped for BREAKER_COOLDOWN_S, so one blocked upstream can no longer
 * make every query wait for its timeout. This is the difference between
 * "sometimes slow" and "hangs" in an agent loop.
 *
 *   duckduckgo    html.duckduckgo.com through Chromium (best coverage, ~2-4 s)
 *   bing          real Chromium SERP with form=QBLH (throttles this IP)
 *   marginalia    real Chromium SERP, own independent index (small web)
 *   searxng       local SearXNG, keyless API engines only (see searxng/settings.yml)
 *   wikipedia     MediaWiki API, keyless
 *   stackexchange Stack Exchange API, keyless
 *   hn            Algolia HN API, keyless
 *
 * The last three need no browser and no key: they are the always-up tail.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { duckduckgoSearch, bingSearch, marginaliaSearch } = require('./browser.cjs');

const DEBUG = () => process.env.WEBSEARCH_DEBUG === '1';

function envInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

const SEARXNG_URL = (process.env.SEARXNG_URL || 'http://searxng:8080').replace(/\/+$/, '');
const SEARXNG_CATEGORIES = process.env.SEARXNG_CATEGORIES || 'general,it,science';
const LANE_TIMEOUT_MS = envInt('LANE_TIMEOUT_MS', 20000);
const TOTAL_TIMEOUT_MS = envInt('TOTAL_TIMEOUT_MS', 45000);
const CACHE_TTL_S = envInt('CACHE_TTL_S', 86400);
const CACHE_MAX = envInt('CACHE_MAX', 800);
const BREAKER_THRESHOLD = envInt('BREAKER_THRESHOLD', 3);
const BREAKER_COOLDOWN_S = envInt('BREAKER_COOLDOWN_S', 600);
const DEFAULT_COUNT = envInt('DEFAULT_COUNT', 8);
const MAX_COUNT = envInt('MAX_COUNT', 20);

const CACHE_DIR = process.env.CACHE_DIR || '/tmp/websearch/cache';
const STATE_DIR = process.env.STATE_DIR || '/tmp/websearch/state';
const STATE_FILE = path.join(STATE_DIR, 'breakers.json');

const ALL_LANES = [
  'duckduckgo',
  'bing',
  'marginalia',
  'searxng',
  'wikipedia',
  'stackexchange',
  'hn',
];

const LANES = (process.env.WEBSEARCH_LANES || ALL_LANES.join(','))
  .split(',')
  .map((name) => name.trim())
  .filter((name) => ALL_LANES.includes(name));

const FRESHNESS = new Set(['day', 'week', 'month', 'year']);

function log(...args) {
  if (DEBUG()) console.error('[websearch]', ...args);
}

/* ------------------------------------------------------------------ */
/* Cache                                                              */
/* ------------------------------------------------------------------ */

function ensureDir(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    log('mkdir failed', dir, err.message);
  }
}

function cacheKey({ query, count, freshness, site }) {
  const material = JSON.stringify({ q: query.toLowerCase(), count, freshness: freshness || '', site: site || '' });
  return crypto.createHash('sha256').update(material).digest('hex').slice(0, 32);
}

function cacheGet(key) {
  const file = path.join(CACHE_DIR, `${key}.json`);
  try {
    const stat = fs.statSync(file);
    if (Date.now() - stat.mtimeMs > CACHE_TTL_S * 1000) {
      fs.unlink(file, () => {});
      return null;
    }
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (err) {
    return null;
  }
}

function cacheSet(key, payload) {
  ensureDir(CACHE_DIR);
  const file = path.join(CACHE_DIR, `${key}.json`);
  const tmp = `${file}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(tmp, JSON.stringify({ ts: Date.now(), payload }));
    fs.renameSync(tmp, file);
  } catch (err) {
    log('cache write failed', err.message);
    return;
  }
  sweepCache();
}

let sweepCounter = 0;
function sweepCache() {
  if (++sweepCounter % 50 !== 0) return;
  try {
    const files = fs
      .readdirSync(CACHE_DIR)
      .filter((name) => name.endsWith('.json'))
      .map((name) => {
        const full = path.join(CACHE_DIR, name);
        return { full, mtime: fs.statSync(full).mtimeMs };
      })
      .sort((a, b) => a.mtime - b.mtime);
    for (const entry of files.slice(0, Math.max(0, files.length - CACHE_MAX))) {
      fs.unlink(entry.full, () => {});
    }
  } catch (err) {
    log('cache sweep failed', err.message);
  }
}

function cacheStats() {
  try {
    const entries = fs.readdirSync(CACHE_DIR).filter((name) => name.endsWith('.json')).length;
    return { dir: CACHE_DIR, entries, ttlSeconds: CACHE_TTL_S, max: CACHE_MAX };
  } catch (err) {
    return { dir: CACHE_DIR, entries: 0, ttlSeconds: CACHE_TTL_S, max: CACHE_MAX };
  }
}

/* ------------------------------------------------------------------ */
/* Circuit breaker                                                    */
/* ------------------------------------------------------------------ */

function loadBreakers() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch (err) {
    return {};
  }
}

function saveBreakers(state) {
  ensureDir(STATE_DIR);
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state));
  } catch (err) {
    log('breaker persist failed', err.message);
  }
}

let breakers = loadBreakers();
const lastRuns = {};

function breakerOpen(name) {
  const entry = breakers[name];
  if (!entry || !entry.openedAt) return false;
  return Date.now() - entry.openedAt < BREAKER_COOLDOWN_S * 1000;
}

function breakerFail(name, reason) {
  const entry = breakers[name] || { strikes: 0, openedAt: 0, reason: '' };
  entry.strikes += 1;
  entry.reason = String(reason || '').slice(0, 200);
  if (entry.strikes >= BREAKER_THRESHOLD) {
    entry.openedAt = Date.now();
    entry.strikes = 0;
  }
  breakers[name] = entry;
  saveBreakers(breakers);
}

function breakerOk(name) {
  if (breakers[name]) {
    delete breakers[name];
    saveBreakers(breakers);
  }
}

function breakerState() {
  const out = {};
  for (const [name, entry] of Object.entries(breakers)) {
    out[name] = {
      open: breakerOpen(name),
      strikes: entry.strikes,
      openedAt: entry.openedAt ? new Date(entry.openedAt).toISOString() : null,
      reason: entry.reason || null,
    };
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', '#39': "'" };

function decodeEntities(text) {
  return String(text || '').replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, (match, code) => {
    const key = code.toLowerCase();
    if (ENTITIES[key]) return ENTITIES[key];
    if (key.startsWith('#x')) return String.fromCodePoint(Number.parseInt(key.slice(2), 16));
    if (key.startsWith('#')) return String.fromCodePoint(Number.parseInt(key.slice(1), 10));
    return match;
  });
}

function stripTags(text) {
  return decodeEntities(String(text || '').replace(/<[^>]*>/g, ' '))
    .replace(/\s+/g, ' ')
    .trim();
}

function clip(text, max) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function normalizeUrl(raw) {
  try {
    const url = new URL(raw);
    url.hash = '';
    for (const param of [...url.searchParams.keys()]) {
      if (/^(utm_|fbclid$|gclid$|msclkid$|ref$)/i.test(param)) url.searchParams.delete(param);
    }
    let host = url.host.toLowerCase();
    if (host.startsWith('www.')) host = host.slice(4);
    const pathname = url.pathname.replace(/\/+$/, '');
    return `${host}${pathname}${url.search}`.toLowerCase();
  } catch (err) {
    return String(raw || '').toLowerCase();
  }
}

function sanitizeQuery(raw) {
  const query = String(raw || '')
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!query) throw new Error('query is required');
  if (query.length > 400) return query.slice(0, 400);
  return query;
}

function sanitizeSite(raw) {
  if (raw === undefined || raw === null || raw === '') return '';
  const site = String(raw).trim().toLowerCase();
  if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)+$/.test(site)) {
    throw new Error('site must be a bare domain, e.g. docs.docker.com');
  }
  return site;
}

function sanitizeCount(raw) {
  if (raw === undefined || raw === null || raw === '') return DEFAULT_COUNT;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value)) return DEFAULT_COUNT;
  return Math.min(Math.max(value, 1), MAX_COUNT);
}

function sanitizeFreshness(raw) {
  if (!raw) return '';
  const value = String(raw).toLowerCase();
  if (!FRESHNESS.has(value)) throw new Error('freshness must be one of day, week, month, year');
  return value;
}

const HTML_ACCEPT = 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8';

async function fetchJson(url, timeoutMs) {
  const response = await fetch(url, {
    signal: AbortSignal.timeout(timeoutMs),
    headers: {
      'user-agent':
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
      accept: HTML_ACCEPT,
      'accept-language': 'en-US,en;q=0.9',
    },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------------ */
/* Lanes                                                              */
/* ------------------------------------------------------------------ */

// DuckDuckGo's HTML endpoint is the strongest keyless SERP from this host: it
// answers a real browser with ten on-topic results, while plain HTTP clients
// get 202 + an anomaly page. It is still a public endpoint, so keep it paced.
const ddgBudget = { hourStart: Date.now(), used: 0, lastAt: 0 };
const DDG_MIN_INTERVAL_MS = envInt('DDG_MIN_INTERVAL_S', 3) * 1000;
const DDG_MAX_PER_HOUR = envInt('DDG_MAX_PER_HOUR', 150);

async function laneDuckduckgo({ query, count, freshness, timeoutMs }) {
  const now = Date.now();
  if (now - ddgBudget.hourStart > 3600 * 1000) {
    ddgBudget.hourStart = now;
    ddgBudget.used = 0;
  }
  if (ddgBudget.used >= DDG_MAX_PER_HOUR) {
    throw throttled(`duckduckgo hourly budget (${DDG_MAX_PER_HOUR}) exhausted`);
  }
  if (ddgBudget.lastAt && now - ddgBudget.lastAt < DDG_MIN_INTERVAL_MS) {
    const wait = Math.ceil((DDG_MIN_INTERVAL_MS - (now - ddgBudget.lastAt)) / 1000);
    throw throttled(`duckduckgo pacing: ${wait}s to go`);
  }

  ddgBudget.used += 1;
  ddgBudget.lastAt = now;

  const { items, noResults, blocked, diagnostic } = await duckduckgoSearch({
    query,
    count,
    freshness,
    timeoutMs,
  });
  if (!items.length) {
    throw throttled(
      blocked
        ? 'duckduckgo served its anomaly page (IP flagged)'
        : `duckduckgo returned no results: ${diagnostic || 'no diagnostic'}`,
      true,
    );
  }
  return items.map((item) => ({ ...item, engine: 'duckduckgo' }));
}

// Bing answers a real browser well for the first queries, then starts serving
// an empty SERP from this IP ("There are no results for <query>" while the
// query plainly has results). Budgeting the lane keeps it useful instead of
// spending 8 s per call to collect nothing: at most BING_MAX_PER_HOUR queries,
// at least BING_MIN_INTERVAL_S apart, and three empty SERPs in a row trip the
// breaker for BREAKER_COOLDOWN_S.
const bingBudget = { hourStart: Date.now(), used: 0, lastAt: 0 };
const BING_MIN_INTERVAL_MS = envInt('BING_MIN_INTERVAL_S', 8) * 1000;
const BING_MAX_PER_HOUR = envInt('BING_MAX_PER_HOUR', 40);

function throttled(message, strike = false) {
  const error = new Error(message);
  error.kind = 'throttled';
  error.strike = strike;
  return error;
}

async function laneBing({ query, count, freshness, timeoutMs }) {
  const now = Date.now();
  if (now - bingBudget.hourStart > 3600 * 1000) {
    bingBudget.hourStart = now;
    bingBudget.used = 0;
  }
  if (bingBudget.used >= BING_MAX_PER_HOUR) {
    throw throttled(`bing hourly budget (${BING_MAX_PER_HOUR}) exhausted`);
  }
  if (bingBudget.lastAt && now - bingBudget.lastAt < BING_MIN_INTERVAL_MS) {
    const wait = Math.ceil((BING_MIN_INTERVAL_MS - (now - bingBudget.lastAt)) / 1000);
    throw throttled(`bing pacing: ${wait}s to go`);
  }

  bingBudget.used += 1;
  bingBudget.lastAt = now;

  const { items, noResults, diagnostic } = await bingSearch({ query, count, freshness, timeoutMs });
  if (!items.length) {
    throw throttled(
      noResults
        ? 'bing returned an empty SERP (query/IP throttled)'
        : `bing returned no parsable results: ${diagnostic || 'no diagnostic'}`,
      true,
    );
  }
  return items.map((item) => ({ ...item, engine: 'bing' }));
}

async function laneMarginalia({ query, count, timeoutMs }) {
  const { items } = await marginaliaSearch({ query, count, timeoutMs });
  return items.map((item) => ({ ...item, engine: 'marginalia' }));
}

async function laneSearxng({ query, count, freshness, timeoutMs }) {
  const url = new URL('/search', SEARXNG_URL);
  url.searchParams.set('q', query);
  url.searchParams.set('format', 'json');
  url.searchParams.set('language', 'en');
  url.searchParams.set('safesearch', '1');
  // SearXNG's default category is `general` only, which on the keyless engine
  // set (see searxng/settings.yml) means mwmbl + wiby and nothing else:
  // github/mdn/stackoverflow live in `it`, arxiv/openalex/crossref in
  // `science`. Asking for all three is what makes the fallback return a mixed,
  // useful page (measured: 85 -> 191 results, zero unresponsive engines).
  url.searchParams.set('categories', SEARXNG_CATEGORIES);
  if (freshness) url.searchParams.set('time_range', freshness);

  const data = await fetchJson(url, timeoutMs);
  const results = Array.isArray(data?.results) ? data.results : [];
  return results.slice(0, count).map((item) => ({
    title: stripTags(item.title),
    url: item.url,
    snippet: clip(stripTags(item.content || item.publishedDate || ''), 400),
    engine: `searxng:${item.engine || 'unknown'}`,
  }));
}

async function laneWikipedia({ query, count, timeoutMs }) {
  const url = new URL('https://en.wikipedia.org/w/api.php');
  url.searchParams.set('action', 'query');
  url.searchParams.set('list', 'search');
  url.searchParams.set('srsearch', query);
  url.searchParams.set('srlimit', String(count));
  url.searchParams.set('format', 'json');
  url.searchParams.set('utf8', '1');

  const data = await fetchJson(url, timeoutMs);
  const hits = data?.query?.search || [];
  return hits.map((hit) => ({
    title: hit.title,
    url: `https://en.wikipedia.org/wiki/${encodeURIComponent(hit.title.replace(/ /g, '_'))}`,
    snippet: clip(stripTags(hit.snippet), 400),
    engine: 'wikipedia',
  }));
}

async function laneStackexchange({ query, count, timeoutMs }) {
  const url = new URL('https://api.stackexchange.com/2.3/search/advanced');
  url.searchParams.set('order', 'desc');
  url.searchParams.set('sort', 'relevance');
  url.searchParams.set('q', query);
  url.searchParams.set('site', 'stackoverflow');
  url.searchParams.set('pagesize', String(count));
  url.searchParams.set('filter', 'default');

  const data = await fetchJson(url, timeoutMs);
  const items = Array.isArray(data?.items) ? data.items : [];
  return items.map((item) => ({
    title: decodeEntities(item.title),
    url: item.link,
    snippet: clip(
      `score ${item.score} · ${item.is_answered ? 'answered' : 'unanswered'}${
        item.tags?.length ? ` · ${item.tags.slice(0, 5).join(', ')}` : ''
      }`,
      200,
    ),
    engine: 'stackoverflow',
  }));
}

async function laneHn({ query, count, timeoutMs }) {
  const url = new URL('https://hn.algolia.com/api/v1/search');
  url.searchParams.set('query', query);
  url.searchParams.set('hitsPerPage', String(count));
  url.searchParams.set('tags', 'story');

  const data = await fetchJson(url, timeoutMs);
  const hits = Array.isArray(data?.hits) ? data.hits : [];
  return hits.map((hit) => ({
    title: hit.title || `HN item ${hit.objectID}`,
    url: hit.url || `https://news.ycombinator.com/item?id=${hit.objectID}`,
    snippet: clip(
      stripTags(hit.story_text || '') ||
        `HN · ${hit.points || 0} points · ${hit.num_comments || 0} comments`,
      300,
    ),
    engine: 'hn',
  }));
}

const LANE_FUNCS = {
  duckduckgo: laneDuckduckgo,
  bing: laneBing,
  marginalia: laneMarginalia,
  searxng: laneSearxng,
  wikipedia: laneWikipedia,
  stackexchange: laneStackexchange,
  hn: laneHn,
};

/* ------------------------------------------------------------------ */
/* Orchestration                                                      */
/* ------------------------------------------------------------------ */

function errorKind(err) {
  const message = String(err && err.message ? err.message : err);
  if (/timeout|timed out/i.test(message)) return 'timeout';
  return 'error';
}

async function webSearch(input = {}) {
  const started = Date.now();
  const query = sanitizeQuery(input.query);
  const count = sanitizeCount(input.count);
  const freshness = sanitizeFreshness(input.freshness);
  const site = sanitizeSite(input.site);
  const effectiveQuery = site ? `${query} site:${site}` : query;

  const key = cacheKey({ query: effectiveQuery, count, freshness, site });
  const cached = cacheGet(key);
  if (cached) {
    log('cache hit', key);
    return { ...cached.payload, cached: true, tookMs: Date.now() - started };
  }

  const results = [];
  const seen = new Set();
  const laneReports = [];

  for (const name of LANES) {
    if (results.length >= count) break;

    const remaining = TOTAL_TIMEOUT_MS - (Date.now() - started);
    if (remaining < 2000) {
      laneReports.push({ name, status: 'skipped', reason: 'total timeout budget exhausted' });
      continue;
    }

    if (breakerOpen(name)) {
      laneReports.push({ name, status: 'open', reason: 'circuit breaker open' });
      continue;
    }

    const laneStarted = Date.now();
    const timeoutMs = Math.min(LANE_TIMEOUT_MS, remaining);

    try {
      const items = await LANE_FUNCS[name]({ query: effectiveQuery, count, freshness, timeoutMs });
      const accepted = [];
      for (const item of items) {
        if (!item?.url || !item?.title) continue;
        const norm = normalizeUrl(item.url);
        if (!norm || seen.has(norm)) continue;
        seen.add(norm);
        accepted.push({
          title: clip(item.title, 200),
          url: item.url,
          snippet: clip(item.snippet, 400),
          engine: item.engine || name,
        });
        if (results.length + accepted.length >= count) break;
      }

      const ms = Date.now() - laneStarted;
      if (accepted.length) {
        breakerOk(name);
        results.push(...accepted);
      }
      laneReports.push({ name, status: accepted.length ? 'ok' : 'empty', ms, results: accepted.length });
      lastRuns[name] = { at: new Date().toISOString(), ms, results: accepted.length };
    } catch (err) {
      const ms = Date.now() - laneStarted;
      const status = err && err.kind === 'throttled' ? 'throttled' : errorKind(err);
      // Pacing/budget refusals are not failures; only real errors and empty
      // SERPs (err.strike) count towards the breaker.
      if (status !== 'throttled' || (err && err.strike)) breakerFail(name, err && err.message);
      laneReports.push({ name, status, ms, error: clip(err && err.message, 200) });
      lastRuns[name] = { at: new Date().toISOString(), ms, results: 0, status };
      log('lane failed', name, status, err && err.message);
    }
  }

  const payload = {
    query: effectiveQuery,
    count,
    freshness: freshness || null,
    site: site || null,
    results,
    lanes: laneReports,
  };

  if (results.length) cacheSet(key, payload);
  else payload.hint = 'No lane returned results. Try the searxng_web_search tool, or web_url_read on a known URL.';

  return { ...payload, cached: false, tookMs: Date.now() - started };
}

function status() {
  return {
    server: 'websearch',
    searxngUrl: SEARXNG_URL,
    lanes: LANES,
    laneTimeoutMs: LANE_TIMEOUT_MS,
    totalTimeoutMs: TOTAL_TIMEOUT_MS,
    breakers: breakerState(),
    lastRuns,
    cache: cacheStats(),
  };
}

module.exports = { webSearch, status, ALL_LANES };
