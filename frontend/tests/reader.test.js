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

// The reader selector and the status block render the /api/status shape the
// pysim-free server defines (Phase 3d-2), not pySim internals.
let code = 'var _pysimReaderKey = null;\nvar _pysimActiveReader = null;\n';
// the app's esc(): the status values are escaped (quotes included)
code += "globalThis.esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')"
	+ ".replace(/>/g, '&gt;').replace(/\"/g, '&quot;').replace(/'/g, '&#39;');\n";
code += 'globalThis.t = s => s;\n';
code += extractFunc(html, 'pysimStatusHtml') + '\n';
code += extractFunc(html, 'pysimReaderRender') + '\n';
code += extractFunc(html, 'pysimReaderChanged', true) + '\n';
// one evaluation scope: the module-level reader state is shared by the three
// extracted functions (a separate scope per function would lose it)
const { pysimStatusHtml: statusHtml, pysimReaderRender: readerRender,
	pysimReaderChanged: readerChanged } = new Function(
	code + '; return { pysimStatusHtml, pysimReaderRender, pysimReaderChanged };')();

test('the status block renders our own /api/status shape, not pySim internals', () => {
	const out = statusHtml({
		connected: true, card_present: true, equipping: false,
		reader: 'ACS ACR39U', atr: '3B9F', iccid: '8970119000004600098',
		card: { kind: 'uicc', name: 'UICC' }, adm_verified: true,
		selection: { path: 'MF/7F10/6F46', name: 'EF.SPN', fid: '6F46',
			kind: 'transparent', size: 17, record_len: null, num_records: null },
		proactive_mode: 'polling',
		poll: { enabled: true, suspended: false, interval: 30 },
		test_run: { running: false },
		bip: { owner: null, bip: { enabled: false, channels: [] } },
		scp80: null,
	});
	// the reader lives in its own block now, not in the card grid
	assert.ok(!out.includes('ACS ACR39U'), out);
	// the definition-grid labels, in order: one Card row for the identity,
	// presence and init state (v4.4.0), no session/presence rows
	for (const label of ['Card:', 'ATR:', 'ICCID:', 'Selection:', 'State:']) {
		assert.ok(out.includes('>' + label + '<'), label + ' in ' + out);
	}
	assert.ok(!out.includes('>Card session:<'), out);
	assert.ok(!out.includes('>Card present:<'), out);
	assert.ok(out.includes('UICC'), out);
	assert.ok(out.includes('8970119000004600098'), out);
	assert.ok(out.includes('EF.SPN'), out);
	assert.ok(out.includes('MF/7F10/6F46'), out);
	assert.ok(out.includes('mode=polling'), out);
	assert.ok(out.includes('poll=on (30s)'), out);
	assert.ok(out.includes('test run=idle'), out);
	// the rows are a two-column grid, not a '<br>'/pipe run-on
	assert.ok(!out.includes('<br>'), out);
	assert.ok(!out.includes(' | '), out);
	// an inactive BIP session reports `{owner: null, bip: {enabled: false,
	// mode: 'redirect', channels: []}}` - it is always present, so the block
	// must not claim a redirect session
	assert.ok(!out.includes('BIP='), out);
	// the legacy pySim fields are not rendered
	assert.ok(!out.includes('Profile:'), out);
	assert.ok(!out.includes('CLA'), out);
	assert.ok(!out.includes('Channels'), out);
	assert.ok(!out.includes('current_selection'), out);
});

test('the status block reports a BIP session only while one exists', () => {
	const base = {
		connected: true, card_present: true, equipping: false,
		reader: 'ACS ACR39U', card: { kind: 'uicc', name: 'UICC' },
		poll: { enabled: true, suspended: false, interval: 30 },
		test_run: { running: false },
	};
	// an owner without a channel: the mode is named
	const running = statusHtml(Object.assign({}, base, {
		bip: { owner: 'scp81', bip: { enabled: true, mode: 'redirect', channels: [] } },
	}));
	assert.ok(running.includes('BIP=redirect'), running);
	// channels alone also count (a start in progress may precede the owner)
	const channels = statusHtml(Object.assign({}, base, {
		bip: { owner: null, bip: { enabled: false, mode: 'redirect',
			channels: [{ id: 1 }] } },
	}));
	assert.ok(channels.includes('BIP=redirect (1 ch)'), channels);
	// inactive: nothing
	const idle = statusHtml(Object.assign({}, base, {
		bip: { owner: null, bip: { enabled: false, mode: 'redirect', channels: [] } },
	}));
	assert.ok(!idle.includes('BIP='), idle);
});

