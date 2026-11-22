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
code += 'globalThis.esc = s => s;\n';
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
	assert.ok(out.includes('ACS ACR39U'), out);
	assert.ok(out.includes('UICC'), out);
	assert.ok(out.includes('8970119000004600098'), out);
	assert.ok(out.includes('EF.SPN'), out);
	assert.ok(out.includes('MF/7F10/6F46'), out);
	assert.ok(out.includes('mode=polling'), out);
	assert.ok(out.includes('poll=on (30s)'), out);
	assert.ok(out.includes('test run=idle'), out);
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

test('the status block folds a name and kind that differ only by case', () => {
	const out = statusHtml({ card: { kind: 'uicc', name: 'UICC' } });
	assert.ok(out.includes('<b>UICC</b>'), out);
	assert.ok(!out.includes('(uicc)'), 'the kind is not repeated');
	const named = statusHtml({ card: { kind: 'uicc', name: 'USIM' } });
	assert.ok(named.includes('(uicc)'), named);
});

function fakeReaderDom(readers, active) {
	const row = { hidden: false, classList: { toggle: (c, on) => { row.hidden = on; } } };
	const sel = { innerHTML: '', value: active };
	const ids = {
		'pysim-reader-row': row,
		'pysim-reader-sel': sel,
	};
	globalThis.document = { getElementById: id => ids[id] || null };
	return { row, sel };
}

test('the reader selector shows only when more than one reader is present', () => {
	const one = fakeReaderDom(['ACS ACR39U'], 'ACS ACR39U');
	readerRender({ readers: ['ACS ACR39U'], reader: 'ACS ACR39U' });
	assert.strictEqual(one.row.hidden, true, 'one reader: no selector');
	assert.strictEqual(one.sel.value, 'ACS ACR39U');

	const two = fakeReaderDom(['ACS ACR39U', 'Gemalto'], 'Gemalto');
	readerRender({ readers: ['ACS ACR39U', 'Gemalto'], reader: 'Gemalto' });
	assert.strictEqual(two.row.hidden, false, 'two readers: selector shown');
	assert.ok(two.sel.innerHTML.includes('ACS ACR39U'));
	assert.ok(two.sel.innerHTML.includes('Gemalto'));
	assert.strictEqual(two.sel.value, 'Gemalto');
	delete globalThis.document;
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
