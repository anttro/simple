// The per-PIN lock state (v4.2.0): the header badge, the per-PIN state lines
// and the Verify gate.  The state comes from the server's attempt-free readout
// (`/api/status.chv` / `POST /api/chv-status`) - a UICC's FCP PS template
// (TS 102 221 §9.5.2: which references are enabled) combined with the
// empty-data VERIFY PIN probe (TS 102 221 §11.1.9.1.2: the retry counter), or
// a 2G SIM's DF.GSM SELECT bytes (TS 51 011 §9.2.1) - so the PWA only renders
// it.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name, asyncFn) {
	const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
	const m = re.exec(src);
	if (!m) throw new Error('function ' + name + ' not found');
	let i = m.index + m[0].length - 1;
	let depth = 0;
	for (; i < src.length; i++) {
		if (src[i] === '{') depth++;
		else if (src[i] === '}') {
			depth--;
			if (depth === 0) break;
		}
	}
	return (asyncFn ? 'async ' : '') + src.slice(m.index, i + 1);
}

function extractBlock(src, start, name) {
	const from = src.indexOf(start);
	if (from < 0) throw new Error('block ' + name + ' not found');
	const end = src.indexOf(';', from);
	if (end < 0) throw new Error('block ' + name + ' is not terminated');
	return 'var ' + src.slice(from + 'const '.length, end) + ';\n';
}

let code = '';
for (const f of ['pysimPinStateEntry', 'pysimPinState', 'pysimPinVerifyGate',
	'pysimPinNotRequiredText', 'pysimPinTriesLeft', 'pysimPinStateLabel',
	'pysimPinStateRender', 'pysimUpdatePinIndicator', 'pysimVisibleElement',
	'cardsShowEditor']) {
	code += extractFunc(html, f) + '\n';
}
code += extractFunc(html, 'pysimRefreshChvState', true) + '\n';
code += extractBlock(html, 'const PYSIM_PIN_BADGES =', 'PYSIM_PIN_BADGES') + '\n';
code += extractBlock(html, 'const PYSIM_CHV_REFRESH_STALE_MS =',
	'PYSIM_CHV_REFRESH_STALE_MS') + '\n';
code += 'var _pysimChvState = null;\n';
code += 'var _pysimPinStateKey = null;\n';
code += 'var _pysimChvRefreshing = false;\n';
code += 'var _pysimChvRefreshAt = 0;\n';
code += 'var _pysimLastStatus = { connected: true };\n';
code += 'var _cardsView = "list";\n';
eval(code);

// A minimal DOM: the badge, the two state lines, the Cards editor views and
// the message lines the error path writes to.
function element(id) {
	const classes = new Set();
	const attrs = {};
	return {
		id,
		textContent: '',
		disabled: false,
		attrs,
		classList: {
			add: (...names) => names.forEach(n => classes.add(n)),
			remove: (...names) => names.forEach(n => classes.delete(n)),
			contains: name => classes.has(name),
		},
		setAttribute: (k, v) => { attrs[k] = String(v); },
		getAttribute: k => (k in attrs ? attrs[k] : null),
		removeAttribute: k => { delete attrs[k]; },
	};
}

const ELEMENT_IDS = ['state-indicator-pin', 'cards-pin-state', 'pysim-pin-state',
	'cards-chv-status', 'pysim-pin-msg', 'cards-list-view', 'cards-editor-view'];

let elements = {};
let fetchCalls = [];
let availabilityCalls = 0;
let navCalls = 0;
function stubPage() {
	elements = {};
	for (const id of ELEMENT_IDS) elements[id] = element(id);
	fetchCalls = [];
	availabilityCalls = 0;
	navCalls = 0;
	_pysimChvState = null;
	_pysimPinStateKey = null;
	_pysimChvRefreshing = false;
	_pysimChvRefreshAt = 0;
	_pysimLastStatus = { connected: true };
	global.t = s => s;
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true };
	};
	global.pysimApplyAvailability = () => { availabilityCalls += 1; };
	global.navRecord = () => { navCalls += 1; };
	global.document = {
		getElementById: id => (id in elements ? elements[id] : null),
	};
}

