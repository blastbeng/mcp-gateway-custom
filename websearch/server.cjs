#!/usr/bin/env node
'use strict';

/**
 * Minimal MCP stdio server (JSON-RPC 2.0, newline-delimited).
 *
 * Hand-rolled on purpose: the only dependency this image needs is the
 * `playwright` package already baked into mcp/playwright, so the server has no
 * build step, no npm install and no SDK version drift. stdout carries protocol
 * frames only; every log goes to stderr.
 */

const readline = require('node:readline');

const { webSearch, status } = require('./search.cjs');
const { closeBrowser } = require('./browser.cjs');

const SERVER_INFO = { name: 'websearch', version: '1.0.0' };
const DEFAULT_PROTOCOL = '2024-11-05';

const TOOLS = [
  {
    name: 'web_search',
    description:
      'Web search from this host, no API keys. Runs headless Chromium against DuckDuckGo ' +
      '(html endpoint), then Bing, Marginalia (independent index), the local keyless SearXNG ' +
      'instance, and finally Wikipedia / Stack Overflow / Hacker News APIs. Results are cached for ' +
      '24 h and a failing engine is skipped by a circuit breaker, so the call degrades ' +
      'instead of hanging. Returns JSON: {query, results:[{title,url,snippet,engine}], ' +
      'lanes:[{name,status,ms,results}], cached, tookMs}.',
    inputSchema: {
      type: 'object',
      properties: {
        query: {
          type: 'string',
          description: 'Search terms. Natural language or keywords; do not include site: here (use "site").',
        },
        count: {
          type: 'integer',
          description: 'Maximum number of results to return (1-20, default 8).',
          minimum: 1,
          maximum: 20,
        },
        freshness: {
          type: 'string',
          description: 'Restrict to recent pages.',
          enum: ['day', 'week', 'month', 'year'],
        },
        site: {
          type: 'string',
          description: 'Restrict to one bare domain, e.g. "docs.docker.com".',
        },
      },
      required: ['query'],
      additionalProperties: false,
    },
  },
  {
    name: 'web_search_status',
    description:
      'Diagnostics for the local search stack: configured lanes, per-lane circuit-breaker ' +
      'state, last run per lane, cache size and the SearXNG URL. Use it when web_search ' +
      'returns few or no results.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
];

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function reply(id, result) {
  send({ jsonrpc: '2.0', id, result });
}

function replyError(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

async function callTool(name, args) {
  if (name === 'web_search') {
    return { content: [{ type: 'text', text: JSON.stringify(await webSearch(args), null, 2) }] };
  }
  if (name === 'web_search_status') {
    return { content: [{ type: 'text', text: JSON.stringify(status(), null, 2) }] };
  }
  const error = new Error(`Unknown tool: ${name}`);
  error.code = -32602;
  throw error;
}

async function handle(message) {
  const { id, method, params } = message;
  const isRequest = id !== undefined && id !== null;

  try {
    switch (method) {
      case 'initialize':
        return reply(id, {
          protocolVersion: params?.protocolVersion || DEFAULT_PROTOCOL,
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER_INFO,
          instructions:
            'Local keyless web search. Call web_search for any web lookup; it already ' +
            'includes fallbacks, caching and timeouts. Use web_search_status only to debug.',
        });

      case 'notifications/initialized':
      case 'notifications/cancelled':
        return undefined;

      case 'ping':
        return reply(id, {});

      case 'tools/list':
        return reply(id, { tools: TOOLS });

      case 'tools/call': {
        const name = params?.name;
        const args = params?.arguments || {};
        if (!name) return replyError(id, -32602, 'params.name is required');
        try {
          return reply(id, await callTool(name, args));
        } catch (err) {
          if (err && err.code === -32602) return replyError(id, -32602, err.message);
          return reply(id, {
            content: [{ type: 'text', text: `web_search failed: ${err && err.message ? err.message : err}` }],
            isError: true,
          });
        }
      }

      case 'logging/setLevel':
        return reply(id, {});

      case 'resources/list':
        return reply(id, { resources: [] });

      case 'prompts/list':
        return reply(id, { prompts: [] });

      default:
        if (isRequest) return replyError(id, -32601, `Method not found: ${method}`);
        return undefined;
    }
  } catch (err) {
    if (isRequest) replyError(id, -32603, String((err && err.message) || err));
  }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

// Requests are handled one at a time: Chromium is a single shared page, and
// serialising keeps a burst of tool calls from fighting over it. `drain` is
// what lets a piped session (tests, `docker run -i`) finish its last call
// before the process exits on EOF.
let drain = Promise.resolve();

rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let message;
  try {
    message = JSON.parse(trimmed);
  } catch (err) {
    console.error('[websearch] ignoring non-JSON input');
    return;
  }
  drain = drain.then(() =>
    handle(message).catch((err) => console.error('[websearch] handler error:', err)),
  );
});

rl.on('close', async () => {
  await drain;
  await closeBrowser();
  process.exit(0);
});

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, async () => {
    await closeBrowser();
    process.exit(0);
  });
}

process.on('uncaughtException', (err) => {
  console.error('[websearch] uncaught exception:', err);
});
process.on('unhandledRejection', (err) => {
  console.error('[websearch] unhandled rejection:', err);
});

console.error('[websearch] ready');
