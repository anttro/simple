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

let code = '';
for (const fn of ['cardsTarValue', 'cardsFormValues', 'cardsClearForm', 'cardsEdit',
	'cardsAdd', 'cardsImport', 'cardsApply', 'ramApplyCard',
	'spTarKeyForPack', 'spPresetTar', 'packToSp']) {
	code += extractFunc(html, fn) + '\n';
}
eval(code);

const CARD_IDS = ['cards-name','cards-iccid','cards-adm','cards-kic','cards-kid',
	'cards-spi1','cards-spi2','cards-tar','cards-uicc-tar','cards-usim-tar',
	'cards-cntr','cards-kic-key','cards-kid-key','cards-psk-id','cards-psk-key',
	'cards-add-btn','cards-cancel-btn'];
const SP_IDS = ['sp-card-sel','sp-tar','sp-spi1','sp-spi2','sp-spi2-sm','sp-spi2-hex',
	'sp-kic-idx','sp-kic-alg','sp-kic-hex','sp-kid-idx','sp-kid-alg','sp-kid-hex',
	'sp-cntr','sp-kic-key','sp-kid-key','sp-apdu','sp-result','sp-spi1-hex'];
const CHAIN_IDS = ['chain-sim-preview','chain-usim-preview','chain-ram-preview',
	'ber-result','hota-preview'];

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

function setup() {
	const els = {};
	for (const id of CARD_IDS.concat(SP_IDS, CHAIN_IDS)) els[id] = fakeEl();
	globalThis.document = { getElementById: id => els[id] || null, querySelectorAll: () => [] };
	globalThis.cards = [];
	globalThis._cardsEditIdx = null;
	globalThis._spTarKey = 'tar';
	globalThis.t = s => s;
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
	return els;
}

test('cardsTarValue falls back to the per-target spec defaults', () => {
	assert.strictEqual(cardsTarValue('tar', '123456'), '123456');
	assert.strictEqual(cardsTarValue('tar', ''), '000000');
	assert.strictEqual(cardsTarValue('uiccTar', ''), 'B00000');
	assert.strictEqual(cardsTarValue('usimTar', null), 'B00001');
	assert.strictEqual(cardsTarValue('nope', ''), '');
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

test('saving a preset stores spec defaults for empty TARs', () => {
	const els = setup();
	els['cards-name'].value = 'New card';
	els['cards-kic'].value = '15';
	els['cards-kid'].value = '15';
	els['cards-kic-key'].value = 'AA';
	els['cards-kid-key'].value = 'BB';
	els['cards-tar'].value = '';
	els['cards-uicc-tar'].value = '';
	els['cards-usim-tar'].value = '';
	els['cards-adm'].value = 'aa bb';
	cardsAdd();
	assert.strictEqual(cards.length, 1);
	assert.strictEqual(cards[0].tar, '000000');
	assert.strictEqual(cards[0].uiccTar, 'B00000');
	assert.strictEqual(cards[0].usimTar, 'B00001');
	assert.strictEqual(cards[0].adm, 'AABB');
	assert.strictEqual(cards[0].spi1, '16');
});

test('import fills missing TARs with spec defaults and normalizes ADM', () => {
	setup();
	cardsImport(JSON.stringify([
		{ name: 'Imported', kic: '15', kid: '15', adm: ' aa bb ' },
		{ name: 'Explicit', tar: '000009', uiccTar: 'B0000C', usimTar: 'B0000D', adm: 'cc' },
	]));
	assert.strictEqual(cards.length, 2);
	assert.strictEqual(cards[0].tar, '000000');
	assert.strictEqual(cards[0].uiccTar, 'B00000');
	assert.strictEqual(cards[0].usimTar, 'B00001');
	assert.strictEqual(cards[0].adm, 'AABB');
	assert.strictEqual(cards[1].tar, '000009');
	assert.strictEqual(cards[1].uiccTar, 'B0000C');
	assert.strictEqual(cards[1].usimTar, 'B0000D');
	assert.strictEqual(cards[1].adm, 'CC');
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
