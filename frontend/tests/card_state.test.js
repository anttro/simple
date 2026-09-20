const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name) {
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
	return src.slice(m.index, i + 1);
}

let code = 'var _pysimCardStateKey = null;\nvar _pysimCardSession = null;\n'
	+ 'var _pysimServerAvailable = null;\nvar _pysimCardEquipped = false;\n'
	+ 'var _pysimProactiveSeq = null;\nvar _pysimStkSig = null;\nvar _pysimAdmVerified = null;\n'
	+ 'var _pysimAdmKey = null;\n'
	+ 'var _pysimHeaderIccid = undefined;\nvar _pysimHeaderScp80 = undefined;\nvar _pysimHeaderScp81 = undefined;\n'
	+ 'var _cardsAutoIccid = null;\nvar _pysimCardIccid = null;\n';
code += extractFunc(html, 'pysimCardStateUpdate') + '\n';
code += extractFunc(html, 'pysimAvailabilityState') + '\n';
code += extractFunc(html, 'pysimControlDisabled') + '\n';
code += extractFunc(html, 'pysimProactiveSeqChanged') + '\n';
code += extractFunc(html, 'pysimStkStatusChanged') + '\n';
code += extractFunc(html, 'pysimUpdateAdmIndicator') + '\n';
code += extractFunc(html, 'pysimUpdateIccidIndicator') + '\n';
code += extractFunc(html, 'pysimUpdatePresetIndicator') + '\n';
code += extractFunc(html, 'pysimUpdatePresetIndicators') + '\n';
code += extractFunc(html, 'cardsMatchedPreset') + '\n';
code += extractFunc(html, 'cardsAdmPresent') + '\n';
code += extractFunc(html, 'cardsScp80Complete') + '\n';
code += extractFunc(html, 'cardsScp81Complete') + '\n';
code += extractFunc(html, 'pysimSetServerAvailable') + '\n';
code += '\nglobalThis.esc = s => s;\n';
code += 'globalThis.t = s => s;\n';
eval(code);

function fakeIndicator() {
	const classes = new Set();
	const el = {
		classes, textContent: '', title: null,
		classList: {
			add: (...c) => c.forEach(x => classes.add(x)),
			remove: (...c) => c.forEach(x => classes.delete(x)),
			contains: c => classes.has(c),
		},
		setAttribute: (k, v) => { if (k === 'title') el.title = v; },
		removeAttribute: (k) => { if (k === 'title') el.title = null; },
	};
	return el;
}

function setup() {
	const el = { textContent: 'status line', innerHTML: '' };
	const adm = fakeIndicator();
	const iccidEl = fakeIndicator();
	const scp80El = fakeIndicator();
	const scp81El = fakeIndicator();
	const calls = { connected: [], resets: [], refreshStatus: [], proactive: 0, autoIccid: [] };
	_pysimCardStateKey = null;
	_pysimCardSession = null;
	_pysimProactiveSeq = null;
	_pysimAdmVerified = null;
	_pysimAdmKey = null;
	_pysimHeaderIccid = undefined;
	_pysimHeaderScp80 = undefined;
	_pysimHeaderScp81 = undefined;
	_pysimServerAvailable = null;
	_cardsAutoIccid = null;
	_pysimCardIccid = null;
	globalThis.document = {
		getElementById: id => id === 'state-indicator-adm' ? adm
			: (id === 'state-indicator-iccid' ? iccidEl
			: (id === 'state-indicator-scp80' ? scp80El
			: (id === 'state-indicator-scp81' ? scp81El : el))),
		querySelectorAll: () => [],
	};
	globalThis.cards = [];
	globalThis.cardsFindByIccid = () => -1;
	globalThis.pysimSetConnected = v => calls.connected.push(v);
	globalThis.pysimResetCardData = refresh => calls.resets.push(refresh);
	globalThis.pysimApplyAvailability = () => {};
	globalThis.netStateRender = () => {};
	globalThis.isViewVisible = () => true;
	globalThis.pysimProactiveLogRender = () => { calls.proactive++; };
	globalThis.cardsAutoSelectByIccid = iccid => { calls.autoIccid.push(iccid); return -1; };
	return { el, adm, iccidEl, scp80El, scp81El, calls };
}

