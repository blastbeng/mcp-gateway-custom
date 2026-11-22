'use strict';

/**
 * Headless Chromium worker for the local websearch MCP server.
 *
 * One browser, one context, one page, reused across queries: a Pi 5 cannot
 * afford a Chromium per request, and serialising the work keeps peak RSS flat.
 * Images, fonts, media and stylesheets are aborted -- they cost CPU/RAM and
 * never contribute to an extracted result list.
 *
 * Only engines that answer a real browser from a normal host IP are wired
 * here. Measured on this stack: Bing honours multi-word queries only when the
 * SERP is requested with `form=QBLH` (without it the same IP gets a generic
 * "first keyword" page, which is what made SearXNG look broken), Marginalia
 * serves results after a ~2 s JS gate, while Google/Brave/DDG/Startpage
 * answer a CAPTCHA, 429 or an anomaly page regardless of the client.
 */

const { chromium } = require('playwright');

const DEBUG = process.env.WEBSEARCH_DEBUG === '1';
const HEADLESS = process.env.BROWSER_HEADLESS !== 'false';

const USER_AGENT =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/131.0.0.0 Safari/537.36';

const LAUNCH_ARGS = [
  '--no-sandbox',
  '--disable-dev-shm-usage',
  '--disable-gpu',
  '--disable-blink-features=AutomationControlled',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--disable-features=TranslateUI',
];

