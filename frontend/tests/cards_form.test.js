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

let code = '';
const ASYNC_FNS = ['cardsAdd', 'cardsImport', 'cardsRemove'];
for (const fn of ['cardsFormValues', 'cardsClearForm', 'cardsEdit',
	'cardsAdd', 'cardsImport', 'cardsRemove', 'ramCardIdxAfterRemove',
	'spPresetIdx', 'spPresetCounterValue', 'spCntrPlaceholder', 'spCntrEffective',
	'spCntrAhead', 'spCntrLocalSync', 'ramSaveCntr',
	'cardsApplyFields', 'cardsApply', 'ramApplyCard',
	'cardsScp80Complete', 'cardsScp81Complete', 'cardsAdmPresent',
	'spTarKeyForPack', 'spPresetTar', 'spTarListRender', 'packToSp',
	'esc', 'escHtml',
	'spKeysetKvnOf', 'spKeysetList', 'spKeysetFor', 'spKeysetCheck',
	'spKeysetOptionsHtml', 'cardsKeysetRowHtml', 'cardsKeysetRowsRender',
	'cardsKeysetAdd', 'cardsKeysetRemove', 'cardsKeysetKvnUpdate',
	'cardsKeysetsFromForm', 'spKeysetSync', 'spKeysetApply', 'spKeysetChanged',
	'ramKeysetChanged', 'ramPresetIdx', 'tarPresetIdx', 'spKeysetGuard', 'ramKeysetGuard',
	'cardsTarList', 'cardsRoleDefault', 'cardsRoleTar',
	'cardsTarEntry', 'cardsTarMsl', 'cardsTarText', 'spMslWarningText',
	'cardsTarRowHtml', 'cardsTarRowsRender', 'cardsTarAdd', 'cardsTarRemove',
	'cardsFreeTarsFromForm', 'cardsTarsFromTable', 'cardsTarsFromForm', 'cardsTarsReset',
	'cardsShowList', 'cardsShowEditor', 'cardsSetFormMode', 'cardsNew',
	'cardsCancelEdit']) {
	code += extractFunc(html, fn, ASYNC_FNS.indexOf(fn) >= 0) + '\n';
}
code = extractConst(html, 'CARDS_TAR_DEFAULTS') + extractConst(html, 'CARDS_TAR_ROLES')
	+ extractConst(html, 'CARDS_MSL_DEFAULT') + extractConst(html, 'CARDS_TAR_ROLE_DESC')
	+ 'var _cardsKeysetCount = 0;\nvar _cardsTarCount = 0;\n'
	+ 'globalThis.spPresetWarningRender = function() {};\n'
	+ 'globalThis.spMslWarningRender = function() {};\n'
	+ 'globalThis.pysimApplyAvailability = function() {};\n' + code;
eval(code);
globalThis.t = s => s;

const CARD_IDS = ['cards-name','cards-iccid','cards-release','cards-pin1','cards-puk1','cards-pin2','cards-puk2','cards-adm',
	'cards-tars','cards-list-view','cards-editor-view','cards-editor-title','cards-new-btn',
	'cards-psk-id','cards-psk-key','cards-keysets',
	'cards-add-btn','cards-cancel-btn',
	// the keyset editor rows (two rows is enough for the tests)
	'cards-ks-0-kvn','cards-ks-0-kic','cards-ks-0-kid','cards-ks-0-kic-key',
	'cards-ks-0-kid-key','cards-ks-0-cntr',
	'cards-ks-1-kvn','cards-ks-1-kic','cards-ks-1-kid','cards-ks-1-kic-key',
	'cards-ks-1-kid-key','cards-ks-1-cntr',
	// the TAR table rows: 0-2 are the fixed role rows, 3+ the free rows
	'cards-tr-0-tar','cards-tr-0-msl','cards-tr-1-tar','cards-tr-1-msl',
	'cards-tr-2-tar','cards-tr-2-msl',
	'cards-tr-3-tar','cards-tr-3-msl','cards-tr-3-desc',
	'cards-tr-4-tar','cards-tr-4-msl','cards-tr-4-desc'];
