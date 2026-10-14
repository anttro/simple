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
for (const fn of ['spKeysetKvnOf', 'spKeysetList', 'spKeysetFor', 'spKeysetCheck',
	'spTarOptionsHtml', 'spTarRender', 'spSpi1Seed', 'spTarKeysetGuard', 'cardsTarKeysetCell', 'cardsTarKeysetFromForm', 'cardsTarKeysetRefresh', 'cardsTarRowsRender', 'cardsTarRowHtml', 'cardsTarsFromTable', 'cardsFreeTarsFromForm', 'spCounterTracked', 'spPresetIdx', 'ramPresetIdx',
	'tarPresetIdx', 'spKeysetSync', 'ramRender', 'esc', 'escHtml',
	'cardsTarList', 'cardsTarEntry', 'cardsRoleTar',
	'spTarChanged', 'spKeysetApply', 'updateSpKic', 'updateSpKid']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'function t(s){return s;}\n';
code += 'globalThis.pysimApplyAvailability = () => {};\n';
eval(code);

const PRESET = {
	name: 'Card A',
	keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' },
		{ kic: '29', kid: '29', kicKey: 'CC', kidKey: 'DD', cntr: '0000000005' },
	],
};

test('spKeysetKvnOf reads the keyset number from the high nibble', () => {
	assert.strictEqual(spKeysetKvnOf('15'), 1);
	assert.strictEqual(spKeysetKvnOf('29'), 2);
	assert.strictEqual(spKeysetKvnOf('3A'), 3);
	assert.strictEqual(spKeysetKvnOf('05'), 0);
	assert.strictEqual(spKeysetKvnOf('ff'), 15);
	// not a byte: no keyset number
	assert.strictEqual(spKeysetKvnOf(''), null);
	assert.strictEqual(spKeysetKvnOf('5'), null);
	assert.strictEqual(spKeysetKvnOf('ZZ'), null);
	assert.strictEqual(spKeysetKvnOf(null), null);
});

test('spKeysetList converts a v3.8.0 flat preset into one keyset', () => {
	assert.deepStrictEqual(spKeysetList({ kic: '15', kid: '15', kicKey: 'A', kidKey: 'B',
		cntr: '0000000001' }), [{ kic: '15', kid: '15', kicKey: 'A', kidKey: 'B',
		cntr: '0000000001' }]);
	assert.deepStrictEqual(spKeysetList(PRESET).length, 2);
	assert.deepStrictEqual(spKeysetList(null), []);
	assert.deepStrictEqual(spKeysetList({}), []);
});

test('spKeysetFor finds a keyset by number', () => {
	assert.strictEqual(spKeysetFor(PRESET, 1).cntr, '0000000001');
	assert.strictEqual(spKeysetFor(PRESET, 2).cntr, '0000000005');
	assert.strictEqual(spKeysetFor(PRESET, 3), null);
	assert.strictEqual(spKeysetFor(null, 1), null);
});

test('spKeysetCheck enforces the keyset-number rules', () => {
	// a defined keyset number passes
	assert.strictEqual(spKeysetCheck(PRESET, '15', '15'), '');
	assert.strictEqual(spKeysetCheck(PRESET, '29', '29'), '');
	// '00'/'00' means no security: no keyset needed (TS 102 225 A.2)
	assert.strictEqual(spKeysetCheck(PRESET, '00', '00'), '');
	// ... and one zero byte with a defined version passes too
	assert.strictEqual(spKeysetCheck(PRESET, '15', '00'), '');
	// KIc/KID versions must agree when both are non-zero (A.2)
	assert.match(spKeysetCheck(PRESET, '15', '25'), /same keyset number/);
	// the keyset number must be defined in the preset
	assert.match(spKeysetCheck(PRESET, '35', '35'), /3 .*not defined/);
	// one zero byte: the other byte's version applies (keyset 2 is defined)
	assert.strictEqual(spKeysetCheck(PRESET, '00', '25'), '');
	assert.match(spKeysetCheck(PRESET, '00', '35'), /3 .*not defined/);
	// without a preset only the byte rules apply (hand-entered keys)
	assert.strictEqual(spKeysetCheck(null, '35', '35'), '');
	assert.match(spKeysetCheck(null, '15', '25'), /15 \/ 25/);
	// malformed bytes are named
	assert.match(spKeysetCheck(PRESET, '5', '15'), /KIc/);
	assert.match(spKeysetCheck(PRESET, '15', 'ZZ'), /KID/);
});

