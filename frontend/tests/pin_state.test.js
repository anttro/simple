// The per-PIN lock state (v4.2.0, v4.3.0 glyphs): the header badge, the
// reader's PIN/ADM rows and the Verify gates.
//
// The state comes from the server's attempt-free readout (`/api/status.chv` /
// `POST /api/chv-status`): the application DF's PS template `C6` decides a
// disabled PIN (TS 102 221 §11.1.1.3.1 Table 11.3 / §9.5.2) and the empty-data
// VERIFY PIN supplies the retry counter (§11.1.9.1.2); a 2G SIM reads its
// DF.GSM SELECT bytes (TS 51 011 §9.2.1).  The badge glyphs are not translated.
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
	'pysimPinNotRequiredText', 'pysimPinTriesLeft', 'pysimPinStateText',
	'pysimPinStateLabel', 'pysimAdmStateText', 'pysimPresetCodePresent',
	'pysimPinLegendRender', 'pysimPinStateRender', 'pysimUpdatePinIndicator',
	'pysimVisibleElement', 'cardsShowEditor']) {
	code += extractFunc(html, f) + '\n';
}
code += extractFunc(html, 'pysimRefreshChvState', true) + '\n';
code += extractBlock(html, 'const PYSIM_PIN_BADGES =', 'PYSIM_PIN_BADGES') + '\n';
code += extractBlock(html, 'const PYSIM_CHV_REFRESH_STALE_MS =',
	'PYSIM_CHV_REFRESH_STALE_MS') + '\n';
// The pill's own renderer is covered by chv.test.js; the rows only need the
// hook (pysimPinStateRender drives it after every poll).
code += 'var pysimChvStateRender = function() {};\n';
code += 'var _pysimChvState = null;\n';
code += 'var _pysimPinStateKey = null;\n';
code += 'var _pysimChvRefreshing = false;\n';
code += 'var _pysimChvRefreshAt = 0;\n';
code += 'var _pysimAdmAttemptsLeft = null;\n';
code += 'var _pysimAdmBlocked = false;\n';
code += 'var _pysimLastStatus = { connected: true };\n';
code += 'var _cardsView = "list";\n';
eval(code);

// A minimal DOM: the badge, the reader rows, the message lines and the Cards
// editor views.  Unknown ids auto-vivify (the pill/rows touch fixed ids only).
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

const ELEMENT_IDS = ['state-indicator-pin', 'pysim-pin1-state',
	'pysim-pin2-state', 'pysim-adm-state', 'pysim-pin-msg', 'pysim-chv-result',
	'pysim-status', 'pysim-pin-legend', 'cards-list-view', 'cards-editor-view'];

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
	_pysimAdmAttemptsLeft = null;
	_pysimAdmBlocked = false;
	_pysimLastStatus = { connected: true };
	global.t = s => s;
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true };
	};
	global.pysimApplyAvailability = () => { availabilityCalls += 1; };
	global.navRecord = () => { navCalls += 1; };
	// no matched preset unless a test says otherwise (the legend follows it)
	global.cardsMatchedPreset = () => null;
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

test('the badge renders all six PIN states as glyphs', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];

	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(badge.textContent, 'PIN1 !');
	assert.ok(badge.classList.contains('text-amber-600'));
	assert.ok(badge.classList.contains('dark:text-amber-400'));
	assert.ok(!badge.classList.contains('hidden'));
	assert.match(badge.getAttribute('title'), /^PIN1 verification is required — 3 tries left/);
	assert.match(badge.getAttribute('title'), /click to re-read/);
	assert.strictEqual(badge.getAttribute('role'), 'button');
	assert.strictEqual(badge.getAttribute('onclick'), 'pysimRefreshChvState()');

	pysimUpdatePinIndicator(status('required', { attempts_left: null, sw: '63C1' }));
	assert.doesNotMatch(badge.getAttribute('title'), /tries left/);

	pysimUpdatePinIndicator(status('not_required', { sw: '9000' }));
	assert.strictEqual(badge.textContent, 'PIN1 ✓');
	assert.ok(badge.classList.contains('text-emerald-600'));
	assert.match(badge.getAttribute('title'), /not required on this card/);

	pysimUpdatePinIndicator(status('blocked', { attempts_left: 0, sw: '6983' }));
	assert.strictEqual(badge.textContent, 'PIN1 ✗');
	assert.ok(badge.classList.contains('text-red-500'));
	assert.match(badge.getAttribute('title'), /blocked — no retries left/);

	pysimUpdatePinIndicator(status('not_usable', { sw: '6984' }));
	assert.strictEqual(badge.textContent, 'PIN1 ⊘');
	assert.ok(badge.classList.contains('text-gray-500'));
	assert.match(badge.getAttribute('title'), /not usable/);

	pysimUpdatePinIndicator(status('not_initialized', { sw: '6A88' }));
	assert.strictEqual(badge.textContent, 'PIN1 –');
	assert.match(badge.getAttribute('title'), /no such PIN/);

	pysimUpdatePinIndicator(status('unknown', { sw: '6985' }));
	assert.strictEqual(badge.textContent, 'PIN1 ?');
	assert.match(badge.getAttribute('title'), /PIN state unknown — SW: 6985/);
});