const SP_IDS = ['sp-card-sel','sp-tar','sp-spi1','sp-spi2','sp-spi2-sm','sp-spi2-hex',
	'sp-kic-idx','sp-kic-alg','sp-kic-hex','sp-kid-idx','sp-kid-alg','sp-kid-hex',
	'sp-cntr','sp-kic-key','sp-kid-key','sp-apdu','sp-result','sp-spi1-hex','sp-tar-list',
	'sp-keyset-sel','ram-keyset-sel'];
const CHAIN_IDS = ['chain-sim-preview','chain-usim-preview','chain-ram-preview',
	'ber-result','hota-preview'];

function ksRow(els, i, ks) {
	for (const field of ['kic', 'kid', 'kic-key', 'kid-key', 'cntr']) {
		const id = 'cards-ks-' + i + '-' + field;
		if (!els[id]) els[id] = fakeEl();
		els[id].value = ks[field === 'kic-key' ? 'kicKey' : (field === 'kid-key' ? 'kidKey' : field)] || '';
	}
}

function tarRow(els, i, e) {
	for (const field of ['tar', 'msl', 'desc']) {
		const id = 'cards-tr-' + i + '-' + field;
		if (!els[id]) els[id] = fakeEl();
		els[id].value = e[field] || '';
	}
}

function presetFixture(extra) {
	return Object.assign({
		name: 'C',
		keysets: [{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }],
		tars: [{ role: 'isd', tar: '000000', msl: '16' },
			{ role: 'uiccRfm', tar: 'B00000', msl: '16' },
			{ role: 'usimRfm', tar: 'B00001', msl: '16' }],
	}, extra || {});
}

function fakeEl() {
	const classes = new Set();
	return {
		value: '',
		textContent: '',
		innerHTML: '',
		focus() {},
		classList: {
			add: (...cs) => cs.forEach(c => classes.add(c)),
			remove: (...cs) => cs.forEach(c => classes.delete(c)),
			toggle: (c, on) => {
				const want = on === undefined ? !classes.has(c) : !!on;
				if (want) classes.add(c); else classes.delete(c);
				return want;
			},
			contains: c => classes.has(c),
		},
		setAttribute() {}, removeAttribute() {},
	};
}

let apiCalls = [];

function setup() {
	const els = {};
	for (const id of CARD_IDS.concat(SP_IDS, CHAIN_IDS)) els[id] = fakeEl();
	globalThis.document = { getElementById: id => els[id] || null, querySelectorAll: () => [] };
	globalThis.cards = [];
	globalThis._cardsEditIdx = null;
	globalThis._ramCardIdx = null;
	globalThis._cardsKeysetCount = 1;
	_cardsTarCount = 0;
	globalThis._spTarKey = 'isd';
	globalThis.t = s => s;
	globalThis.esc = s => s;
	globalThis.escHtml = s => s;
	globalThis.alert = () => {};
	globalThis.cardsSave = () => {};
	globalThis.cardsRender = () => {};
	globalThis.cardsRebuildSelect = () => {};
	globalThis.scp81PushPskMap = () => {};
	globalThis.cardsFindDuplicateIccid = () => -1;
	globalThis.ioStatus = () => {};
	globalThis.switchTab = () => {};
	globalThis.scp80SwitchSubtab = () => {};
	globalThis.updateSpKic = () => {};
	globalThis.updateSpKid = () => {};
	globalThis.spInvalidate = () => {};
	globalThis.updateSp = () => {};
	globalThis.genSp = () => {};
	globalThis._genSpBuild = () => {};
	globalThis.spShowSizeInfo = () => {};
	globalThis.ramRender = () => {};
	globalThis.switchTab = () => {};
	// the server store is the source of truth (v3.8.0): the Cards tab talks to
	// /api/presets* and reloads through cardsFetch()
	apiCalls = [];
	globalThis.pysimFetchOk = async (path, body) => {
		apiCalls.push({ path: path, body: body });
		return { ok: true, added: 1, skipped: 0 };
	};
	globalThis.pysimFetch = async (path, body) => {
		apiCalls.push({ path: path, body: body });
		return { ok: true };
	};
	globalThis.cardsFetch = async () => true;
	return els;
}