function status(state, entry) {
	return Object.assign({ connected: true }, {
		chv: {
			pin1: Object.assign({ state, attempts_left: null, sw: null,
				enabled: null }, entry || {}),
			pin2: { state: 'not_required', attempts_left: null, sw: '9000',
				enabled: null },
		},
	});
}

test('the badge renders all six PIN states', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];

	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(badge.textContent, 'PIN1 locked');
	assert.ok(badge.classList.contains('text-amber-600'));
	assert.ok(badge.classList.contains('dark:text-amber-400'));
	assert.ok(!badge.classList.contains('hidden'));
	assert.match(badge.getAttribute('title'), /^PIN1 verification is required — 3 tries left/);
	assert.match(badge.getAttribute('title'), /click to re-read/);
	assert.strictEqual(badge.getAttribute('role'), 'button');
	assert.strictEqual(badge.getAttribute('onclick'), 'pysimRefreshChvState()');

	pysimUpdatePinIndicator(status('required', { attempts_left: null, sw: '63C1' }));
	// no count known: the tooltip must not invent one
	assert.doesNotMatch(badge.getAttribute('title'), /tries left/);

	pysimUpdatePinIndicator(status('not_required', { sw: '9000' }));
	assert.strictEqual(badge.textContent, 'PIN1 unlocked');
	assert.ok(badge.classList.contains('text-emerald-600'));
	assert.match(badge.getAttribute('title'), /not required on this card/);

	pysimUpdatePinIndicator(status('blocked', { attempts_left: 0, sw: '6983' }));
	assert.strictEqual(badge.textContent, 'PIN1 blocked');
	assert.ok(badge.classList.contains('text-red-500'));
	assert.match(badge.getAttribute('title'), /blocked — no retries left/);

	pysimUpdatePinIndicator(status('not_usable', { sw: '6984' }));
	assert.strictEqual(badge.textContent, 'PIN1 not usable');
	assert.ok(badge.classList.contains('text-amber-600'));
	assert.match(badge.getAttribute('title'), /not usable/);

	pysimUpdatePinIndicator(status('not_initialized', { sw: '6A88' }));
	assert.strictEqual(badge.textContent, 'PIN1 n/a');
	assert.ok(badge.classList.contains('text-gray-500'));
	assert.match(badge.getAttribute('title'), /no such PIN/);

	pysimUpdatePinIndicator(status('unknown', { sw: '6985' }));
	assert.strictEqual(badge.textContent, 'PIN1 ?');
	assert.match(badge.getAttribute('title'), /PIN state unknown — SW: 6985/);
});

test('the enabled flag distinguishes disabled from already verified', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	// C6 says the reference is disabled (a disabled PIN may still answer 63CX)
	pysimUpdatePinIndicator(status('not_required',
		{ enabled: false, attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(badge.textContent, 'PIN1 unlocked');
	assert.match(badge.getAttribute('title'), /disabled on this card/);
	assert.match(elements['cards-pin-state'].textContent,
		/PIN1: not required — disabled on this card/);
	// C6 says enabled, the probe answered 9000: already verified
	pysimUpdatePinIndicator(status('not_required', { enabled: true, sw: '9000' }));
	assert.match(badge.getAttribute('title'), /already verified/);
	assert.match(elements['cards-pin-state'].textContent,
		/PIN1: not required — already verified/);
	// no flag at all (no C6): the generic wording
	pysimUpdatePinIndicator(status('not_required', { enabled: null, sw: '9000' }));
	assert.match(badge.getAttribute('title'), /not required on this card/);
	assert.match(elements['cards-pin-state'].textContent,
		/PIN1: not required — no PIN needed/);
});

test('a one-try count uses the singular form', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('required', { attempts_left: 1, sw: '63C1' }));
	assert.match(badge.getAttribute('title'), /— 1 try left/);
	assert.match(elements['cards-pin-state'].textContent, /PIN1: required, 1 try left/);
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.match(badge.getAttribute('title'), /— 3 tries left/);
	assert.match(elements['cards-pin-state'].textContent, /PIN1: required, 3 tries left/);
});