test('the Card row shows the reported name without a kind suffix', () => {
	const out = statusHtml({ connected: true, card: { kind: 'uicc', name: 'UICC' } });
	assert.ok(out.includes('>UICC<'), out);
	assert.ok(!out.includes('(uicc)'), 'the kind suffix is gone');
	const named = statusHtml({ connected: true, card: { kind: 'uicc', name: 'USIM' } });
	assert.ok(named.includes('>USIM<'), named);
	assert.ok(!named.includes('(uicc)'), named);
	// the name-or-kind fallback survives when the server reports only a kind
	assert.ok(statusHtml({ connected: true, card: { kind: 'sim' } }).includes('>sim<'));
});

test('the Card row merges the identity, the presence and the init state', () => {
	// a usable session (connected and a card object): the server's card name
	assert.ok(statusHtml({ connected: true, card: { kind: 'euicc', name: 'eUICC' } })
		.includes('>eUICC<'));
	assert.ok(statusHtml({ connected: true, card: { kind: 'uicc', name: 'UICC' } })
		.includes('>UICC<'));
	// equipping (not connected yet): initializing, the existing key
	const boot = statusHtml({ connected: false, equipping: true, card_present: true });
	assert.ok(boot.includes('>initializing<'), boot);
	// present but neither connected nor equipping
	const inserted = statusHtml({ connected: false, card_present: true });
	assert.ok(inserted.includes('>inserted, not initialized<'), inserted);
	// nothing in the reader
	const empty = statusHtml({});
	assert.ok(empty.includes('>not inserted<'), empty);
	// the old rows (and their keys) are gone
	for (const label of ['Card session:', 'Card present:']) {
		assert.ok(!empty.includes('>' + label + '<'), label + ' is gone');
	}
	assert.ok(!html.includes("'Card session:'"), 'the session key is gone');
	assert.ok(!html.includes("'Card present:'"), 'the presence key is gone');
	assert.ok(!html.includes("'n/a':"), 'the n/a key is gone');
	// the two new keys are translated
	assert.match(html, /'inserted, not initialized': 'вставлена, не инициализирована'/);
	assert.match(html, /'not inserted': 'не вставлена'/);
});

test('the status block renders a grid row per field, escaped', () => {
	const out = statusHtml({
		connected: true, equipping: false, card_present: true,
		atr: '3B9F&', iccid: null,
		card: { kind: 'sim', name: 'SIM <x>' },
		selection: { name: 'EF.A&B', kind: 'transparent', fid: '6F46' },
	});
	assert.ok(out.includes('SIM &lt;x&gt;'), out);
	assert.ok(out.includes('3B9F&amp;'), out);
	// an empty value hides its row (no n/a filler)
	assert.ok(!out.includes('>ICCID:<'), out);
	assert.ok(!out.includes('n/a'), out);
	assert.ok(out.includes('EF.A&amp;B [transparent] FID=6F46'), out);
	// only the fields that are set: an empty selection is "none"
	assert.ok(statusHtml({}).includes('none'), 'no selection -> none');
});

test('the ATR and ICCID rows exist only when the server reports them', () => {
	const full = statusHtml({ connected: true, card: { kind: 'uicc', name: 'UICC' },
		atr: '3B9F', iccid: '8970119000004600098' });
	assert.ok(full.includes('>ATR:<'), full);
	assert.ok(full.includes('>3B9F<'), full);
	assert.ok(full.includes('>ICCID:<'), full);
	assert.ok(full.includes('>8970119000004600098<'), full);
	const bare = statusHtml({ connected: true, card: { kind: 'uicc', name: 'UICC' } });
	assert.ok(!bare.includes('>ATR:<'), bare);
	assert.ok(!bare.includes('>ICCID:<'), bare);
	assert.ok(!bare.includes('>EID:<'), bare);
	// the EID stays conditional (an eUICC), the State row is always rendered
	assert.ok(statusHtml({ connected: true, card: { kind: 'euicc', name: 'eUICC' },
		eid: '8904903' }).includes('>EID:<'));
	assert.ok(bare.includes('>State:<'), bare);
	assert.ok(statusHtml({}).includes('>State:<'),
		'the State row is always rendered');
});

function fakeReaderDom(readers, active) {
	const mk = () => {
		const st = { hidden: false };
		st.classList = { toggle: (c, on) => { st.hidden = on; } };
		return st;
	};
	const sel = mk();
	sel.innerHTML = '';
	sel.value = active;
	sel.disabled = false;
	const hint = mk();
	const note = mk();
	const ids = {
		'pysim-reader-sel': sel,
		'pysim-reader-hint': hint,
		'pysim-reader-switch-note': note,
	};
	globalThis.document = { getElementById: id => ids[id] || null };
	return { sel, hint, note };
}

