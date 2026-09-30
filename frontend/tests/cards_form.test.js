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

let code = '';
const ASYNC_FNS = ['cardsAdd', 'cardsImport', 'cardsRemove'];
for (const fn of ['cardsTarValue', 'cardsFormValues', 'cardsClearForm', 'cardsEdit',
	'cardsAdd', 'cardsImport', 'cardsRemove', 'ramCardIdxAfterRemove',
	'spPresetIdx', 'spCntrSyncPreset', 'spCntrLocalSync', 'ramSaveCntr',
	'cardsApplyFields', 'cardsApply', 'ramApplyCard',
	'cardsScp80Complete', 'cardsScp81Complete', 'cardsAdmPresent',
	'spTarKeyForPack', 'spPresetTar', 'packToSp',
	'esc', 'escHtml',
	'spKeysetKvnOf', 'spKeysetList', 'spKeysetFor', 'spKeysetCheck',
	'spKeysetOptionsHtml', 'cardsKeysetRowHtml', 'cardsKeysetRowsRender',
	'cardsKeysetAdd', 'cardsKeysetRemove', 'cardsKeysetKvnUpdate',
	'cardsKeysetsFromForm', 'spKeysetSync', 'spKeysetApply', 'spKeysetChanged',
	'ramKeysetChanged', 'ramPresetIdx', 'spKeysetGuard', 'ramKeysetGuard']) {
	code += extractFunc(html, fn, ASYNC_FNS.indexOf(fn) >= 0) + '\n';
}
code = 'var _cardsKeysetCount = 0;\n' + code;
eval(code);

const CARD_IDS = ['cards-name','cards-iccid','cards-adm',
	'cards-spi1','cards-spi2','cards-tar','cards-uicc-tar','cards-usim-tar',
	'cards-psk-id','cards-psk-key','cards-keysets',
	'cards-add-btn','cards-cancel-btn',
	// the keyset editor rows (two rows is enough for the tests)
	'cards-ks-0-kvn','cards-ks-0-kic','cards-ks-0-kid','cards-ks-0-kic-key',
	'cards-ks-0-kid-key','cards-ks-0-cntr',
	'cards-ks-1-kvn','cards-ks-1-kic','cards-ks-1-kid','cards-ks-1-kic-key',
	'cards-ks-1-kid-key','cards-ks-1-cntr'];