test('the badge glyphs are not translated', () => {
	const badges = extractBlock(html, 'const PYSIM_PIN_BADGES =', 'badges');
	assert.doesNotMatch(badges, /t\(/, 'the glyph labels must not go through t()');
	assert.match(html, /el\.textContent = badge\[0\];/);
});

test('the enabled flag distinguishes disabled from already verified', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('not_required',
		{ enabled: false, attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(badge.textContent, 'PIN1 ✓');
	assert.match(badge.getAttribute('title'), /disabled on this card/);
	assert.match(elements['pysim-pin1-state'].textContent,
		/not required — disabled on this card/);
	pysimUpdatePinIndicator(status('not_required', { enabled: true, sw: '9000' }));
	assert.match(badge.getAttribute('title'), /already verified/);
	assert.match(elements['pysim-pin1-state'].textContent,
		/not required — already verified/);
	pysimUpdatePinIndicator(status('not_required', { enabled: null, sw: '9000' }));
	assert.match(elements['pysim-pin1-state'].textContent,
		/not required — no PIN needed/);
});

test('a one-try count uses the singular form', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('required', { attempts_left: 1, sw: '63C1' }));
	assert.match(badge.getAttribute('title'), /— 1 try left/);
	assert.match(elements['pysim-pin1-state'].textContent, /required, 1 try left/);
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.match(elements['pysim-pin1-state'].textContent, /required, 3 tries left/);
});

test('the badge is hidden without a card session and the rows stay blank', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.ok(!badge.classList.contains('hidden'));
	pysimUpdatePinIndicator(null);
	assert.ok(badge.classList.contains('hidden'));
	assert.strictEqual(badge.textContent, '');
	assert.strictEqual(badge.getAttribute('title'), null);
	assert.strictEqual(_pysimChvState, null);
	assert.strictEqual(elements['pysim-pin1-state'].textContent, '');
	assert.strictEqual(elements['pysim-pin2-state'].textContent, '');
	assert.strictEqual(elements['pysim-adm-state'].textContent, '');
	pysimUpdatePinIndicator({ connected: false, chv: null });
	assert.ok(badge.classList.contains('hidden'));
});

test('the badge dedupes a repeated state, enabled included', () => {
	stubPage();
	const badge = elements['state-indicator-pin'];
	pysimUpdatePinIndicator(status('not_required', { enabled: false, sw: '9000' }));
	badge.textContent = 'sentinel';
	pysimUpdatePinIndicator(status('not_required', { enabled: false, sw: '9000' }));
	assert.strictEqual(badge.textContent, 'sentinel');
	pysimUpdatePinIndicator(status('not_required', { enabled: true, sw: '9000' }));
	assert.strictEqual(badge.textContent, 'PIN1 ✓');
	assert.match(badge.getAttribute('title'), /already verified/);
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(badge.textContent, 'PIN1 !');
	badge.textContent = 'sentinel';
	pysimUpdatePinIndicator(status('required', { attempts_left: 2, sw: '63C2' }));
	assert.strictEqual(badge.textContent, 'PIN1 !');
});