function envInt(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

// Bing encodes freshness as ex1:"ezN" (N = 1 day, 2 week, 3 month).
const BING_FRESHNESS = { day: '1', week: '2', month: '3' };

// How long to keep polling for a result container. Well under LANE_TIMEOUT_MS
// (20 s) so a late page still leaves the rest of the total budget to the other
// lanes, but long enough for the hydration case (~3-5 s on this Pi).
const BING_WAIT_MS = envInt('BING_WAIT_MS', 12000);
const MARGINALIA_WAIT_MS = envInt('MARGINALIA_WAIT_MS', 12000);
const DDG_WAIT_MS = envInt('DDG_WAIT_MS', 12000);

let browserPromise = null;
let contextPromise = null;
let pagePromise = null;
let queue = Promise.resolve();

function log(...args) {
  if (DEBUG) console.error('[websearch/browser]', ...args);
}

async function launchBrowser() {
  // Playwright >=1.49 defaults headless to the `headless_shell` build, which
  // mcp/playwright does not ship (it bakes the full chromium-XXXX tree only).
  // `channel: chromium` points at that baked build instead of phoning home.
  const options = { headless: HEADLESS, args: LAUNCH_ARGS };
  if (process.env.CHROMIUM_EXECUTABLE) options.executablePath = process.env.CHROMIUM_EXECUTABLE;
  else options.channel = 'chromium';

  const browser = await chromium.launch(options);
  browser.on('disconnected', () => {
    log('browser disconnected');
    browserPromise = null;
    contextPromise = null;
    pagePromise = null;
  });
  return browser;
}

async function newContext(browser) {
  const context = await browser.newContext({
    locale: 'en-US',
    userAgent: USER_AGENT,
    viewport: { width: 1366, height: 900 },
    // The result lists we read are public pages; keep the profile ephemeral so
    // one query cannot poison the next.
    ignoreHTTPSErrors: true,
  });

  context.setDefaultTimeout(15000);
  context.setDefaultNavigationTimeout(20000);

  // Images, media and fonts never contribute to an extracted list. Stylesheets
  // are deliberately NOT aborted: Bing and Marginalia both ship pages whose
  // JS fills the result container after hydration, and blocking CSS made that
  // hydration fail intermittently (measured: the same query returned 10
  // `li.b_algo` with CSS and 0 without).
  await context.route('**/*', (route) => {
    const type = route.request().resourceType();
    if (type === 'image' || type === 'media' || type === 'font') {
      return route.abort().catch(() => {});
    }
    return route.continue().catch(() => {});
  });

  return context;
}

async function ensurePage() {
  if (pagePromise) {
    const page = await pagePromise.catch(() => null);
    if (page && !page.isClosed()) return page;
    pagePromise = null;
  }

  if (!browserPromise) browserPromise = launchBrowser();
  const browser = await browserPromise.catch((err) => {
    browserPromise = null;
    throw err;
  });

  if (!contextPromise) contextPromise = newContext(browser);
  const context = await contextPromise.catch((err) => {
    contextPromise = null;
    throw err;
  });

  pagePromise = context.newPage().then(async (page) => {
    page.on('crash', () => {
      log('page crashed');
      pagePromise = null;
    });
    return page;
  });

  return pagePromise;
}

/** Drop the current page (and browser, if it died) after a hard failure. */
async function resetPage() {
  pagePromise = null;
  try {
    if (contextPromise) {
      const context = await contextPromise.catch(() => null);
      if (context) await context.close().catch(() => {});
    }
  } finally {
    contextPromise = null;
  }
}

/**
 * Serialise every page interaction. Chromium is the one expensive resource on
 * this host: one query at a time keeps RSS predictable and avoids two lanes
 * fighting over the same page.
 */
function withPage(fn) {
  const run = queue.then(() => ensurePage().then(fn));
  queue = run.then(
    () => {},
    () => {},
  );
  return run;
}

async function closeBrowser() {
  const browser = await browserPromise?.catch(() => null);
  browserPromise = contextPromise = pagePromise = null;
  if (browser) await browser.close().catch(() => {});
}

/** Dismiss the EU cookie banner if Bing decides to show one. */
async function dismissConsent(page) {
  for (const selector of ['#bnp_btn_accept', 'button#bnp_btn_accept', '#bnp_btn_reject']) {
    const locator = page.locator(selector).first();
    if (await locator.count().catch(() => 0)) {
      await locator.click({ timeout: 1000 }).catch(() => {});
      return;
    }
  }
}

/* ------------------------------------------------------------------ */
/* DuckDuckGo (html endpoint)                                         */
/* ------------------------------------------------------------------ */

// DDG's date filter for html.duckduckgo.com: df=d|w|m|y.
const DDG_FRESHNESS = { day: 'd', week: 'w', month: 'm', year: 'y' };

const DDG_EXTRACT = () => {
  // Result links are either absolute or wrapped as
  // `//duckduckgo.com/l/?uddg=<encoded>&rut=...`.
  const decode = (href) => {
    if (!href) return null;
    let value = href;
    if (!/^https?:\/\//.test(value)) value = 'https:' + value.replace(/^https?:/, '');
    try {
      const url = new URL(value);
      const target = url.searchParams.get('uddg');
      if (target) return decodeURIComponent(target);
      if (/duckduckgo\.com\/l\//.test(url.pathname)) return null;
      return /^https?:\/\//.test(value) ? value : null;
    } catch (err) {
      return null;
    }
  };

  const out = [];
  for (const block of document.querySelectorAll('.result')) {
    const cls = block.className || '';
    if (/result--ad/.test(cls)) continue;
    const anchor = block.querySelector('a.result__a[href]');
    if (!anchor) continue;
    const title = anchor.textContent.replace(/\s+/g, ' ').trim();
    const url = decode(anchor.getAttribute('href'));
    if (!title || !url) continue;
    const snippetNode = block.querySelector('.result__snippet');
    out.push({
      title,
      url,
      snippet: snippetNode ? snippetNode.textContent.replace(/\s+/g, ' ').trim() : '',
    });
  }

  const text = document.textContent || '';
  return {
    items: out,
    // DDG answers an over-used IP with its "anomaly" page instead of results.
    blocked: out.length === 0 && /anomaly|unusual traffic|not a robot|bots use ddg/i.test(text),
    noResults: out.length === 0 && /no results|non ci sono risultati/i.test(text),
    title: (document.title || '').slice(0, 120),
    body: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 160),
  };
};

async function duckduckgoSearch({ query, count, freshness, timeoutMs }) {
  return withPage(async (page) => {
    // html.duckduckgo.com serves plain server-rendered HTML (`lite.` is the
    // sparser sibling). Plain HTTP clients get HTTP 202 + an anomaly page from
    // this IP; a real browser gets the list. Measured: 10 on-topic results for
    // a three-word technical query, with title, real URL and snippet.
    const url = new URL('https://html.duckduckgo.com/html/');
    url.searchParams.set('q', query);
    url.searchParams.set('kl', 'us-en');
    if (freshness && DDG_FRESHNESS[freshness]) url.searchParams.set('df', DDG_FRESHNESS[freshness]);
    log('duckduckgo goto', url.toString());

    const started = Date.now();
    await page.goto(url.toString(), { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    const gotoMs = Date.now() - started;

    const deadline = Date.now() + Math.min(timeoutMs, DDG_WAIT_MS);
    let extracted = { items: [], blocked: false, noResults: false, title: '', body: '' };
    while (Date.now() < deadline) {
      extracted = await page.evaluate(DDG_EXTRACT).catch(() => extracted);
      if (extracted.items.length || extracted.blocked || extracted.noResults) break;
      await page.waitForTimeout(400);
    }

    const items = extracted.items.slice(0, count);
    const diagnostic = items.length
      ? null
      : `title="${extracted.title}" body="${extracted.body}" blocked=${extracted.blocked}`;
    log('duckduckgo timings', {
      gotoMs,
      ms: Date.now() - started,
      items: items.length,
      blocked: extracted.blocked,
    });

    return {
      items,
      noResults: extracted.noResults,
      blocked: extracted.blocked,
      diagnostic,
    };
  });
}

/* ------------------------------------------------------------------ */
/* Bing                                                               */
/* ------------------------------------------------------------------ */

const BING_EXTRACT = () => {
  const decodeBing = (href) => {
    try {
      const url = new URL(href, 'https://www.bing.com');
      const encoded = url.searchParams.get('u');
      if (encoded) {
        let b64 = encoded.startsWith('a1') ? encoded.slice(2) : encoded;
        b64 = b64.replace(/-/g, '+').replace(/_/g, '/');
        while (b64.length % 4) b64 += '=';
        const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
        const decoded = new TextDecoder().decode(bytes);
        if (/^https?:\/\//.test(decoded)) return decoded;
      }
    } catch (err) {
      /* fall through to the cite-based URL */
    }
    return null;
  };

  const citeToUrl = (li) => {
    const cite = li.querySelector('cite');
    const text = cite ? cite.textContent.trim() : '';
    if (!text) return null;
    const parts = text.split(/\s*[›>]\s*/).filter(Boolean);
    if (!parts.length) return null;
    const host = parts.shift();
    if (!/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(host)) return null;
    return 'https://' + host + (parts.length ? '/' + parts.join('/') : '');
  };

  const nodes = Array.from(
    document.querySelectorAll('#b_results > li.b_algo, #b_results li.b_algo'),
  );

  const out = [];
  for (const li of nodes) {
    const heading = li.querySelector('h2');
    const anchor = heading ? heading.querySelector('a[href]') : null;
    const title = heading ? heading.textContent.replace(/\s+/g, ' ').trim() : '';
    const url =
      (anchor && decodeBing(anchor.getAttribute('href') || '')) || citeToUrl(li) || '';
    const snippetNode = li.querySelector('.b_caption p, .b_snippet, p');
    const snippet = snippetNode ? snippetNode.textContent.replace(/\s+/g, ' ').trim() : '';
    if (!title || !/^https?:\/\//.test(url)) continue;
    if (/^https?:\/\/(www\.)?bing\.com/.test(url)) continue;
    out.push({ title, url, snippet });
  }

  // Bing answers a throttled IP with an explicit empty SERP rather than an
  // error, so the lane reports that separately and the breaker backs off.
  const body = document.body?.innerText || '';
  const noResults = out.length === 0 && /there are no results|non ci sono risultati/i.test(body);

  return {
    items: out,
    noResults,
    // Kept for the failure message: "0 results" is not actionable, the page
    // title and opening text say whether it was a throttle, a consent wall or
    // a rewrite of the markup we extract from.
    title: (document.title || '').slice(0, 120),
    body: body.replace(/\s+/g, ' ').slice(0, 160),
  };
};

async function bingSearch({ query, count, freshness, timeoutMs }) {
  return withPage(async (page) => {
    const url = new URL('https://www.bing.com/search');
    url.searchParams.set('q', query);
    // form=QBLH is not cosmetic: without it Bing degrades multi-word queries
    // to a generic page keyed on the first term (see file header).
    url.searchParams.set('form', 'QBLH');
    url.searchParams.set('setlang', 'en');
    url.searchParams.set('cc', 'us');
    url.searchParams.set('count', String(Math.min(Math.max(count * 2, 10), 30)));
    if (freshness && BING_FRESHNESS[freshness]) {
      url.searchParams.set('filters', `ex1:"ez${BING_FRESHNESS[freshness]}"`);
    }
    log('bing goto', url.toString());

    const started = Date.now();
    await page.goto(url.toString(), { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    const gotoMs = Date.now() - started;
    await dismissConsent(page);

    // The same URL returns three different pages: the normal SERP (li.b_algo
    // present within a second), a skeleton whose result container is filled
    // after hydration, and -- on a throttled IP -- an explicit "There are no
    // results" page. A single fixed waitForSelector covered only the first and
    // reported the other two as "no results", so poll instead, and stop early
    // when Bing says there is nothing.
    const deadline = Date.now() + Math.min(timeoutMs, BING_WAIT_MS);
    let extracted = { items: [], noResults: false, title: '', body: '' };
    while (Date.now() < deadline) {
      extracted = await page.evaluate(BING_EXTRACT).catch(() => extracted);
      if (extracted.items.length || extracted.noResults) break;
      await page.waitForTimeout(500);
    }
    const selectorMs = Date.now() - started;

    const items = extracted.items.slice(0, count);
    const diagnostic = items.length
      ? null
      : `title="${extracted.title}" body="${extracted.body}" noResults=${extracted.noResults}`;
    log('bing timings', { gotoMs, selectorMs, items: items.length, noResults: extracted.noResults });

    return { items, noResults: extracted.noResults, diagnostic };
  });
}

/* ------------------------------------------------------------------ */
/* Marginalia                                                         */
/* ------------------------------------------------------------------ */

const MARGINALIA_EXTRACT = () => {
  const out = [];
  const seen = new Set();
  const skip =
    /marginalia-search\.com|old-search\.marginalia\.nu|git\.marginalia\.nu|search\.marginalia\.nu/;

  const push = (anchor, card) => {
    const url = anchor.href;
    if (!url || skip.test(url) || seen.has(url)) return;
    seen.add(url);

    const title = anchor.textContent.replace(/\s+/g, ' ').trim();
    if (!title) return;

    let snippet = card ? card.textContent.replace(/\s+/g, ' ').trim() : '';
    if (snippet) {
      snippet = snippet.replace(title, ' ');
      const domain = url.replace(/^https?:\/\//, '').split('/')[0];
      if (domain) snippet = snippet.split(domain).join(' ');
      // Cards start with the fetch date: "2024-10-15 · <url> <snippet>".
      snippet = snippet.replace(/^\s*\d{4}-\d{2}-\d{2}\s*[·-]?\s*/, '');
    }
    out.push({ title, url, snippet: snippet.replace(/\s+/g, ' ').trim().slice(0, 400) });
  };

  // Result cards are `div.bg-white` and every one carries the fetch date next
  // to the URL ("2025-11-16 · https://..."). That date is what separates a real
  // result from the filter panel and the "submit your site" promo, which are
  // `div.bg-white` too and used to arrive as the only hit ("git repository")
  // when the page was read before hydration.
  const cards = document.querySelectorAll('div.bg-white');
  for (const card of cards) {
    const text = card.textContent || '';
    if (!/\d{4}-\d{2}-\d{2}/.test(text)) continue;
    const anchor = card.querySelector('h2 a[href^="http"]');
    if (!anchor) continue;
    push(anchor, card);
  }

  return out;
};

async function marginaliaSearch({ query, count, timeoutMs }) {
  return withPage(async (page) => {
    const url = new URL('https://marginalia-search.com/search');
    url.searchParams.set('query', query);
    log('marginalia goto', url.toString());

    const started = Date.now();
    await page.goto(url.toString(), { waitUntil: 'domcontentloaded', timeout: timeoutMs });

    const deadline = Date.now() + Math.min(timeoutMs, MARGINALIA_WAIT_MS);
    let items = [];
    // The page hydrates: reading it immediately returns the promo/filter cards
    // only. One short pause, then poll, is what makes the first read a real one.
    await page.waitForTimeout(800);

    while (Date.now() < deadline) {
      items = await page.evaluate(MARGINALIA_EXTRACT).catch(() => []);
      if (items.length) break;
      // Bot gate: "Wait A Moment ... please wait 2 seconds", then a reload.
      const gate = await page
        .evaluate(() => /wait a moment|bot activity/i.test(document.body?.innerText || ''))
        .catch(() => false);
      if (gate) {
        await page.waitForTimeout(2200);
        await page.reload({ waitUntil: 'domcontentloaded', timeout: timeoutMs }).catch(() => {});
      } else {
        await page.waitForTimeout(700);
      }
    }

    log('marginalia timings', { ms: Date.now() - started, items: items.length });
    return { items: items.slice(0, count), noResults: false };
  });
}

module.exports = {
  duckduckgoSearch,
  bingSearch,
  marginaliaSearch,
  closeBrowser,
  resetPage,
  withPage,
};