test('spTarKeysetGuard follows the TAR default keyset (v3.26.0)', () => {
	const preset = Object.assign({}, PRESET, { tars: [
		{ role: 'isd', tar: '000000', msl: '16', keyset: 1 },
		{ role: 'uiccRfm', tar: 'B00000', msl: '16', keyset: 2 },
		{ role: 'usimRfm', tar: 'B00001', msl: '00' },
		{ tar: 'B00011', msl: '16', keyset: 3 }] });
	assert.strictEqual(spTarKeysetGuard(preset, '000000'), '');
	assert.strictEqual(spTarKeysetGuard(preset, 'B00001'), '');   // MSL 00: keyless is fine
	// a TAR naming a keyset the preset does not define
	assert.match(spTarKeysetGuard(preset, 'B00011'), /is not defined in the preset/);
	// a security TAR without a default keyset
	const noKeyset = Object.assign({}, PRESET, { tars: [
		{ role: 'isd', tar: '000000', msl: '16' }] });
	assert.match(spTarKeysetGuard(noKeyset, '000000'), /needs a default keyset/);
	// a TAR outside the preset is the server's business; no preset: nothing
	assert.strictEqual(spTarKeysetGuard(preset, 'B00099'), '');
	assert.strictEqual(spTarKeysetGuard(null, '000000'), '');
});