function status(extra) {
	return Object.assign({ connected: false, card_present: false, equipping: false, auto_equip: false, card_session: 1 }, extra);
}

test('disconnect without card shows the no-card message', () => {
	const { el, calls } = setup();
	pysimCardStateUpdate(status({}));
	assert.deepStrictEqual(calls.connected, [false]);
	assert.ok(el.innerHTML.includes('No card detected'), el.innerHTML);
});

test('disconnect with card present shows the Equip hint when auto-equip is off', () => {
	const { el } = setup();
	pysimCardStateUpdate(status({ card_present: true }));
	assert.ok(el.innerHTML.includes('Card inserted — press Equip'), el.innerHTML);
});

test('disconnect with auto-equip shows the initializing message', () => {
	const { el } = setup();
	pysimCardStateUpdate(status({ card_present: true, auto_equip: true }));
	assert.ok(el.innerHTML.includes('initializing'), el.innerHTML);
});

test('unchanged state key does not touch the UI again', () => {
	const { el, calls } = setup();
	pysimCardStateUpdate(status({ card_session: 7 }));
	el.innerHTML = 'unchanged';
	calls.connected.length = 0;
	pysimCardStateUpdate(status({ card_session: 7 }));
	assert.deepStrictEqual(calls.connected, []);
	assert.strictEqual(el.innerHTML, 'unchanged');
});

test('connected restores the UI and reloads card data', () => {
	const { calls } = setup();
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: '8970119000004600098' }));
	assert.deepStrictEqual(calls.connected, [true]);
	assert.deepStrictEqual(calls.resets, [true]);
	assert.deepStrictEqual(calls.autoIccid, ['8970119000004600098']);
});

test('a disconnect clears the ICCID auto-selection guard', () => {
	setup();
	_cardsAutoIccid = '8970119000004600098';
	pysimCardStateUpdate(status({ connected: false, card_present: false, card_session: 5 }));
	assert.strictEqual(_cardsAutoIccid, null);
});

test('the last status ICCID is kept for the From card button', () => {
	setup();
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: '8970119000004600098' }));
	assert.strictEqual(_pysimCardIccid, '8970119000004600098');
	// equipped but unreadable -> null (button disabled)
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: null }));
	assert.strictEqual(_pysimCardIccid, null);
	// disconnect clears it as well
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: '8970119000004600098' }));
	pysimCardStateUpdate(status({ connected: false, card_present: false, card_session: 6 }));
	assert.strictEqual(_pysimCardIccid, null);
});

test('card session change triggers a data reset', () => {
	const { calls } = setup();
	pysimCardStateUpdate(status({ card_session: 3 }));
	calls.resets.length = 0;
	pysimCardStateUpdate(status({ card_session: 4 }));
	assert.deepStrictEqual(calls.resets, [false]);
});

test('first observation does not trigger a reset on its own', () => {
	const { calls } = setup();
	pysimCardStateUpdate(status({ card_session: 9 }));
	assert.deepStrictEqual(calls.resets, []);
});

test('payload without connected flag is ignored', () => {
	const { calls } = setup();
	pysimCardStateUpdate({ reader: 'x' });
	pysimCardStateUpdate(null);
	assert.deepStrictEqual(calls.connected, []);
});

test('availability state and control gating follow server/card state', () => {
	_pysimServerAvailable = null;
	assert.strictEqual(pysimAvailabilityState(), 'server-down');
	assert.strictEqual(pysimControlDisabled('server', 'server-down'), true);
	assert.strictEqual(pysimControlDisabled('card', 'server-down'), true);

	_pysimServerAvailable = true;
	_pysimCardEquipped = false;
	assert.strictEqual(pysimAvailabilityState(), 'no-card');
	assert.strictEqual(pysimControlDisabled('server', 'no-card'), false);
	assert.strictEqual(pysimControlDisabled('card', 'no-card'), true);

	_pysimCardEquipped = true;
	assert.strictEqual(pysimAvailabilityState(), 'card');
	assert.strictEqual(pysimControlDisabled('card', 'card'), false);
	assert.strictEqual(pysimControlDisabled('server', 'card'), false);
});

