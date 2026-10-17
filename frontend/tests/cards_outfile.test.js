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

function extractConst(src, name) {
	const m = new RegExp('const\\s+' + name + '\\s*=\\s*([^;]+);').exec(src);
	if (!m) throw new Error('const ' + name + ' not found');
	return 'var ' + name + ' = ' + m[1] + ';\n';
}

const ASYNC_FNS = ['cardsOutfileFile', 'cardsOutfilePreview', 'cardsOutfileImport'];
let code = '';
for (const fn of ['cardsOutfileTarsFromDom', 'cardsOutfileTarsFromForm',
	'cardsOutfileTarRowHtml', 'cardsOutfileTarRowsHtml', 'cardsOutfileTarAdd',
	'cardsOutfileTarRemove', 'cardsOutfileTarRender', 'cardsOutfileKeysetOptions',
	'cardsOutfileResultHtml', 'cardsOutfileControls', 'cardsOutfileClose',
	'cardsOutfileRender', 'cardsOutfileFile', 'cardsOutfilePreview',
	'cardsOutfileImport', 'esc', 'escHtml', 'cardsRoleDefault', 'spKeysetKvnOf']) {
	code += extractFunc(html, fn, ASYNC_FNS.indexOf(fn) >= 0) + '\n';
}
code = extractConst(html, 'CARDS_TAR_DEFAULTS') + extractConst(html, 'CARDS_TAR_ROLES')
	+ extractConst(html, 'CARDS_MSL_DEFAULT') + extractConst(html, 'CARDS_TAR_ROLE_DESC')
	+ 'var _cardsOutfile = null;\nvar _cardsOutfileTars = 0;\n' + code;

// A minimal DOM: the tests set the elements they need, everything else is
// absent (the code guards every getElementById result).
const els = {};
const app = new Function('els', `
	var sandbox = { calls: [], io: [], alerts: [], fetched: 0, reply: null, replyError: null, confirmResult: true };
	var document = { getElementById: id => els[id] || null };
	var t = k => k;
	var alert = m => { sandbox.alerts.push(String(m)); };
	var confirm = m => { sandbox.confirmCalls = (sandbox.confirmCalls || []); sandbox.confirmCalls.push(String(m)); return sandbox.confirmResult; };
	var pysimAlert = m => alert(m);
	var pysimConfirm = async m => confirm(m);
	var pysimPrompt = async () => '';          // not used by the outfile flow
	var ioStatus = (id, msg) => { sandbox.io.push([id, msg]); };
	var pysimFetchOk = async (path, body) => {
		sandbox.calls.push({ path: path, body: body });
		if (sandbox.replyError) throw new Error(sandbox.replyError);
		return sandbox.reply;
	};
	var cardsFetch = async () => { sandbox.fetched++; };
	${code}
	sandbox.getState = () => _cardsOutfile;
	sandbox.setState = v => { _cardsOutfile = v; };
	sandbox.getTars = () => _cardsOutfileTars;
	sandbox.setTars = v => { _cardsOutfileTars = v; };
	Object.defineProperty(sandbox, 'state', { get: () => _cardsOutfile, set: v => { _cardsOutfile = v; } });
	Object.defineProperty(sandbox, 'tars', { get: () => _cardsOutfileTars, set: v => { _cardsOutfileTars = v; } });
	sandbox.cardsOutfileTarsFromDom = cardsOutfileTarsFromDom;
	sandbox.cardsOutfileTarsFromForm = cardsOutfileTarsFromForm;
	sandbox.cardsOutfileTarRowHtml = cardsOutfileTarRowHtml;
	sandbox.cardsOutfileTarRowsHtml = cardsOutfileTarRowsHtml;
	sandbox.cardsOutfileTarAdd = cardsOutfileTarAdd;
	sandbox.cardsOutfileTarRemove = cardsOutfileTarRemove;
	sandbox.cardsOutfileTarRender = cardsOutfileTarRender;
	sandbox.cardsOutfileKeysetOptions = cardsOutfileKeysetOptions;
	sandbox.cardsOutfileResultHtml = cardsOutfileResultHtml;
	sandbox.cardsOutfileControls = cardsOutfileControls;
	sandbox.cardsOutfileClose = cardsOutfileClose;
	sandbox.cardsOutfileRender = cardsOutfileRender;
	sandbox.cardsOutfileFile = cardsOutfileFile;
	sandbox.cardsOutfilePreview = cardsOutfilePreview;
	sandbox.cardsOutfileImport = cardsOutfileImport;
	return sandbox;
`)(els);
app.state = null;
app.tars = 0;