test('the badge is hidden without a card session and re-renders on change', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.ok(!badge.classList.contains('hidden'));
	pysimUpdatePinIndicator(null);
	assert.ok(badge.classList.contains('hidden'));
	assert.strictEqual(badge.textContent, '');
	assert.strictEqual(badge.getAttribute('title'), null);
	assert.strictEqual(badge.getAttribute('onclick'), null);
	assert.strictEqual(_pysimChvState, null);
	// no state: the rows stay blank (no invented "PIN1: unknown")
	assert.strictEqual(elements['cards-pin-state'].textContent, '');
	assert.strictEqual(elements['pysim-pin-state'].textContent, '');
	// a disconnected status behaves the same
	pysimUpdatePinIndicator(status('required'));
	pysimUpdatePinIndicator({ connected: false, chv: null });
	assert.ok(badge.classList.contains('hidden'));
});

test('the badge dedupes a repeated state, enabled included', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('not_required', { enabled: false, sw: '9000' }));
	badge.textContent = 'sentinel';
	// the same state must not rewrite the DOM (the 2 s poll calls it)
	pysimUpdatePinIndicator(status('not_required', { enabled: false, sw: '9000' }));
	assert.strictEqual(badge.textContent, 'sentinel');
	pysimUpdatePinIndicator(status('not_required', { enabled: true, sw: '9000' }));
	assert.strictEqual(badge.textContent, 'PIN1 unlocked');
	assert.match(badge.getAttribute('title'), /already verified/);
	// a changed attempt count re-renders too
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(badge.textContent, 'PIN1 locked');
	badge.textContent = 'sentinel';
	pysimUpdatePinIndicator(status('required', { attempts_left: 2, sw: '63C2' }));
	assert.strictEqual(badge.textContent, 'PIN1 locked');
});

test('the per-PIN state lines name both PINs', () => {
	stubPage();
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	const text = elements['cards-pin-state'].textContent;
	assert.match(text, /^PIN1: required, 3 tries left/);
	assert.match(text, /PIN2: not required — no PIN needed$/);
	assert.strictEqual(elements['pysim-pin-state'].textContent, text);

	pysimUpdatePinIndicator(status('blocked'));
	assert.match(elements['cards-pin-state'].textContent, /PIN1: blocked/);

	pysimUpdatePinIndicator(status('not_usable'));
	assert.match(elements['cards-pin-state'].textContent, /PIN1: not usable/);
	pysimUpdatePinIndicator(status('not_initialized'));
	assert.match(elements['cards-pin-state'].textContent, /PIN1: not initialized/);
	pysimUpdatePinIndicator(status('unknown', { sw: '6985' }));
	assert.match(elements['cards-pin-state'].textContent, /PIN1: unknown \(6985\)/);
});

test('the Verify gate only blocks a PIN that has nothing to verify', () => {
	stubPage();
	// required and unknown keep the Verify action (the card may still ask)
	for (const state of ['required', 'unknown']) {
		pysimUpdatePinIndicator(status(state));
		assert.strictEqual(pysimPinVerifyGate('pin1'), null, state);
	}
	// the gate repeats the row/tooltip wording and says why it is disabled
	pysimUpdatePinIndicator(status('not_required'));
	assert.match(pysimPinVerifyGate('pin1'),
		/no PIN needed \(there is nothing to verify\)/);
	pysimUpdatePinIndicator(status('not_required', { enabled: false }));
	assert.match(pysimPinVerifyGate('pin1'),
		/disabled on this card \(there is nothing to verify\)/);
	pysimUpdatePinIndicator(status('not_required', { enabled: true }));
	assert.match(pysimPinVerifyGate('pin1'),
		/already verified \(there is nothing to verify\)/);
	pysimUpdatePinIndicator(status('not_usable'));
	assert.match(pysimPinVerifyGate('pin1'), /not usable/);
	pysimUpdatePinIndicator(status('not_initialized'));
	assert.match(pysimPinVerifyGate('pin1'), /no such PIN/);
	pysimUpdatePinIndicator(status('blocked'));
	assert.match(pysimPinVerifyGate('pin1'), /PIN1 is blocked/);
	// no state at all: the button keeps its normal availability rules
	_pysimChvState = null;
	assert.strictEqual(pysimPinVerifyGate('pin1'), null);
});