test('no card with auto-equip enabled still shows the no-card message', () => {
	const { el } = setup();
	pysimCardStateUpdate(status({ connected: false, card_present: false, auto_equip: true }));
	assert.ok(el.innerHTML.includes('No card detected'), el.innerHTML);
	assert.ok(!el.innerHTML.includes('initializing'), el.innerHTML);
});

test('proactive log refreshes when the status sequence changes', () => {
	const { calls } = setup();
	pysimCardStateUpdate(status({ proactive_seq: 7 }));
	pysimCardStateUpdate(status({ proactive_seq: 7 }));
	assert.strictEqual(calls.proactive, 1);
	pysimCardStateUpdate(status({ proactive_seq: 8 }));
	assert.strictEqual(calls.proactive, 2);
});

test('proactive log is not refreshed while the phone view is hidden', () => {
	const { calls } = setup();
	globalThis.isViewVisible = () => false;
	pysimCardStateUpdate(status({ proactive_seq: 3 }));
	assert.strictEqual(calls.proactive, 0);
});

test('pysimProactiveSeqChanged tracks the last sequence', () => {
	_pysimProactiveSeq = null;
	assert.ok(pysimProactiveSeqChanged(4));
	assert.ok(!pysimProactiveSeqChanged(4));
	assert.ok(pysimProactiveSeqChanged(5));
	assert.ok(!pysimProactiveSeqChanged(undefined));
	assert.ok(!pysimProactiveSeqChanged(null));
});

test('pysimStkStatusChanged detects menu state transitions', () => {
	_pysimStkSig = null;
	assert.ok(pysimStkStatusChanged({ active: false, pending: false }));
	assert.ok(!pysimStkStatusChanged({ active: false, pending: false }));
	assert.ok(pysimStkStatusChanged({ active: true, pending: true, pending_type: 'select_item' }));
	assert.ok(!pysimStkStatusChanged({ active: true, pending: true, pending_type: 'select_item' }));
	assert.ok(pysimStkStatusChanged({ active: true, pending: false }));
	assert.ok(!pysimStkStatusChanged(null));
});

test('the header ADM badge shows verified / not verified / hidden', () => {
	const { adm, calls } = setup();
	pysimCardStateUpdate(status({ connected: true, adm_verified: true }));
	assert.ok(!adm.classes.has('hidden'));
	assert.strictEqual(adm.textContent, 'ADM ✓');
	assert.ok(adm.classes.has('text-emerald-600'));
	assert.ok(adm.classes.has('dark:text-emerald-400'));
	assert.strictEqual(adm.title, 'Verified — no ADM key in the card preset');
	// an unchanged state must not rewrite the badge
	adm.textContent = '';
	pysimCardStateUpdate(status({ connected: true, adm_verified: true }));
	assert.strictEqual(adm.textContent, '', 'unchanged ADM state rewrote the badge');
	// verification lost (e.g. card reset)
	pysimCardStateUpdate(status({ connected: true, adm_verified: false }));
	assert.strictEqual(adm.textContent, 'ADM ✗');
	assert.ok(adm.classes.has('text-red-500'));
	assert.ok(!adm.classes.has('text-emerald-600'));
	assert.strictEqual(adm.title, 'No ADM key in the card preset — not verified');
	// no card session hides it
	pysimCardStateUpdate(status({ connected: false }));
	assert.ok(adm.classes.has('hidden'));
	assert.strictEqual(adm.title, null);
	// the ADM update must not disturb the connect/reset flow
	assert.deepStrictEqual(calls.connected, [true, false]);
});