test('the reader rows show each PIN state', () => {
	stubPage();
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(elements['pysim-pin1-state'].textContent,
		'required, 3 tries left');
	assert.strictEqual(elements['pysim-pin2-state'].textContent,
		'not required — no PIN needed');
	pysimUpdatePinIndicator(status('blocked'));
	assert.strictEqual(elements['pysim-pin1-state'].textContent, 'blocked');
	pysimUpdatePinIndicator(status('not_usable'));
	assert.strictEqual(elements['pysim-pin1-state'].textContent, 'not usable');
	pysimUpdatePinIndicator(status('not_initialized'));
	assert.strictEqual(elements['pysim-pin1-state'].textContent, 'not initialized');
	pysimUpdatePinIndicator(status('unknown', { sw: '6985' }));
	assert.strictEqual(elements['pysim-pin1-state'].textContent, 'unknown (6985)');
});

test('the ADM row follows the verify latch, the attempts and the blocked state', () => {
	stubPage();
	_pysimLastStatus = { connected: true, adm_verified: false };
	pysimUpdatePinIndicator(_pysimLastStatus);
	assert.strictEqual(elements['pysim-adm-state'].textContent, 'not verified');
	_pysimLastStatus = { connected: true, adm_verified: true };
	pysimUpdatePinIndicator(_pysimLastStatus);
	assert.strictEqual(elements['pysim-adm-state'].textContent, 'verified');
	_pysimLastStatus = { connected: true, adm_verified: false };
	_pysimAdmAttemptsLeft = 2;
	pysimUpdatePinIndicator(_pysimLastStatus);
	assert.strictEqual(elements['pysim-adm-state'].textContent,
		'verification failed, 2 attempt(s) left');
	_pysimAdmBlocked = true;
	pysimUpdatePinIndicator(_pysimLastStatus);
	assert.match(elements['pysim-adm-state'].textContent,
		/^blocked — unblock the card to try again/);
});

test('the Verify gate only blocks a PIN that has nothing to verify', () => {
	stubPage();
	for (const state of ['required', 'unknown']) {
		pysimUpdatePinIndicator(status(state));
		assert.strictEqual(pysimPinVerifyGate('pin1'), null, state);
	}
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
	_pysimChvState = null;
	assert.strictEqual(pysimPinVerifyGate('pin1'), null);
});