test('the reader block is always shown; a single reader disables the select', () => {
	// one reader: the select stays (the name is visible) but is disabled,
	// with the hint and without the switch note
	const one = fakeReaderDom(['ACS ACR39U'], 'ACS ACR39U');
	readerRender({ readers: ['ACS ACR39U'], reader: 'ACS ACR39U' });
	assert.strictEqual(one.sel.disabled, true);
	assert.strictEqual(one.hint.hidden, false, 'the single-reader hint shows');
	assert.strictEqual(one.note.hidden, true, 'no switching note with one reader');
	assert.ok(one.sel.innerHTML.includes('ACS ACR39U'));
	assert.strictEqual(one.sel.value, 'ACS ACR39U');

	// two readers: the select is enabled and the reset note shows
	const two = fakeReaderDom(['ACS ACR39U', 'Gemalto'], 'Gemalto');
	readerRender({ readers: ['ACS ACR39U', 'Gemalto'], reader: 'Gemalto' });
	assert.strictEqual(two.sel.disabled, false);
	assert.strictEqual(two.hint.hidden, true);
	assert.strictEqual(two.note.hidden, false, 'the switching note shows');
	assert.ok(two.sel.innerHTML.includes('ACS ACR39U'));
	assert.ok(two.sel.innerHTML.includes('Gemalto'));
	assert.strictEqual(two.sel.value, 'Gemalto');

	// no readers at all: still disabled with the hint (the block stays)
	const none = fakeReaderDom([], null);
	readerRender({ readers: [], reader: null });
	assert.strictEqual(none.sel.disabled, true);
	assert.strictEqual(none.hint.hidden, false);
	delete globalThis.document;
});

test('the Card reader layout: three legend fieldsets, no run-on labels', () => {
	const tab = /<div id="tab-pysim"[\s\S]*?<div id="tab-files"/.exec(html)[0];
	// the three blocks in order
	const legends = [...tab.matchAll(/<legend[^>]*data-l10n="([^"]+)"/g)]
		.map(m => m[1]);
	assert.deepStrictEqual(legends, ['Reader', 'Card', 'PIN / ADM'], tab);
	// the PIN / ADM legend follows the matched preset (id + base text)
	assert.ok(tab.includes('id="pysim-pin-legend"'));
	assert.ok(!tab.includes('PIN / ADM (matched preset)'), 'the static preset text is gone');
	for (const legend of legends) {
		assert.ok(tab.includes('<fieldset class="border border-gray-200 dark:border-slate-700 rounded p-3'));
	}
	// Card and PIN / ADM share one responsive row below the Reader block
	const rowStart = tab.indexOf('<div class="flex flex-col lg:flex-row gap-3 items-start">');
	assert.ok(rowStart >= 0, 'the Card + PIN/ADM row wrapper');
	const rowEnd = tab.indexOf('<div id="pysim-body"', rowStart);
	const row = tab.slice(rowStart, rowEnd);
	assert.ok(row.includes('data-l10n="Card"'));
	assert.ok(row.includes('id="pysim-pin-legend"'));
	assert.ok(row.includes('id="pysim-status"'));
	for (const id of ['pysim-pin1-state', 'pysim-pin2-state', 'pysim-adm-state']) {
		assert.ok(row.includes('id="' + id + '"'));
	}
	// both blocks flex in the row and can shrink (the long values wrap)
	assert.strictEqual((tab.match(/rounded p-3 flex-1 min-w-0/g) || []).length, 2);
	// the Reader block sits above the row
	assert.ok(tab.indexOf('data-l10n="Reader"') < tab.indexOf('lg:flex-row'));
	// the pills: PIN / ADM first (active, its panel shown), Raw APDU second
	const chvBtn = tab.indexOf('data-pysim-sub="chv"');
	const apduBtn = tab.indexOf('data-pysim-sub="apdu"');
	assert.ok(chvBtn > 0 && chvBtn < apduBtn, 'PIN / ADM before Raw APDU');
	const activeChv = tab.slice(chvBtn - 200, chvBtn);
	assert.ok(activeChv.includes('bg-blue-600'), 'the PIN / ADM pill is active');
	const activeApdu = tab.slice(apduBtn - 200, apduBtn);
	assert.ok(!activeApdu.includes('bg-blue-600'), 'Raw APDU starts inactive');
	// the panels follow the buttons and their default visibility matches
	const chvPanel = tab.indexOf('id="pysim-sub-chv"');
	const apduPanel = tab.indexOf('id="pysim-sub-apdu"');
	assert.ok(chvPanel > 0 && chvPanel < apduPanel, 'the CHV panel comes first');
	assert.ok(!/<div id="pysim-sub-chv" class="hidden"/.test(tab),
		'the PIN / ADM panel is visible by default');
	assert.ok(/<div id="pysim-sub-apdu" class="hidden"/.test(tab),
		'the Raw APDU panel is hidden by default');
	// the action buttons live in the Reader block, below the select
	assert.ok(tab.indexOf('id="pysim-reader-sel"') < tab.indexOf('pysimEquipCmd()'));
	assert.ok(tab.indexOf('pysimResetCmd()') < tab.indexOf('data-l10n="Card"'));
	// the reader block keeps its hint and switch note
	assert.ok(tab.includes('id="pysim-reader-hint"'));
	assert.ok(tab.includes('id="pysim-reader-switch-note"'));
	// the PIN/ADM rows are inside the legend-ed fieldset, with the message line
	const pinBlock = /<legend[^>]*data-l10n="PIN \/ ADM"[\s\S]*?<\/fieldset>/.exec(tab)[0];
	for (const id of ['pysim-pin1-state', 'pysim-pin2-state', 'pysim-adm-state',
		'pysim-pin-msg']) {
		assert.ok(pinBlock.includes('id="' + id + '"'), id + ' inside the PIN/ADM block');
	}
	// the status grid container sits in the Card block
	assert.ok(/<legend[^>]*data-l10n="Card"[\s\S]*?id="pysim-status"[\s\S]*?<\/fieldset>/.test(tab));
});