test('the gate is wired into the availability pass and the buttons', () => {
	// The gate runs inside pysimApplyAvailability's own disabled/hint pass, so
	// the generic card hint still wins when the card is the blocker.
	const apply = /function pysimApplyAvailability\(\)[\s\S]*?\n\}/.exec(html);
	assert.ok(apply, 'pysimApplyAvailability not found');
	const body = apply[0];
	assert.match(body, /const pinGate = el\.getAttribute\('data-pin-gate'\)/);
	assert.match(body, /const gate = pysimPinVerifyGate\(pinGate\)/);
	assert.match(body, /disabled = true;/);
	for (const call of ["cardsChv('pin1','verify')", "cardsChv('pin2','verify')",
		"pysimVerifyPin('pin1')", "pysimVerifyPin('pin2')"]) {
		const re = new RegExp('<button[^>]*data-pin-gate="pin\\d"[^>]*' +
			call.replace(/[()']/g, '\\$&'));
		assert.match(html, re, 'no data-pin-gate on the ' + call + ' button');
	}
	// Change / Unblock are not gated
	assert.doesNotMatch(html, /data-pin-gate="pin1"[^>]*cardsChv\('pin1','change'\)/);
});

test('the status poll feeds the badge', () => {
	// card_state.test.js stubs pysimUpdatePinIndicator, so pin the call here.
	const source = extractFunc(html, 'pysimCardStateUpdate');
	assert.match(source, /pysimUpdateAdmIndicator\(status\);\s*pysimUpdatePinIndicator\(status\);/);
});

test('a language switch re-renders both badges', () => {
	const source = extractFunc(html, 'refreshDynamicI18n');
	assert.match(source, /_pysimAdmStateKey = null/);
	assert.match(source, /_pysimPinStateKey = null/);
	assert.match(source, /pysimUpdateAdmIndicator\(_pysimLastStatus\)/);
	assert.match(source, /pysimUpdatePinIndicator\(_pysimLastStatus\)/);
});

test('the Cards editor only refreshes with a card session', async () => {
	stubPage();
	// no card session: an offline editor must not fire a failing POST
	_pysimLastStatus = { connected: false };
	cardsShowEditor();
	assert.strictEqual(navCalls, 1, 'the editor still opens');
	assert.strictEqual(fetchCalls.length, 0);
	// equipped: the state is re-read
	_pysimLastStatus = { connected: true };
	cardsShowEditor();
	assert.deepStrictEqual(fetchCalls, [{ path: '/api/chv-status', body: {} }]);
});

test('the badge click re-reads the state through /api/chv-status', async () => {
	stubPage();
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true, chv: {
			pin1: { state: 'required', attempts_left: 1, sw: '63C1',
				enabled: true },
			pin2: { state: 'not_required', attempts_left: null, sw: '9000',
				enabled: true },
		} };
	};
	await pysimRefreshChvState();
	assert.deepStrictEqual(fetchCalls, [{ path: '/api/chv-status', body: {} }]);
	assert.strictEqual(_pysimChvState.pin1.attempts_left, 1);
	// the last status is kept in step, so a language switch re-renders it
	assert.strictEqual(_pysimLastStatus.chv.pin1.attempts_left, 1);
	assert.strictEqual(elements['state-indicator-pin'].getAttribute('title'),
		'PIN1 verification is required — 1 try left — click to re-read');
	assert.strictEqual(availabilityCalls, 1, 'the Verify gates refresh too');

	fetchCalls = [];
	await pysimRefreshChvState(['pin2']);
	assert.deepStrictEqual(fetchCalls, [{ path: '/api/chv-status', body: { codes: ['pin2'] } }]);
});