const SP_IDS = ['sp-card-sel','sp-tar','sp-spi1','sp-spi2','sp-spi2-sm','sp-spi2-hex',
	'sp-kic-idx','sp-kic-alg','sp-kic-hex','sp-kid-idx','sp-kid-alg','sp-kid-hex',
	'sp-cntr','sp-kic-key','sp-kid-key','sp-apdu','sp-result','sp-spi1-hex',
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

function fakeEl() {
	return {
		value: '',
		textContent: '',
		innerHTML: '',
		focus() {},
		classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
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
	globalThis._spTarKey = 'tar';
	globalThis.t = s => s;
	globalThis.esc = s => s;
	globalThis.escHtml = s => s;
	globalThis.alert = () => {};
	globalThis.cardsSave = () => {};
	globalThis.cardsRender = () => {};
	globalThis.cardsRebuildSelect = () => {};
	globalThis.scp81PushPskMap = () => {};
	globalThis.cardsSetFormMode = () => {};
	globalThis.cardsCancelEdit = () => {};
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

test('cardsTarValue falls back to the per-target spec defaults', () => {
	assert.strictEqual(cardsTarValue('tar', '123456'), '123456');
	assert.strictEqual(cardsTarValue('tar', ''), '000000');
	assert.strictEqual(cardsTarValue('uiccTar', ''), 'B00000');
	assert.strictEqual(cardsTarValue('usimTar', null), 'B00001');
	assert.strictEqual(cardsTarValue('nope', ''), '');
});

test('preset completeness predicates used by the header markers', () => {
	const full = { kic: '15', kid: '15', spi1: '16', spi2: '01', cntr: '0000000001',
		kicKey: 'AA', kidKey: 'BB', pskIdentity: 'id', pskKey: 'KEY', adm: '0011' };
	assert.ok(cardsScp80Complete(full));
	assert.ok(cardsScp81Complete(full));
	assert.ok(cardsAdmPresent(full));
	// every SCP80 field is required; whitespace counts as empty
	for (const field of ['kic', 'kid', 'spi1', 'spi2', 'cntr', 'kicKey', 'kidKey']) {
		const c = Object.assign({}, full);
		c[field] = '   ';
		assert.ok(!cardsScp80Complete(c), 'SCP80 marked complete with empty ' + field);
	}
	assert.ok(!cardsScp81Complete({ pskIdentity: 'id' }));
	assert.ok(!cardsScp81Complete({ pskKey: 'KEY' }));
	assert.ok(!cardsScp80Complete(null));
	assert.ok(!cardsScp81Complete(null));
	assert.ok(!cardsAdmPresent(null));
	assert.ok(!cardsAdmPresent({ adm: ' ' }));
});

test('clearing the card form keeps the TAR spec defaults', () => {
	const els = setup();
	els['cards-tar'].value = 'AABBCC';
	els['cards-uicc-tar'].value = 'AABBCC';
	els['cards-usim-tar'].value = 'AABBCC';
	els['cards-adm'].value = 'DEAD';
	els['cards-name'].value = 'X';
	cardsClearForm();
	assert.strictEqual(els['cards-tar'].value, '000000');
	assert.strictEqual(els['cards-uicc-tar'].value, 'B00000');
	assert.strictEqual(els['cards-usim-tar'].value, 'B00001');
	assert.strictEqual(els['cards-adm'].value, '');
	assert.strictEqual(els['cards-name'].value, '');
});

test('editing prefills empty stored TARs with the spec defaults', () => {
	const els = setup();
	globalThis.cards = [
		{ name: 'Legacy', kic: '15', kid: '15', tar: '' },
		{ name: 'Full', kic: '15', kid: '15', tar: '000001', uiccTar: 'B0000C', usimTar: 'B0000D', adm: '0011' },
	];
	cardsEdit(0);
	assert.strictEqual(els['cards-tar'].value, '000000');
	assert.strictEqual(els['cards-uicc-tar'].value, 'B00000');
	assert.strictEqual(els['cards-usim-tar'].value, 'B00001');
	assert.strictEqual(els['cards-adm'].value, '');
	cardsEdit(1);
	assert.strictEqual(els['cards-tar'].value, '000001');
	assert.strictEqual(els['cards-uicc-tar'].value, 'B0000C');
	assert.strictEqual(els['cards-usim-tar'].value, 'B0000D');
	assert.strictEqual(els['cards-adm'].value, '0011');
});

test('the form values carry the ADM and per-target TAR fields', () => {
	const els = setup();
	els['cards-tar'].value = ' 000001 ';
	els['cards-uicc-tar'].value = 'B00002';
	els['cards-usim-tar'].value = 'B00003';
	els['cards-adm'].value = ' 00 11 22 33 ';
	const v = cardsFormValues();
	assert.strictEqual(v.tar, '000001');
	assert.strictEqual(v.uiccTar, 'B00002');
	assert.strictEqual(v.usimTar, 'B00003');
	assert.strictEqual(v.adm, '00112233');
});

test('saving a preset posts the form with spec-default TARs', async () => {
	const els = setup();
	els['cards-name'].value = 'New card';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB' });
	globalThis._cardsKeysetCount = 1;
	els['cards-tar'].value = '';
	els['cards-uicc-tar'].value = '';
	els['cards-usim-tar'].value = '';
	els['cards-adm'].value = 'aa bb';
	await cardsAdd();
	assert.strictEqual(apiCalls.length, 1, JSON.stringify(apiCalls));
	assert.strictEqual(apiCalls[0].path, '/api/presets');
	assert.strictEqual(apiCalls[0].body.name, 'New card');
	assert.deepStrictEqual(apiCalls[0].body.keysets, [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '' }]);
	assert.strictEqual(apiCalls[0].body.tar, '000000');
	assert.strictEqual(apiCalls[0].body.uiccTar, 'B00000');
	assert.strictEqual(apiCalls[0].body.usimTar, 'B00001');
	assert.strictEqual(apiCalls[0].body.adm, 'AABB');
});

test('editing a preset updates it by its store id', async () => {
	const els = setup();
	globalThis.cards = [{ id: 'abc123', name: 'Old', keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }] }];
	globalThis._cardsEditIdx = 0;
	els['cards-name'].value = 'Renamed';
	ksRow(els, 0, { kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB' });
	globalThis._cardsKeysetCount = 1;
	await cardsAdd();
	assert.strictEqual(apiCalls[0].path, '/api/presets/update');
	assert.strictEqual(apiCalls[0].body.id, 'abc123');
	assert.strictEqual(apiCalls[0].body.fields.name, 'Renamed');
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

test('the SCP80 counter edit is written to the store, a reported counter only locally', () => {
	const els = setup();
	globalThis.cards = [{ id: 'abc123', name: 'C', keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }] }];
	els['sp-card-sel'].value = '0';
	els['sp-kic-hex'].value = '15';
	els['sp-kid-hex'].value = '15';
	els['sp-cntr'].value = '0000000009';
	assert.strictEqual(spCntrSyncPreset(), true);
	assert.strictEqual(cards[0].keysets[0].cntr, '0000000009');
	assert.strictEqual(apiCalls.length, 1);
	assert.strictEqual(apiCalls[0].path, '/api/presets/update');
	assert.strictEqual(apiCalls[0].body.id, 'abc123');
	assert.strictEqual(apiCalls[0].body.fields.keysets[0].cntr, '0000000009');
	// a counter the server already persisted: display only, no second write
	apiCalls = [];
	spCntrLocalSync('000000000A');
	assert.strictEqual(cards[0].keysets[0].cntr, '000000000A');
	assert.strictEqual(apiCalls.length, 0);
	// no preset selected: nothing to write
	els['sp-card-sel'].value = '';
	assert.strictEqual(spCntrSyncPreset(), false);
	assert.strictEqual(apiCalls.length, 0);
});

test('applying a preset uses the TAR of the current operation', () => {
	const els = setup();
	globalThis.cards = [{
		name: 'C', kic: '15', kid: '15', spi1: '16', spi2: '01',
		tar: '000000', uiccTar: 'B0000C', usimTar: 'B0000D',
		cntr: '0000000001', kicKey: 'AA', kidKey: 'BB',
	}];
	globalThis._spTarKey = 'tar';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, '000000');
	globalThis._spTarKey = 'uiccTar';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, 'B0000C');
	globalThis._spTarKey = 'usimTar';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, 'B0000D');
	// empty stored TAR -> spec default
	globalThis.cards = [{ name: 'D', kic: '15', kid: '15', tar: '' }];
	globalThis._spTarKey = 'tar';
	cardsApply(0);
	assert.strictEqual(els['sp-tar'].value, '000000');
});