test('the preset TAR table helpers read the role entries and the MSLs', () => {
	const p = presetFixture({ tars: [
		{ role: 'isd', tar: 'AF4D01', msl: '1A' },
		{ role: 'uiccRfm', tar: 'B00000', msl: '10' },
		{ role: 'usimRfm', tar: 'B00001', msl: '16' },
		{ tar: 'AF4D02', msl: '0A', desc: 'applet' }] });
	assert.strictEqual(cardsRoleTar(p, 'isd'), 'AF4D01');
	assert.strictEqual(cardsTarMsl(p, 'af4d02'), '0A');
	assert.strictEqual(cardsTarMsl(p, '123456'), '');
	assert.strictEqual(cardsTarEntry(p, 'AF4D02').desc, 'applet');
	// a missing role entry falls back to the spec default
	assert.strictEqual(cardsRoleTar({}, 'uiccRfm'), 'B00000');
	assert.strictEqual(cardsRoleTar(null, 'usimRfm'), 'B00001');
	assert.strictEqual(cardsRoleDefault('nope'), '');
	assert.strictEqual(cardsTarText(p),
		'ISD AF4D01\u00b71A UICC B00000\u00b710 ADF B00001\u00b716 AF4D02\u00b70A (applet)');
	assert.strictEqual(cardsTarText({}), '\u2014');
});

test('spMslWarningText compares the SPI1 with the TAR MSL numerically', () => {
	const p = presetFixture({ tars: [
		{ role: 'isd', tar: '000000', msl: '10' },
		{ role: 'uiccRfm', tar: 'B00000', msl: '16' },
		{ role: 'usimRfm', tar: 'B00001', msl: '16' }] });
	assert.strictEqual(spMslWarningText(p, '000000', '10'), '');
	assert.strictEqual(spMslWarningText(p, '000000', '1A'), '');
	assert.match(spMslWarningText(p, '000000', '0A'), /MSL/);
	// numeric, not lexicographic: '9' < '10'
	assert.match(spMslWarningText(p, '000000', '9'), /MSL/);
	// a TAR the preset does not carry (or a missing SPI1) is not a warning
	assert.strictEqual(spMslWarningText(p, 'AF4D01', '00'), '');
	assert.strictEqual(spMslWarningText(p, '000000', ''), '');
});

test('preset completeness predicates used by the header markers', () => {
	const full = presetFixture({ pskIdentity: 'id', pskKey: 'KEY', adm: '0011' });
	assert.ok(cardsScp80Complete(full));
	assert.ok(cardsScp81Complete(full));
	assert.ok(cardsAdmPresent(full));
	// a complete keyset is required
	assert.ok(!cardsScp80Complete(Object.assign({}, full, { keysets: [] })));
	assert.ok(!cardsScp80Complete(Object.assign({}, full, {
		keysets: [{ kic: '15', kid: '15', kicKey: '', kidKey: 'BB', cntr: '0000000001' }] })));
	// every role entry needs its TAR and MSL
	for (const role of ['isd', 'uiccRfm', 'usimRfm']) {
		const noMsl = presetFixture({ tars: full.tars.map(e =>
			e.role === role ? Object.assign({}, e, { msl: '' }) : e) });
		assert.ok(!cardsScp80Complete(noMsl), 'SCP80 complete with an empty MSL for ' + role);
		const noTar = presetFixture({ tars: full.tars.map(e =>
			e.role === role ? Object.assign({}, e, { tar: '' }) : e) });
		assert.ok(!cardsScp80Complete(noTar), 'SCP80 complete with an empty TAR for ' + role);
	}
	assert.ok(!cardsScp81Complete({ pskIdentity: 'id' }));
	assert.ok(!cardsScp81Complete({ pskKey: 'KEY' }));
	assert.ok(!cardsScp80Complete(null));
	assert.ok(!cardsScp81Complete(null));
	assert.ok(!cardsAdmPresent(null));
	assert.ok(!cardsAdmPresent({ adm: ' ' }));
});