test('the Cards form wires the keyset editor', () => {
	assert.match(html, /id="cards-keysets"/);
	assert.match(html, /onclick="cardsKeysetAdd\(\)"/);
	assert.match(html, /oninput="cardsKeysetKvnUpdate\(/);
	assert.match(html, /data-l10n="Add keyset"/);
	// the remove button is the red action style, like the preset list's Remove
	assert.match(html, /cardsKeysetRemove\(' \+ i \+ '\)" class="[^"]*bg-red-600 text-white/);
	// the keyset rows are built with the KIc/KID/key/counter inputs
	assert.match(html, /cards-ks-' \+ i \+ '-kic-key/);
	assert.match(html, /cards-ks-' \+ i \+ '-cntr/);
	// v3.26.0: the keyset selectors are gone - the SCP80 TAR select drives
	// the keyset, the RAM operations use the preset's ISD entry, and the TAR
	// table gained a keyset column
	assert.match(html, /id="sp-tar" onchange="spTarChanged\(\)"/);
	assert.ok(!html.includes('id="sp-keyset-sel"'));
	assert.ok(!html.includes('id="ram-keyset-sel"'));
	assert.ok(!html.includes('id="tar-keyset-sel"'));
	assert.match(html, /id="cards-tr-' \+ i \+ '-keyset"/);
	// the packet actions run the guard
	assert.ok(html.includes('const ksErr = spKeysetGuard();'));
	assert.ok(html.includes('const ksErr = ramKeysetGuard();'));
});

test('spCounterTracked follows SPI1 b5b4 (TS 102 225 5.1.1)', () => {
	// b5b4 = 00: "present, ignored, never updated" - no counter to track
	for (const spi1 of ['00', '01', '06', '']) {
		assert.strictEqual(spCounterTracked(spi1), false, spi1);
	}
	for (const spi1 of ['08', '10', '16', '18', '1E']) {
		assert.strictEqual(spCounterTracked(spi1), true, spi1);
	}
	assert.strictEqual(spCounterTracked('zz'), false);
});

test('ramRender keeps the remembered preset (v3.26.0: no keyset selector)', () => {
	const els = {
		'ram-card-sel': { value: '', innerHTML: '', appendChild() {} },
		'sp-kic-hex': { value: '25' },
		'sp-kid-hex': { value: '25' },
	};
	globalThis.document = {
		getElementById: id => els[id] || null,
		createElement: () => ({ value: '', textContent: '' }),
	};
	globalThis.cards = [{ name: 'EP', keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' },
		{ kic: '25', kid: '25', kicKey: 'CC', kidKey: 'DD', cntr: '0000000184' }] }];
	globalThis._ramCardIdx = 0;
	globalThis.ramOpChanged = () => {};
	globalThis.ramExpandedDetailsInit = () => {};
	ramRender();
	assert.strictEqual(els['ram-card-sel'].value, '0',
		'the preset select keeps the remembered card');
});

test('the RAM and TAR views rebuild their keyset lists when their preset is known', () => {
	// ramRender() syncs the keyset selectors once the preset is set ...
	const ram = html.slice(html.indexOf('function ramRender()'),
		html.indexOf('function ramRender()') + 1400);
	assert.ok(ram.includes('spKeysetSync();'), 'ramRender must rebuild the keyset lists');
	// ... tarRender() already does, and keeps the remembered selection
	const tar = html.slice(html.indexOf('function tarRender()'),
		html.indexOf('function tarRender()') + 1200);
	assert.ok(tar.includes('spKeysetSync();'));
	assert.ok(tar.includes(': _ramCardIdx;'), 'tarRender keeps the remembered preset');
	// opening the TAR pill populates both its selectors
	assert.ok(html.includes("if (name === 'tar') { tarRender(); tarProbeRender(); }"));
});

test('spTarChanged applies the TAR keyset and clears it for a keyless TAR', () => {
	// v3.26.0 review: a TAR without a default keyset (MSL 00) must reset the
	// form to the no-security '00/00' form - a stale previous TAR's KIc/KID
	// would otherwise travel explicitly and build a non-keyless packet
	globalThis.updateKeyPlaceholder = () => {};
	globalThis.spInvalidate = () => {};
	globalThis.updateSp = () => {};
	globalThis.spCntrPlaceholder = () => {};
	const els = {
		'sp-tar': { value: '', disabled: false, innerHTML: '', options: [] },
		'sp-card-sel': { value: '0' },
		'sp-kic-idx': { value: '' }, 'sp-kic-alg': { value: '' }, 'sp-kic-hex': { value: '' },
		'sp-kid-idx': { value: '' }, 'sp-kid-alg': { value: '' }, 'sp-kid-hex': { value: '' },
		'sp-kic-key': { value: '' }, 'sp-kid-key': { value: '' }, 'sp-cntr': { value: 'X' },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.cards = [{ name: 'EP', keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }],
		tars: [
			{ role: 'isd', tar: '000000', msl: '16', keyset: 1 },
			{ tar: 'B00011', msl: '00' }] }];
	// a keyset TAR applies its KIc/KID and keys (the counter override clears)
	els['sp-tar'].value = '000000';
	spTarChanged();
	assert.strictEqual(els['sp-kic-hex'].value, '15');
	assert.strictEqual(els['sp-kid-hex'].value, '15');
	assert.strictEqual(els['sp-kic-key'].value, 'AA');
	assert.strictEqual(els['sp-cntr'].value, '');
	// a keyless TAR (MSL 00, no default keyset) resets to the no-security form
	els['sp-kic-key'].value = 'AA';
	els['sp-tar'].value = 'B00011';
	spTarChanged();
	assert.strictEqual(els['sp-kic-hex'].value, '00');
	assert.strictEqual(els['sp-kid-hex'].value, '00');
	assert.strictEqual(els['sp-kic-key'].value, '');
});

test('the TAR selector waits for a preset and lists its entries', () => {
	const els = {
		'sp-tar': { value: '', disabled: false, innerHTML: '', options: [] },
		'sp-card-sel': { value: '' },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.cards = [];
	spTarRender();
	assert.strictEqual(els['sp-tar'].disabled, true, 'no preset: the selector waits');
	assert.match(els['sp-tar'].innerHTML, /select a card preset/);
	// a selected preset enables the selector and lists its TARs with their
	// MSL and default keyset
	globalThis.cards = [{ name: 'EP', tars: [
		{ role: 'isd', tar: '000000', msl: '16', keyset: 1 },
		{ tar: 'AF4D02', msl: '0A', desc: 'My applet' }] }];
	els['sp-card-sel'].value = '0';
	spTarRender();
	assert.strictEqual(els['sp-tar'].disabled, false);
	assert.match(els['sp-tar'].innerHTML, /value="000000"[^>]*>000000 \u00b7 ISD \u00b7 MSL 16 \u00b7 Keyset 1</);
	assert.match(els['sp-tar'].innerHTML, /value="AF4D02"[^>]*>AF4D02 \u00b7 My applet \u00b7 MSL 0A</);
});

test('clearing the preset selection resets the TAR selector', () => {
	const at = html.indexOf('function cardsApply(');
	assert.ok(at > 0);
	const body = html.slice(at, at + 600);
	assert.ok(body.includes('spTarRender(null);'),
		'cardsApply must reset the TAR selector when no preset is selected');
});