test('the ADM badge marks an ADM key in the matching preset', () => {
	const { adm } = setup();
	const st = extra => status(Object.assign({ connected: true, iccid: '89701450001700031958' }, extra));
	globalThis.cards = [{ name: 'C', adm: '0011' }];
	globalThis.cardsFindByIccid = () => 0;
	pysimCardStateUpdate(st({ adm_verified: true }));
	assert.strictEqual(adm.textContent, 'ADM ✓ ⚿');
	assert.strictEqual(adm.title, 'ADM key in the card preset — verified');
	// key present, verification lost (e.g. card reset)
	pysimCardStateUpdate(st({ adm_verified: false }));
	assert.strictEqual(adm.textContent, 'ADM ✗ ⚿');
	assert.strictEqual(adm.title, 'ADM key in the card preset — not verified');
	// verified manually, preset has no ADM -> no key glyph
	globalThis.cards = [];
	pysimCardStateUpdate(st({ adm_verified: true }));
	assert.strictEqual(adm.textContent, 'ADM ✓');
	assert.strictEqual(adm.title, 'Verified — no ADM key in the card preset');
	// whitespace-only preset ADM counts as absent
	globalThis.cards = [{ name: 'C', adm: '   ' }];
	globalThis.cardsFindByIccid = () => 0;
	pysimCardStateUpdate(st({ adm_verified: true }));
	assert.strictEqual(adm.textContent, 'ADM ✓');
});

test('the header SCP80/SCP81 markers follow the matching preset', () => {
	const { scp80El, scp81El } = setup();
	globalThis.cards = [{ name: 'C', kic: '15', kid: '15', spi1: '16', spi2: '01',
		cntr: '0000000001', kicKey: 'AA', kidKey: 'BB', pskIdentity: 'id', pskKey: 'KEY' }];
	globalThis.cardsFindByIccid = () => 0;
	pysimCardStateUpdate(status({ connected: true, card_session: 2, iccid: '89701450001700031958' }));
	assert.strictEqual(scp80El.textContent, 'SCP80');
	assert.strictEqual(scp81El.textContent, 'SCP81');
	assert.ok(!scp80El.classes.has('hidden'));
	assert.ok(!scp81El.classes.has('hidden'));
	// disconnect hides them again
	pysimCardStateUpdate(status({ connected: false, card_present: false, card_session: 3 }));
	assert.ok(scp80El.classes.has('hidden'));
	assert.ok(scp81El.classes.has('hidden'));
	assert.strictEqual(scp80El.textContent, '');
});

test('losing the server hides the ADM badge and the preset markers', () => {
	const { adm, scp80El, scp81El } = setup();
	globalThis.cards = [{ name: 'C', adm: '0011', kic: '15', kid: '15', spi1: '16', spi2: '01',
		cntr: '0000000001', kicKey: 'AA', kidKey: 'BB', pskIdentity: 'id', pskKey: 'KEY' }];
	globalThis.cardsFindByIccid = () => 0;
	pysimCardStateUpdate(status({ connected: true, adm_verified: true, iccid: '89701450001700031958' }));
	assert.ok(!adm.classes.has('hidden'));
	assert.ok(!scp80El.classes.has('hidden'));
	assert.ok(!scp81El.classes.has('hidden'));
	pysimSetServerAvailable(false);
	assert.ok(adm.classes.has('hidden'));
	assert.ok(scp80El.classes.has('hidden'));
	assert.ok(scp81El.classes.has('hidden'));
});

test('the header indicator prints the equipped card ICCID', () => {
	const { iccidEl } = setup();
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: '89701450001700031958' }));
	assert.strictEqual(iccidEl.textContent, '89701450001700031958');
	assert.ok(!iccidEl.classes.has('hidden'));
	// equipped but unreadable -> hidden again
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: null }));
	assert.strictEqual(iccidEl.textContent, '');
	assert.ok(iccidEl.classes.has('hidden'));
	// losing the server hides it too
	pysimCardStateUpdate(status({ connected: true, card_present: true, card_session: 2, iccid: '89701450001700031958' }));
	pysimSetServerAvailable(false);
	assert.ok(iccidEl.classes.has('hidden'));
});