test('clearing the card form keeps the role TAR/MSL defaults', () => {
	const els = setup();
	els['cards-pin1'].value = '1234';
	els['cards-adm'].value = 'DEAD';
	els['cards-name'].value = 'X';
	// a free row from a previous edit
	tarRow(els, 3, { tar: 'AF4D01', msl: '0A', desc: 'applet' });
	_cardsTarCount = 1;
	cardsClearForm();
	assert.strictEqual(els['cards-pin1'].value, '');
	assert.strictEqual(els['cards-adm'].value, '');
	assert.strictEqual(els['cards-name'].value, '');
	assert.strictEqual(_cardsTarCount, 0, 'the free TAR rows are cleared');
	// the role rows render the pre-settled defaults
	assert.match(els['cards-tars'].innerHTML, /cards-tr-0-tar"[^>]*value="000000"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-0-msl"[^>]*value="16"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-1-tar"[^>]*value="B00000"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-2-tar"[^>]*value="B00001"/);
	// ... and the table reads back as the three role entries
	assert.deepStrictEqual(cardsTarsFromForm(), [
		{ role: 'isd', tar: '000000', msl: '16', desc: '' },
		{ role: 'uiccRfm', tar: 'B00000', msl: '16', desc: '' },
		{ role: 'usimRfm', tar: 'B00001', msl: '16', desc: '' },
	]);
});

test('editing prefills the code fields and renders the TAR table', () => {
	const els = setup();
	globalThis.cards = [
		{ name: 'Legacy', keysets: [] },
		presetFixture({ name: 'Full', adm: '0011', pin1: '1234', puk1: '12345678',
			pin2: '0000', puk2: '87654321', tars: [
			{ role: 'isd', tar: '000001', msl: '0A' },
			{ role: 'uiccRfm', tar: 'B0000C', msl: '16' },
			{ role: 'usimRfm', tar: 'B0000D', msl: '16' },
			{ tar: 'AF4D02', msl: '1A', desc: 'applet' }] }),
	];
	cardsEdit(0);
	// the editor opens; missing codes read as empty
	assert.ok(!els['cards-editor-view'].classList.contains('hidden'));
	assert.ok(els['cards-list-view'].classList.contains('hidden'));
	assert.strictEqual(els['cards-pin1'].value, '');
	assert.strictEqual(els['cards-adm'].value, '');
	cardsEdit(1);
	assert.strictEqual(els['cards-pin1'].value, '1234');
	assert.strictEqual(els['cards-puk1'].value, '12345678');
	assert.strictEqual(els['cards-pin2'].value, '0000');
	assert.strictEqual(els['cards-puk2'].value, '87654321');
	assert.strictEqual(els['cards-adm'].value, '0011');
	assert.strictEqual(_cardsTarCount, 1);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-0-tar"[^>]*value="000001"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-1-tar"[^>]*value="B0000C"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-2-tar"[^>]*value="B0000D"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-3-tar"[^>]*value="AF4D02"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-3-desc"[^>]*value="applet"/);
	// the role rows carry the fixed descriptions and no remove button
	assert.match(els['cards-tars'].innerHTML, /ISD compact format/);
	assert.match(els['cards-tars'].innerHTML, /UICC shared FS RFM compact format/);
	assert.match(els['cards-tars'].innerHTML, /ADF FS RFM compact format/);
	assert.doesNotMatch(els['cards-tars'].innerHTML, /cardsTarRemove\(0\)/);
	assert.match(els['cards-tars'].innerHTML, /cardsTarRemove\(3\)/);
});

test('the form values carry the codes and the TAR table', () => {
	const els = setup();
	els['cards-pin1'].value = ' 12 34 ';
	els['cards-puk1'].value = '12345678';
	els['cards-adm'].value = ' 00 11 22 33 ';
	tarRow(els, 0, { tar: ' 000001 ', msl: '0a' });
	tarRow(els, 1, { tar: '', msl: '' });          // uiccRfm -> spec default
	tarRow(els, 2, { tar: 'B00003', msl: '1A' });
	tarRow(els, 3, { tar: 'af4d02', msl: '0A', desc: 'My applet' });
	_cardsTarCount = 1;
	const v = cardsFormValues();
	assert.deepStrictEqual(v.tars, [
		{ role: 'isd', tar: '000001', msl: '0A', desc: '' },
		{ role: 'uiccRfm', tar: 'B00000', msl: '16', desc: '' },
		{ role: 'usimRfm', tar: 'B00003', msl: '1A', desc: '' },
		{ tar: 'AF4D02', msl: '0A', desc: 'My applet' },
	]);
	assert.strictEqual(v.pin1, '1234');
	assert.strictEqual(v.puk1, '12345678');
	assert.strictEqual(v.adm, '00112233');
});