test('switching the reader asks first; a cancel restores the select', async () => {
	const { row, sel } = fakeReaderDom(['ACS ACR39U', 'Gemalto'], 'ACS ACR39U');
	const posts = [];
	const confirmations = [];
	globalThis.pysimConfirm = async msg => { confirmations.push(msg); return false; };
	globalThis.pysimAlert = msg => { throw new Error('unexpected alert: ' + msg); };
	globalThis.pysimFetch = async (p, body) => { posts.push([p, body]); return {}; };
	globalThis.pysimSetConnected = () => {};
	globalThis.pysimCardStateUpdate = () => {};
	globalThis.pysimRefresh = async () => {};

	readerRender({ readers: ['ACS ACR39U', 'Gemalto'], reader: 'ACS ACR39U' });
	sel.value = 'Gemalto';                    // the user picked the other one
	await readerChanged('Gemalto');
	assert.strictEqual(posts.length, 0, 'a cancelled switch posts nothing');
	assert.strictEqual(sel.value, 'ACS ACR39U', 'the select shows the active reader again');
	assert.strictEqual(confirmations.length, 1);
	assert.ok(confirmations[0].includes('reset'), confirmations[0]);

	delete globalThis.document;
});

test('a confirmed switch posts /api/reader and warns on failure', async () => {
	fakeReaderDom(['ACS ACR39U', 'Gemalto'], 'ACS ACR39U');
	const posts = [];
	const alerts = [];
	globalThis.pysimConfirm = async () => true;
	globalThis.pysimAlert = msg => alerts.push(msg);
	globalThis.pysimSetConnected = () => {};
	globalThis.pysimCardStateUpdate = () => {};
	globalThis.pysimRefresh = async () => {};
	globalThis.pysimFetch = async (p, body) => {
		posts.push([p, body]);
		return { error: 'reader switch failed: no card' };
	};

	readerRender({ readers: ['ACS ACR39U', 'Gemalto'], reader: 'ACS ACR39U' });
	await readerChanged('Gemalto');
	assert.deepStrictEqual(posts, [['/api/reader', { name: 'Gemalto' }]]);
	assert.strictEqual(alerts.length, 1);
	assert.ok(alerts[0].includes('no card'), alerts[0]);

	delete globalThis.document;
});

test('a confirmed switch feeds the status through the card state update and refresh', async () => {
	fakeReaderDom(['ACS ACR39U', 'Gemalto'], 'ACS ACR39U');
	const states = [];
	const updates = [];
	let refreshed = 0;
	globalThis.pysimConfirm = async () => true;
	globalThis.pysimAlert = msg => { throw new Error('unexpected alert: ' + msg); };
	globalThis.pysimSetConnected = state => states.push(state);
	globalThis.pysimCardStateUpdate = data => updates.push(data);
	globalThis.pysimRefresh = async () => { refreshed++; };
	const body = { connected: true, card_present: true, reader: 'Gemalto' };
	globalThis.pysimFetch = async () => body;

	readerRender({ readers: ['ACS ACR39U', 'Gemalto'], reader: 'ACS ACR39U' });
	await readerChanged('Gemalto');
	assert.deepStrictEqual(states, ['spin']);
	assert.deepStrictEqual(updates, [body]);
	assert.strictEqual(refreshed, 1);

	delete globalThis.document;
});