test('a re-entrant refresh is ignored, a stale guard is released', async () => {
	stubPage();
	const resolvers = [];
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return new Promise(resolve => resolvers.push(resolve));
	};
	const first = pysimRefreshChvState();
	const second = await pysimRefreshChvState();
	assert.strictEqual(second, null, 'the re-entrant call must not fetch');
	assert.strictEqual(resolvers.length, 1);
	resolvers[0]({ ok: true });
	await first;
	assert.strictEqual(_pysimChvRefreshing, false);

	// A fresh guard still blocks ...
	stubPage();
	_pysimChvRefreshing = true;
	_pysimChvRefreshAt = Date.now();
	assert.strictEqual(await pysimRefreshChvState(), null);
	assert.strictEqual(fetchCalls.length, 0);
	// ... but a stalled request must not freeze the badge forever (the
	// v3.28.7 RAM-panel class): a guard older than the timeout is released.
	_pysimChvRefreshAt = Date.now() - (PYSIM_CHV_REFRESH_STALE_MS + 1000);
	const data = await pysimRefreshChvState();
	assert.ok(data, 'the stale guard must be released');
	assert.strictEqual(fetchCalls.length, 1);
	assert.strictEqual(_pysimChvRefreshing, false);
});

test('refreshed state errors go to the message lines, never thrown', async () => {
	stubPage();
	global.pysimFetch = async () => { throw new Error('boom'); };
	const data = await pysimRefreshChvState();
	assert.strictEqual(data, null);
	assert.match(elements['cards-chv-status'].textContent, /Error: boom/);
	assert.strictEqual(elements['pysim-pin-state'].textContent, '',
		'the state line must not hold the error');
	assert.strictEqual(_pysimChvRefreshing, false);

	// The Cards editor's line is hidden on every other tab: pick the next
	// visible message line instead.
	stubPage();
	elements['cards-chv-status'].closest = sel => (sel === '.hidden' ? {} : null);
	global.pysimFetch = async () => { throw new Error('boom'); };
	await pysimRefreshChvState();
	assert.strictEqual(elements['cards-chv-status'].textContent, '',
		'a hidden line must not receive the error');
	assert.match(elements['pysim-pin-msg'].textContent, /Error: boom/);
});

test('the badge labels and the state keys resolve in LANG_RU', () => {
	const dictMatch = html.match(/const LANG_RU = \{([\s\S]*?)\n\};/);
	assert.ok(dictMatch, 'LANG_RU not found');
	const norm = k => k
		.replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
		.replace(/\\'/g, "'")
		.replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&#39;/g, "'");
	const keys = new Set();
	for (const m of dictMatch[1].matchAll(/^\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"):/gm)) {
		keys.add(norm(m[1] !== undefined ? m[1] : m[2]));
	}
	// PYSIM_PIN_BADGES labels are passed to t() dynamically, so the html
	// test's literal sweep cannot cover them.
	const labels = [...html.matchAll(/^\t(required|not_required|blocked|not_usable|not_initialized|unknown): \['([^']+)'/gm)]
		.map(m => m[2]);
	assert.strictEqual(labels.length, 6, 'PYSIM_PIN_BADGES labels: ' + labels.join(', '));
	const missing = labels.filter(label => !keys.has(label));
	assert.deepStrictEqual(missing, [], 'PIN badge labels with no RU entry');
	for (const key of ['PIN1 verification is required', 'tries left', 'try left',
		'click to re-read', 'required', 'not required — no PIN needed',
		'not required — disabled on this card', 'not required — already verified',
		'blocked', 'not usable', 'not initialized', 'unknown', 'PIN state unknown',
		'PIN1 is disabled on this card — no PIN is needed',
		'PIN1 is already verified — no PIN is needed',
		'PIN verification is not required on this card — there is nothing to verify',
		'the card reports this PIN reference as not usable — there is nothing to verify',
		'the card has no such PIN — there is nothing to verify']) {
		assert.ok(keys.has(norm(key)), 'missing RU entry: ' + key);
	}
});