test('RAM card selection forces the ISD TAR', () => {
	const els = setup();
	globalThis.cards = [{ name: 'C', kic: '15', kid: '15', tar: '000000', uiccTar: 'B0000C', usimTar: 'B0000D' }];
	globalThis._spTarKey = 'usimTar';
	ramApplyCard('0');
	assert.strictEqual(globalThis._spTarKey, 'tar');
	assert.strictEqual(els['sp-tar'].value, '000000');
});

test('spTarKeyForPack maps each chain to its preset TAR field', () => {
	assert.strictEqual(spTarKeyForPack('chain-ram-preview'), 'tar');
	assert.strictEqual(spTarKeyForPack('chain-sim-preview'), 'uiccTar');
	assert.strictEqual(spTarKeyForPack('chain-usim-preview'), 'usimTar');
	assert.strictEqual(spTarKeyForPack('ber-result'), null);
	assert.strictEqual(spTarKeyForPack('hota-preview'), null);
});

test('spPresetTar reads the selected preset and falls back to the spec defaults', () => {
	const els = setup();
	globalThis.cards = [{ name: 'A', tar: '000000', uiccTar: 'B0000A' }];
	els['sp-card-sel'].value = '0';
	assert.strictEqual(spPresetTar('uiccTar'), 'B0000A');
	assert.strictEqual(spPresetTar('usimTar'), 'B00001');
	els['sp-card-sel'].value = '';
	assert.strictEqual(spPresetTar('uiccTar'), 'B00000');
});

test('packing a chain selects the TAR of its target', () => {
	const els = setup();
	globalThis.cards = [{ name: 'C', tar: '000000', uiccTar: 'B0000C', usimTar: 'B0000D' }];
	els['sp-card-sel'].value = '0';
	els['chain-sim-preview'].value = 'A0A4000002';
	packToSp('chain-sim-preview');
	assert.strictEqual(globalThis._spTarKey, 'uiccTar');
	assert.strictEqual(els['sp-tar'].value, 'B0000C');
	assert.strictEqual(els['sp-apdu'].value, 'A0A4000002');
	els['chain-usim-preview'].value = '00A4000002';
	packToSp('chain-usim-preview');
	assert.strictEqual(globalThis._spTarKey, 'usimTar');
	assert.strictEqual(els['sp-tar'].value, 'B0000D');
	els['chain-ram-preview'].value = '80E29000';
	packToSp('chain-ram-preview');
	assert.strictEqual(globalThis._spTarKey, 'tar');
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