test('the release field is collected, cleared and restored', () => {
	const els = setup();
	els['cards-release'].value = '14';
	assert.strictEqual(cardsFormValues().release, '14');
	cardsClearForm();
	assert.strictEqual(els['cards-release'].value, '');
	globalThis.cards = [presetFixture({ name: 'Rel', release: '17' })];
	cardsEdit(0);
	assert.strictEqual(els['cards-release'].value, '17');
	// a preset without a release reads as unspecified
	globalThis.cards = [presetFixture({ name: 'Plain' })];
	cardsEdit(0);
	assert.strictEqual(els['cards-release'].value, '');
});

test('saving a preset posts the form with the pre-settled TAR table', async () => {
	const els = setup();
	els['cards-name'].value = 'New card';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' });
	globalThis._cardsKeysetCount = 1;
	// the role fields are pre-settled by the markup; the test's fake elements
	// start empty - the defaults fill in
	els['cards-adm'].value = 'aa bb';
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 1, JSON.stringify(apiCalls));
	assert.strictEqual(apiCalls[0].path, '/api/presets');
	assert.strictEqual(apiCalls[0].body.name, 'New card');
	assert.deepStrictEqual(apiCalls[0].body.keysets, [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }]);
	assert.deepStrictEqual(apiCalls[0].body.tars, [
		{ role: 'isd', tar: '000000', msl: '16', desc: '' },
		{ role: 'uiccRfm', tar: 'B00000', msl: '16', desc: '' },
		{ role: 'usimRfm', tar: 'B00001', msl: '16', desc: '' },
	]);
	assert.strictEqual(apiCalls[0].body.adm, 'AABB');
});

test('saving refuses a free TAR row without its MSL or with a duplicate TAR', async () => {
	const els = setup();
	els['cards-name'].value = 'New card';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' });
	globalThis._cardsKeysetCount = 1;
	const seen = [];
	globalThis.alert = msg => seen.push(msg);
	tarRow(els, 3, { tar: 'AF4D02', msl: '', desc: '' });
	_cardsTarCount = 1;
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 0);
	assert.match(seen[0], /MSL/);
	// a TAR may appear once
	seen.length = 0;
	tarRow(els, 3, { tar: '000000', msl: '16', desc: '' });
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 0);
	assert.match(seen[0], /Duplicate TAR/);
});

test('editing a preset updates it by its store id', async () => {
	const els = setup();
	globalThis.cards = [presetFixture({ id: 'abc123', name: 'Old' })];
	globalThis._cardsEditIdx = 0;
	els['cards-name'].value = 'Renamed';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' });
	globalThis._cardsKeysetCount = 1;
	await cardsAdd();
	assert.strictEqual(apiCalls[0].path, '/api/presets/update');
	assert.strictEqual(apiCalls[0].body.id, 'abc123');
	assert.strictEqual(apiCalls[0].body.fields.name, 'Renamed');
	assert.strictEqual(apiCalls[0].body.fields.tars.length, 3);
});

test('removing a preset deletes it in the store', async () => {
	setup();
	globalThis.cards = [{ id: 'abc123', name: 'A' }, { id: 'def456', name: 'B' }];
	await cardsRemove(0);
	assert.strictEqual(apiCalls.length, 1);
	assert.strictEqual(apiCalls[0].path, '/api/presets/delete');
	assert.strictEqual(apiCalls[0].body.id, 'abc123');
});

test('import posts the entries to the server store', async () => {
	setup();
	await cardsImport(JSON.stringify([
		{ name: 'Imported', kic: '15', kid: '15', adm: ' aa bb ' },
		{ name: 'Explicit', tar: '000009' },
	]));
	assert.strictEqual(apiCalls.length, 1);
	assert.strictEqual(apiCalls[0].path, '/api/presets/import');
	assert.strictEqual(apiCalls[0].body.presets.length, 2);
	assert.strictEqual(apiCalls[0].body.presets[0].name, 'Imported');
	// a payload that is not an array never reaches the server
	apiCalls = [];
	await cardsImport('{"nope": 1}');
	assert.strictEqual(apiCalls.length, 0);
});