function setEl(id, value) {
	els[id] = { value: value, innerHTML: '', classList: { add() {}, remove() {} } };
}
function clearEls() {
	for (const k of Object.keys(els)) delete els[k];
}

const TEXT = 'var_out:IMSI ICCID KIC1 KID1\n250197700000000 8970119000004600098F ' +
	'00112233445566778899AABBCCDDEEFF FFEEDDCCBBAA99887766554433221100\n';

test('the Cards view wires the outfile button, the input and the panel', () => {
	assert.match(html, /onclick="cardsOutfilePick\(\)"[^>]*data-l10n="Import outfile"/);
	assert.match(html, /<input type="file" id="cards-outfile"[^>]*onchange="cardsOutfileFile\(this\)"/);
	assert.match(html, /<div id="cards-outfile-panel"/);
	assert.match(html, /'Import outfile': 'Импорт outfile'/);   // the term stays untranslated
});

test('the preview and the import both post the outfile endpoint', () => {
	const preview = extractFunc(html, 'cardsOutfilePreview', true);
	const imp = extractFunc(html, 'cardsOutfileImport', true);
	assert.match(preview, /'\/api\/presets\/import-outfile'/);
	assert.match(imp, /'\/api\/presets\/import-outfile'/);
	assert.match(imp, /mode: o\.mode/);
	// the keysets travel per TAR row (v3.29.1): no global tar_keyset
	assert.ok(!preview.includes('tar_keyset'), 'the preview must not send a global keyset');
	assert.ok(!imp.includes('tar_keyset'), 'the import must not send a global keyset');
	assert.match(preview, /tars: cardsOutfileTarsFromForm\(\)/);
	assert.match(imp, /tars: cardsOutfileTarsFromForm\(\), mode: o\.mode/);
});

test('the 16-byte algorithm values match the server map', () => {
	const py = fs.readFileSync(
		path.join(__dirname, '..', '..', 'pysim_simple_server', 'outfile.py'), 'utf8');
	const m = /_KEY_ALGO_16\s*=\s*\{([^}]*)\}/.exec(py);
	assert.ok(m, '_KEY_ALGO_16 not found in outfile.py');
	const server = [...m[1].matchAll(/'([a-z0-9]+)'\s*:/g)].map(x => x[1]).sort();
	const ui = [...html.matchAll(/<option value="(3des2|aes)"/g)].map(x => x[1]).sort();
	assert.deepStrictEqual(ui, server);
});

test('cardsOutfileTarsFromForm fills the role defaults and keeps the free rows', () => {
	clearEls();
	app.state = { text: TEXT, name: 'x', algorithm16: '3des2', tarKeyset: null, mode: 'merge', preview: null };
	app.tars = 0;
	const tars = app.cardsOutfileTarsFromForm();
	assert.deepStrictEqual(tars.map(e => e.role), ['isd', 'uiccRfm', 'usimRfm']);
	assert.deepStrictEqual(tars.map(e => e.tar), ['000000', 'B00000', 'B00001']);
	assert.deepStrictEqual(tars.map(e => e.msl), ['16', '16', '16']);
	// a typed role value wins, a typed free row is kept, an empty one dropped
	setEl('cards-of-tar-0-tar', 'af4d01');
	setEl('cards-of-tar-0-msl', '0a');
	app.tars = 2;
	setEl('cards-of-tar-3-tar', 'b00011');
	setEl('cards-of-tar-3-msl', '16');
	setEl('cards-of-tar-3-desc', 'push');
	const tars2 = app.cardsOutfileTarsFromForm();
	assert.strictEqual(tars2.length, 4);
	assert.deepStrictEqual([tars2[0].tar, tars2[0].msl], ['AF4D01', '0A']);
	assert.deepStrictEqual([tars2[3].tar, tars2[3].msl, tars2[3].desc], ['B00011', '16', 'push']);
	assert.strictEqual(tars2[3].role, undefined);
});