test('the gate and the preset-code check are wired into availability and the rows', () => {
	const apply = /function pysimApplyAvailability\(\)[\s\S]*?\n\}/.exec(html);
	assert.ok(apply, 'pysimApplyAvailability not found');
	const body = apply[0];
	assert.match(body, /const pinGate = el\.getAttribute\('data-pin-gate'\)/);
	assert.match(body, /const gate = pysimPinVerifyGate\(pinGate\)/);
	assert.match(body, /const presetCode = el\.getAttribute\('data-preset-code'\)/);
	assert.match(body, /pysimPresetCodePresent\(presetCode\)/);
	assert.match(body, /const chvGate = el\.getAttribute\('data-chv-gate'\)/);
	assert.match(body, /disabled = true;/);
	// the reader's three Verify buttons: PIN1/PIN2 gated by the state and the
	// preset code, ADM by the preset key
	for (const call of ["pysimVerifyPin('pin1')", "pysimVerifyPin('pin2')"]) {
		const re = new RegExp('<button[^>]*data-pin-gate="pin\\d"[^>]*' +
			call.replace(/[()']/g, '\\$&'));
		assert.match(html, re, 'no data-pin-gate on the ' + call + ' button');
	}
	assert.match(html,
		/<button[^>]*data-preset-code="pin1"[^>]*onclick="pysimVerifyPin\('pin1'\)"/);
	assert.match(html,
		/<button[^>]*data-preset-code="pin2"[^>]*onclick="pysimVerifyPin\('pin2'\)"/);
	assert.match(html,
		/<button[^>]*data-preset-code="adm"[^>]*onclick="pysimVerifyAdm\(\)"/);
});

test('the preset-code check follows the matched preset', () => {
	stubPage();
	global.cardsMatchedPreset = () => null;
	assert.strictEqual(pysimPresetCodePresent('pin1'), false);
	global.cardsMatchedPreset = () => ({ pin1: '1111', pin2: '', adm: 'AABBCC' });
	assert.strictEqual(pysimPresetCodePresent('pin1'), true);
	assert.strictEqual(pysimPresetCodePresent('pin2'), false);
	global.cardsAdmPresent = preset => !!(preset && preset.adm);
	assert.strictEqual(pysimPresetCodePresent('adm'), true);
});

test('the PIN / ADM legend names the matched preset', () => {
	stubPage();
	// no matched preset: the base text only
	global.cardsMatchedPreset = () => null;
	pysimPinLegendRender();
	assert.strictEqual(elements['pysim-pin-legend'].textContent, 'PIN / ADM');
	// a matched preset appends its name
	global.cardsMatchedPreset = () => ({ id: 'p1', name: 'Bench card' });
	pysimPinLegendRender();
	assert.strictEqual(elements['pysim-pin-legend'].textContent,
		'PIN / ADM · Bench card');
	// the poll path follows the preset as it appears and disappears
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(elements['pysim-pin-legend'].textContent,
		'PIN / ADM · Bench card');
	global.cardsMatchedPreset = () => null;
	pysimUpdatePinIndicator(status('required', { attempts_left: 3, sw: '63C3' }));
	assert.strictEqual(elements['pysim-pin-legend'].textContent, 'PIN / ADM');
	// a preset without a name adds nothing
	global.cardsMatchedPreset = () => ({ id: 'p2', name: '' });
	pysimPinLegendRender();
	assert.strictEqual(elements['pysim-pin-legend'].textContent, 'PIN / ADM');
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
	_pysimLastStatus = { connected: false };
	cardsShowEditor();
	assert.strictEqual(navCalls, 1, 'the editor still opens');
	assert.strictEqual(fetchCalls.length, 0);
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
	assert.strictEqual(_pysimLastStatus.chv.pin1.attempts_left, 1);
	assert.strictEqual(elements['state-indicator-pin'].getAttribute('title'),
		'PIN1 verification is required — 1 try left — click to re-read');
	assert.strictEqual(availabilityCalls, 1);
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
	assert.strictEqual(second, null);
	assert.strictEqual(resolvers.length, 1);
	resolvers[0]({ ok: true });
	await first;
	assert.strictEqual(_pysimChvRefreshing, false);

	stubPage();
	_pysimChvRefreshing = true;
	_pysimChvRefreshAt = Date.now();
	assert.strictEqual(await pysimRefreshChvState(), null);
	assert.strictEqual(fetchCalls.length, 0);
	_pysimChvRefreshAt = Date.now() - (PYSIM_CHV_REFRESH_STALE_MS + 1000);
	const data = await pysimRefreshChvState();
	assert.ok(data, 'the stale guard must be released');
	assert.strictEqual(fetchCalls.length, 1);
	assert.strictEqual(_pysimChvRefreshing, false);
});

test('refreshed state errors go to a visible message line, never thrown', async () => {
	stubPage();
	global.pysimFetch = async () => { throw new Error('boom'); };
	const data = await pysimRefreshChvState();
	assert.strictEqual(data, null);
	assert.match(elements['pysim-pin-msg'].textContent, /Error: boom/);
	assert.strictEqual(elements['pysim-pin1-state'].textContent, '',
		'the state line must not hold the error');
	assert.strictEqual(_pysimChvRefreshing, false);
});

test('the state keys resolve in LANG_RU', () => {
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
	for (const key of ['PIN1 verification is required', 'tries left', 'try left',
		'click to re-read', 'required', 'not required — no PIN needed',
		'not required — disabled on this card', 'not required — already verified',
		'blocked', 'not usable', 'not initialized', 'unknown', 'PIN state unknown',
		'unblock the card to try again', 'verified', 'not verified',
		'PIN1 is disabled on this card — no PIN is needed',
		'PIN1 is already verified — no PIN is needed',
		'PIN verification is not required on this card — there is nothing to verify',
		'the card reports this PIN reference as not usable — there is nothing to verify',
		'the card has no such PIN — there is nothing to verify']) {
		assert.ok(keys.has(norm(key)), 'missing RU entry: ' + key);
	}
	// the old word badges are gone (v4.3.0 glyphs)
	for (const gone of ['PIN1 locked', 'PIN1 unlocked', 'PIN1 not usable',
		'PIN1 n/a']) {
		assert.ok(!keys.has(gone), 'stale badge label still translated: ' + gone);
	}
});