test('the SCP80 counter field is an override - an edit is never written to the store', () => {
	const els = setup();
	globalThis.cards = [{ id: 'abc123', name: 'C', keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }] }];
	els['sp-card-sel'].value = '0';
	els['sp-kic-hex'].value = '15';
	els['sp-kid-hex'].value = '15';
	els['sp-cntr'].value = '0000000009';           // an explicit override
	// typing alone writes nothing (no spCntrSyncPreset anymore, v3.20.0)
	assert.strictEqual(apiCalls.length, 0);
	// a counter the server persisted: the local copy moves forward, display only
	spCntrLocalSync('000000000A');
	assert.strictEqual(cards[0].keysets[0].cntr, '000000000A');
	assert.strictEqual(apiCalls.length, 0);
	// and the copy never regresses (the store is monotonic)
	spCntrLocalSync('0000000005');
	assert.strictEqual(cards[0].keysets[0].cntr, '000000000A');
	// the override in the field is untouched by the copy sync
	assert.strictEqual(els['sp-cntr'].value, '0000000009');
});

test('applying a preset uses the TAR of the current operation and seeds the SPI1 from its MSL', () => {
	const els = setup();
	globalThis.cards = [presetFixture({ tars: [
		{ role: 'isd', tar: '000000', msl: '16' },
		{ role: 'uiccRfm', tar: 'B0000C', msl: '0A' },
		{ role: 'usimRfm', tar: 'B0000D', msl: '1A' }] })];
	globalThis._spTarKey = 'isd';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, '000000');
	assert.strictEqual(els['sp-spi1'].value, '16');
	globalThis._spTarKey = 'uiccRfm';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, 'B0000C');
	assert.strictEqual(els['sp-spi1'].value, '0A');
	globalThis._spTarKey = 'usimRfm';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, 'B0000D');
	assert.strictEqual(els['sp-spi1'].value, '1A');
	// an empty stored TAR -> spec default
	globalThis.cards = [{ name: 'D', keysets: [], tars: [] }];
	globalThis._spTarKey = 'isd';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, '000000');
	assert.strictEqual(els['sp-spi1'].value, '16');
});

test('RAM card selection forces the ISD TAR', () => {
	const els = setup();
	globalThis.cards = [presetFixture()];
	globalThis._spTarKey = 'usimRfm';
	ramApplyCard('0');
	assert.strictEqual(globalThis._spTarKey, 'isd');
	assert.strictEqual(els['sp-tar'].value, '000000');
});

test('spTarKeyForPack maps each chain to its preset TAR role', () => {
	assert.strictEqual(spTarKeyForPack('chain-ram-preview'), 'isd');
	assert.strictEqual(spTarKeyForPack('chain-sim-preview'), 'uiccRfm');
	assert.strictEqual(spTarKeyForPack('chain-usim-preview'), 'usimRfm');
	assert.strictEqual(spTarKeyForPack('ber-result'), null);
	assert.strictEqual(spTarKeyForPack('hota-preview'), null);
});

test('spPresetTar reads the selected preset and falls back to the spec defaults', () => {
	const els = setup();
	globalThis.cards = [presetFixture({ tars: [
		{ role: 'isd', tar: '000000', msl: '16' },
		{ role: 'uiccRfm', tar: 'B0000A', msl: '16' }] })];
	els['sp-card-sel'].value = '0';
	assert.strictEqual(spPresetTar('uiccRfm'), 'B0000A');
	assert.strictEqual(spPresetTar('usimRfm'), 'B00001');
	els['sp-card-sel'].value = '';
	assert.strictEqual(spPresetTar('uiccRfm'), 'B00000');
});

test('the TAR field suggestions come from the preset table', () => {
	const els = setup();
	spTarListRender(presetFixture({ tars: [
		{ role: 'isd', tar: '000000', msl: '16' },
		{ tar: 'AF4D02', msl: '0A', desc: 'My applet' }] }));
	assert.match(els['sp-tar-list'].innerHTML, /value="000000"[^>]*label="ISD \(MSL 16\)"/);
	assert.match(els['sp-tar-list'].innerHTML, /value="AF4D02"[^>]*label="My applet \(MSL 0A\)"/);
	spTarListRender(null);
	assert.strictEqual(els['sp-tar-list'].innerHTML, '');
});