test('the TAR rows render the role ids and the free rows with a remove button', () => {
	clearEls();
	app.state = { text: TEXT, file: 'a.out', name: 'x', algorithm16: '3des2', tarKeyset: null, mode: 'merge', preview: null };
	app.tars = 0;
	const rows = app.cardsOutfileTarRowsHtml();
	assert.match(rows, /id="cards-of-tar-0-tar"[^>]*value="000000"/);
	assert.match(rows, /id="cards-of-tar-2-msl"[^>]*value="16"/);
	assert.match(rows, /ISD compact format/);
	assert.ok(!rows.includes('cards-of-tar-3-tar'));
	// a free row carries the description input and the remove action
	app.tars = 1;
	const rows2 = app.cardsOutfileTarRowsHtml();
	assert.match(rows2, /id="cards-of-tar-3-tar"/);
	assert.match(rows2, /id="cards-of-tar-3-desc"/);
	assert.match(rows2, /onclick="cardsOutfileTarRemove\(3\)"/);
	// every row has a keyset select, fed by the parsed keysets (v3.29.1)
	assert.match(rows2, /id="cards-of-tar-0-keyset"/);
	assert.match(rows2, /id="cards-of-tar-3-keyset"/);
	app.state.preview = { presets: [{ keysets: [{ kic: '15', kid: '15' }, { kic: '25', kid: '25' }] }] };
	const rows3 = app.cardsOutfileTarRowsHtml();
	assert.match(rows3, /<option value="1" selected>Keyset 1 — 15\/15<\/option>/);
	assert.match(rows3, /<option value="2">Keyset 2 — 25\/25<\/option>/);
	// a non-zero MSL without a keyset gets the amber marker
	app.state.preview = { presets: [] };
	const rows4 = app.cardsOutfileTarRowsHtml();
	assert.match(rows4, /This TAR needs a keyset/);
});

test('cardsOutfileKeysetOptions collects the parsed keysets with their bytes', () => {
	clearEls();
	app.state = {
		text: TEXT, name: 'x', algorithm16: '3des2', mode: 'merge',
		preview: { presets: [
			{ keysets: [{ kic: '25', kid: '25' }, { kic: '15', kid: '15' }] },
			{ keysets: [{ kic: '15', kid: '15' }, { kic: '31', kid: '31' }] },
		] },
	};
	assert.deepStrictEqual(app.cardsOutfileKeysetOptions(),
		[{ v: 1, kic: '15', kid: '15' }, { v: 2, kic: '25', kid: '25' },
			{ v: 3, kic: '31', kid: '31' }]);
	app.state.preview = { presets: [] };
	assert.deepStrictEqual(app.cardsOutfileKeysetOptions(), []);
});

