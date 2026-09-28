const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const swSource = fs.readFileSync(path.join(__dirname, '..', 'sw.js'), 'utf8');

class FakeResponse {
	constructor(body, init) {
		this.body = body;
		this.status = init && init.status;
		this.statusText = init && init.statusText;
	}
	clone() {
		return new FakeResponse(this.body, { status: this.status, statusText: this.statusText });
	}
}

function loadSW({ fetchImpl, cacheMatch }) {
	const listeners = {};
	const puts = [];
	const sandbox = {
		self: {
			addEventListener: (type, fn) => { listeners[type] = fn; },
			skipWaiting: () => {},
		},
		caches: {
			open: async () => ({
				addAll: async () => {},
				put: async (req, res) => { puts.push([String(req && req.url || req), res]); },
			}),
			keys: async () => [],
			delete: async () => true,
			match: cacheMatch,
		},
		clients: { claim: () => {} },
		fetch: fetchImpl,
		Response: FakeResponse,
		URL,
		console,
	};
	vm.createContext(sandbox);
	vm.runInContext(swSource, sandbox);
	return { listeners, puts };
}

function navigateEvent(url) {
	const event = {
		request: { url, method: 'GET', mode: 'navigate' },
		responded: null,
	};
	event.respondWith = p => { event.responded = p; };
	event.passThrough = () => { event.responded = null; };
	return event;
}

test('install precaches every URL with cache: reload (no stale HTTP copies)', async () => {
	const calls = [];
	const { listeners, puts } = loadSW({
		fetchImpl: async (url, init) => {
			calls.push([String(url), init && init.cache]);
			return { ok: true, status: 200 };
		},
		cacheMatch: async () => undefined,
	});
	const event = { waitUntil: p => { event.promise = p; } };
	listeners.install(event);
	await event.promise;
	assert.ok(calls.length >= 10, String(calls.length));
	assert.ok(calls.every(c => c[1] === 'reload'), JSON.stringify(calls[0]));
	assert.strictEqual(puts.length, calls.length);
});

test('offline navigation falls back to the cached index.html', async () => {
	const index = new FakeResponse('html');
	const { listeners } = loadSW({
		fetchImpl: async () => { throw new Error('offline'); },
		cacheMatch: async req => (String(req && req.url || req) === 'index.html' ? index : undefined),
	});
	const event = navigateEvent('http://127.0.0.1:8080/');
	listeners.fetch(event);
	const res = await event.responded;
	assert.strictEqual(res, index);
});

test('offline navigation with empty cache resolves to an offline Response', async () => {
	const { listeners } = loadSW({
		fetchImpl: async () => { throw new Error('offline'); },
		cacheMatch: async () => undefined,
	});
	const event = navigateEvent('http://127.0.0.1:8080/');
	listeners.fetch(event);
	const res = await event.responded;
	assert.ok(res instanceof FakeResponse);
	assert.strictEqual(res.status, 503);
});

test('a successful navigation is cached and returned', async () => {
	const page = new FakeResponse('html');
	const { listeners, puts } = loadSW({
		fetchImpl: async () => page,
		cacheMatch: async () => undefined,
	});
	const event = navigateEvent('http://127.0.0.1:8080/help.html');
	listeners.fetch(event);
	const res = await event.responded;
	assert.strictEqual(res, page);
	await new Promise(r => setImmediate(r));
	assert.strictEqual(puts.length, 1);
	assert.strictEqual(puts[0][0], 'http://127.0.0.1:8080/help.html');
});

test('api requests bypass the service worker', () => {
	const { listeners } = loadSW({
		fetchImpl: async () => { throw new Error('unexpected'); },
		cacheMatch: async () => undefined,
	});
	const event = navigateEvent('http://127.0.0.1:8080/api/status');
	event.request.mode = 'cors';
	listeners.fetch(event);
	assert.strictEqual(event.responded, null);
});

test('uncached asset hits the network and gets cached', async () => {
	const asset = new FakeResponse('js');
	const { listeners, puts } = loadSW({
		fetchImpl: async () => asset,
		cacheMatch: async () => undefined,
	});
	const event = navigateEvent('http://127.0.0.1:8080/des-bundle.js');
	event.request.mode = 'cors';
	listeners.fetch(event);
	const res = await event.responded;
	assert.strictEqual(res, asset);
	await new Promise(r => setImmediate(r));
	assert.strictEqual(puts.length, 1);
});