test('packing a chain selects the TAR of its target', () => {
	const els = setup();
	globalThis.cards = [presetFixture({ tars: [
		{ role: 'isd', tar: '000000', msl: '16' },
		{ role: 'uiccRfm', tar: 'B0000C', msl: '16' },
		{ role: 'usimRfm', tar: 'B0000D', msl: '16' }] })];
	els['sp-card-sel'].value = '0';
	els['chain-sim-preview'].value = 'A0A4000002';
	packToSp('chain-sim-preview');
	assert.strictEqual(globalThis._spTarKey, 'uiccRfm');
	assert.strictEqual(els['sp-tar'].value, 'B0000C');
	assert.strictEqual(els['sp-apdu'].value, 'A0A4000002');
	els['chain-usim-preview'].value = '00A4000002';
	packToSp('chain-usim-preview');
	assert.strictEqual(globalThis._spTarKey, 'usimRfm');
	assert.strictEqual(els['sp-tar'].value, 'B0000D');
	els['chain-ram-preview'].value = '80E29000';
	packToSp('chain-ram-preview');
	assert.strictEqual(globalThis._spTarKey, 'isd');
	assert.strictEqual(els['sp-tar'].value, '000000');
	// BER / HTTP OTA packs leave the TAR alone
	els['sp-tar'].value = 'ABCDEF';
	els['ber-result'].value = 'AA0100';
	packToSp('ber-result');
	assert.strictEqual(els['sp-tar'].value, 'ABCDEF');
	assert.strictEqual(els['sp-apdu'].value, 'AA0100');
});

test('packing without a selected preset uses the spec default TAR', () => {
	const els = setup();
	els['chain-sim-preview'].value = 'A0A4000002';
	packToSp('chain-sim-preview');
	assert.strictEqual(els['sp-tar'].value, 'B00000');
	els['chain-usim-preview'].value = '00A4000002';
	packToSp('chain-usim-preview');
	assert.strictEqual(els['sp-tar'].value, 'B00001');
	els['chain-ram-preview'].value = '80E29000';
	packToSp('chain-ram-preview');
	assert.strictEqual(els['sp-tar'].value, '000000');
});