test('cardsOutfileResultHtml renders the report and the parsed cards', () => {
	clearEls();
	app.state = {
		text: TEXT, name: 'x', algorithm16: '3des2', tarKeyset: null, mode: 'merge',
		preview: {
			presets: [{ iccid: '8970119000004600098', keysets: [{ kic: '15', kid: '15' }], adm: '62329F37' }],
			report: { columns: ['IMSI', 'ICCID', 'KIC1', 'KID1'], rows: 2, cards: 1,
				keysets: ['KIC1/KID1'], skipped: ['line 3: no ICCID'], skipped_more: 1 },
		},
	};
	const out = app.cardsOutfileResultHtml();
	assert.match(out, /Columns: <span class="font-mono">IMSI ICCID KIC1 KID1<\/span>/);
	assert.match(out, /Cards: 1 \/ rows read: 2/);
	assert.match(out, /Keysets: <span class="font-mono">KIC1\/KID1<\/span>/);
	assert.match(out, /line 3: no ICCID/);
	assert.match(out, /\(\+1\)/);
	assert.match(out, /8970119000004600098/);
	assert.match(out, /15\/15/);
	assert.match(out, /ADM/);
	// an empty preview says so, an error is shown
	app.state.preview = null;
	assert.match(app.cardsOutfileResultHtml(), /Parsing\.\.\./);
	app.state.preview = { report: { error: 'no column-name line found' } };
	assert.match(app.cardsOutfileResultHtml(), /no column-name line found/);
});

test('cardsOutfileControls reads the panel controls back into the state', () => {
	clearEls();
	app.state = { text: TEXT, name: 'old', algorithm16: '3des2', mode: 'merge', preview: null };
	setEl('cards-of-name', '  Batch 7  ');
	setEl('cards-of-algo', 'aes');
	setEl('cards-of-mode', 'replace');
	app.cardsOutfileControls();
	assert.strictEqual(app.state.name, 'Batch 7');
	assert.strictEqual(app.state.algorithm16, 'aes');
	assert.strictEqual(app.state.mode, 'replace');
});

test('picking a file previews it with the file name and the TAR defaults', async () => {
	clearEls();
	app.calls = [];
	app.reply = { preview: true, presets: [{ iccid: '8970119000004600098', keysets: [{ kic: '15', kid: '15' }] }],
		report: { columns: ['ICCID'], rows: 1, cards: 1 } };
	setEl('cards-outfile-panel', '');
	const input = { value: 'C:\\fakepath\\batch.out', files: [{ name: 'batch.out', text: async () => TEXT }] };
	await app.cardsOutfileFile(input);
	assert.strictEqual(input.value, '');
	assert.strictEqual(app.calls.length, 1);
	assert.strictEqual(app.calls[0].path, '/api/presets/import-outfile');
	const body = app.calls[0].body;
	assert.strictEqual(body.text, TEXT);
	assert.strictEqual(body.name, 'batch');           // the file base name
	assert.strictEqual(body.algorithm16, '3des2');
	assert.strictEqual(body.mode, undefined);          // the preview is a dry run
	assert.strictEqual(body.tar_keyset, undefined);    // the keysets are per row now
	assert.strictEqual(body.tars.length, 3);
	// the rows' keyset selects are filled from the parsed keysets (the first one
	// is the default)
	assert.match(els['cards-outfile-panel'].innerHTML, /id="cards-of-tar-0-keyset"/);
	assert.match(els['cards-outfile-panel'].innerHTML, /<option value="1" selected>Keyset 1 — 15\/15<\/option>/);
	assert.match(els['cards-outfile-panel'].innerHTML, /8970119000004600098/);
});

test('a failing preview shows the server message in the panel', async () => {
	clearEls();
	app.calls = [];
	app.replyError = 'TAR must be three hex bytes: ZZ';
	setEl('cards-outfile-panel', '');
	const input = { value: '', files: [{ name: 'bad.out', text: async () => TEXT }] };
	await app.cardsOutfileFile(input);
	app.replyError = null;
	assert.match(app.cardsOutfileResultHtml(), /TAR must be three hex bytes/);
});