test('the TAR row values are left-aligned like the headers', () => {
	const out = cardsTarRowHtml({ tar: 'AF4D01', msl: '0A', desc: 'x' }, 3);
	assert.doesNotMatch(out, /text-center/);
	assert.match(out, /<td class="py-1 px-2"><input id="cards-tr-3-tar" class="w-full/);
	assert.match(out, /<td class="py-1 px-2"><input id="cards-tr-3-msl" class="w-full/);
});

test('the keyset row renders as a table row with full-width inputs', () => {
	const out = cardsKeysetRowHtml({ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }, 0);
	assert.match(out, /^<tr>/);
	assert.match(out, /<td class="py-1 px-2"><span id="cards-ks-0-kvn"/);
	assert.match(out, /<td class="py-1 px-2"><input id="cards-ks-0-kic" class="w-full/);
	assert.match(out, /<td class="py-1 px-2"><input id="cards-ks-0-kic-key" class="w-full/);
	assert.match(out, /<td class="py-1 px-2"><input id="cards-ks-0-cntr" class="w-full/);
	assert.match(out, /cardsKeysetRemove\(0\)/);
});

test('the TAR table renders the role rows and adds/removes free rows', () => {
	const els = setup();
	cardsTarRowsRender([{ tar: 'AF4D01', msl: '0A', desc: 'one' },
		{ tar: 'AF4D02', msl: '0A', desc: 'two' }]);
	assert.strictEqual(_cardsTarCount, 2);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-0-tar"[^>]*value=""/);
	assert.match(els['cards-tars'].innerHTML, /ISD compact format/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-3-tar"[^>]*value="AF4D01"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-4-tar"[^>]*value="AF4D02"/);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-4-desc"[^>]*value="two"/);
	assert.match(els['cards-tars'].innerHTML, /cardsTarRemove\(4\)/);
	// simulate the DOM the innerHTML produced
	tarRow(els, 3, { tar: 'AF4D01', msl: '0A', desc: 'one' });
	tarRow(els, 4, { tar: 'AF4D02', msl: '0A', desc: 'two' });
	cardsTarRemove(3);
	assert.strictEqual(_cardsTarCount, 1);
	assert.match(els['cards-tars'].innerHTML, /value="AF4D02"/);
	assert.doesNotMatch(els['cards-tars'].innerHTML, /value="AF4D01"/);
	tarRow(els, 3, { tar: 'AF4D02', msl: '0A', desc: 'two' });
	cardsTarAdd();
	assert.strictEqual(_cardsTarCount, 2);
	// the new row renders empty
	tarRow(els, 4, { tar: '', msl: '', desc: '' });
	assert.deepStrictEqual(cardsFreeTarsFromForm(), [{ tar: 'AF4D02', msl: '0A', desc: 'two' }]);
	// the role rows are never removable
	cardsTarRemove(0);
	assert.strictEqual(_cardsTarCount, 2);
	// an emptied role field survives a re-render (no silent default refill),
	// while the save path still fills the default
	tarRow(els, 0, { tar: '', msl: '' });
	cardsTarRemove(3);
	assert.strictEqual(_cardsTarCount, 0);
	assert.match(els['cards-tars'].innerHTML, /cards-tr-0-tar"[^>]*value=""/);
	assert.strictEqual(cardsTarsFromForm()[0].tar, '000000');
	assert.strictEqual(cardsTarsFromForm()[0].msl, '16');
});

test('the list view opens the editor for a new preset and Cancel returns', () => {
	const els = setup();
	globalThis.cards = [presetFixture()];
	cardsNew();
	assert.ok(!els['cards-editor-view'].classList.contains('hidden'), 'the editor is shown');
	assert.ok(els['cards-list-view'].classList.contains('hidden'), 'the list is hidden');
	assert.strictEqual(globalThis._cardsEditIdx, null);
	assert.strictEqual(els['cards-editor-title'].textContent, 'Add preset');
	assert.strictEqual(els['cards-add-btn'].textContent, 'Add');
	assert.strictEqual(_cardsTarCount, 0);
	cardsCancelEdit();
	assert.ok(!els['cards-list-view'].classList.contains('hidden'), 'the list is back');
	assert.ok(els['cards-editor-view'].classList.contains('hidden'), 'the editor is hidden');
});

test('editing opens the editor with the preset title', () => {
	const els = setup();
	globalThis.cards = [presetFixture({ name: 'Foo' })];
	cardsEdit(0);
	assert.strictEqual(globalThis._cardsEditIdx, 0);
	assert.ok(!els['cards-editor-view'].classList.contains('hidden'));
	assert.strictEqual(els['cards-editor-title'].textContent, 'Edit preset: Foo');
	assert.strictEqual(els['cards-add-btn'].textContent, 'Save');
});

test('the editor refuses PIN/PUK codes that are not 4-8 digits', async () => {
	const els = setup();
	els['cards-name'].value = 'X';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' });
	globalThis._cardsKeysetCount = 1;
	const seen = [];
	globalThis.alert = msg => seen.push(msg);
	els['cards-pin1'].value = '123';
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 0);
	assert.match(seen[0], /PIN\/PUK/);
	seen.length = 0;
	els['cards-pin1'].value = '1234';
	els['cards-puk2'].value = '12A4';
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 0);
	assert.match(seen[0], /PUK2/);
});

test('the editor refuses a counter that is not 1-10 hex digits', async () => {
	const els = setup();
	els['cards-name'].value = 'X';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: 'XYZ' });
	globalThis._cardsKeysetCount = 1;
	const seen = [];
	globalThis.alert = msg => seen.push(msg);
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 0);
	assert.match(seen[0], /Counter must be 1-10 hex digits/);
	// a valid value - spaces included, the server strips them - is posted
	seen.length = 0;
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '00 00 00 00 01' });
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 1, JSON.stringify(seen));
	assert.strictEqual(apiCalls[0].body.keysets[0].cntr, '00 00 00 00 01');
	// an empty counter is refused too (the value is required)
	seen.length = 0;
	apiCalls.length = 0;
	els['cards-name'].value = 'X';
	globalThis._cardsKeysetCount = 1;
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '' });
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 0);
	assert.match(seen[0], /Counter must be 1-10 hex digits/);
});