test('the import posts the chosen mode and refreshes the list', async () => {
	clearEls();
	app.calls = [];
	app.fetched = 0;
	app.io = [];
	app.confirmResult = true;
	app.reply = { ok: true, preview: false, added: 2, skipped: 1, errors: ['dup'] };
	setEl('cards-outfile-panel', '');
	setEl('cards-of-mode', 'replace');
	setEl('cards-of-name', 'Batch 7');
	app.state = { text: TEXT, file: 'batch.out', name: 'Batch 7', algorithm16: '3des2',
		mode: 'merge', preview: { presets: [], report: {} } };
	setEl('cards-of-tar-0-keyset', '2');               // the ISD row's chosen keyset
	await app.cardsOutfileImport();
	assert.strictEqual(app.calls.length, 1);
	assert.strictEqual(app.calls[0].body.mode, 'replace');
	assert.strictEqual(app.calls[0].body.tar_keyset, undefined);   // per row, not global
	assert.strictEqual(app.calls[0].body.tars[0].keyset, 2);
	assert.strictEqual(app.calls[0].body.name, 'Batch 7');
	assert.strictEqual(app.fetched, 1);
	assert.deepStrictEqual(app.io, [['cards-io', 'Imported 2 cards (1 skipped).']]);
	assert.strictEqual(app.state, null);               // the panel closed
	// the replace mode asks first, and a declined answer imports nothing
	assert.deepStrictEqual(app.confirmCalls, ['Replace mode wipes the whole preset store. Continue?']);
});

test('a declined replace confirmation aborts the import', async () => {
	clearEls();
	app.calls = [];
	app.confirmResult = false;
	setEl('cards-of-mode', 'replace');
	app.state = { text: TEXT, file: 'batch.out', name: 'x', algorithm16: '3des2',
		tarKeyset: null, mode: 'merge', preview: { presets: [], report: {} } };
	await app.cardsOutfileImport();
	app.confirmResult = true;
	assert.strictEqual(app.calls.length, 0);
	assert.ok(app.state);                              // the panel stays open
	// merge never asks
	app.confirmResult = false;
	setEl('cards-of-mode', 'merge');
	app.reply = { ok: true, preview: false, added: 1, skipped: 0, errors: [] };
	await app.cardsOutfileImport();
	app.confirmResult = true;
	assert.strictEqual(app.calls.length, 1);
});

test('the Import button is disabled while the report has a file error (v3.29.2)', () => {
	clearEls();
	setEl('cards-outfile-panel', '');
	app.state = { text: TEXT, file: 'bad.out', name: 'x', algorithm16: '3des2', mode: 'merge',
		preview: { presets: [], report: { error: 'line 2 has 2 values, the header declares 3 columns - the file is invalid' } } };
	app.tars = 0;
	app.cardsOutfileRender();
	assert.match(els['cards-outfile-panel'].innerHTML, /id="cards-of-import" disabled/);
	assert.match(els['cards-outfile-panel'].innerHTML, /the header declares 3 columns/);
	// a valid report keeps it enabled
	app.state.preview = { presets: [{ iccid: '8970119000004600098', keysets: [{ kic: '15', kid: '15' }] }],
		report: { columns: ['ICCID', 'KIC1', 'KID1'], rows: 1, cards: 1 } };
	app.cardsOutfileRender();
	assert.match(els['cards-outfile-panel'].innerHTML, /id="cards-of-import" onclick/);
});

test('a row keeps its chosen keyset through the DOM round-trip', () => {
	clearEls();
	app.state = { text: TEXT, name: 'x', algorithm16: '3des2', mode: 'merge',
		preview: { presets: [{ keysets: [{ kic: '15', kid: '15' }, { kic: '25', kid: '25' }] }] } };
	app.tars = 0;
	setEl('cards-of-tar-0-keyset', '2');          // the user picked keyset 2 for the ISD row
	setEl('cards-of-tar-1-keyset', '');
	const tars = app.cardsOutfileTarsFromForm();
	assert.strictEqual(tars[0].keyset, 2);
	assert.strictEqual(tars[1].keyset, null);
	// a free row carries its own keyset too
	app.tars = 1;
	setEl('cards-of-tar-3-tar', 'b00011');
	setEl('cards-of-tar-3-msl', '1a');
	setEl('cards-of-tar-3-keyset', '1');
	const tars2 = app.cardsOutfileTarsFromForm();
	assert.strictEqual(tars2.length, 4);
	assert.strictEqual(tars2[3].keyset, 1);
});
