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
	'spKeysetOptionsHtml', 'spCounterTracked', 'spPresetIdx', 'ramPresetIdx',
	'tarPresetIdx', 'spKeysetSync', 'ramRender', 'esc', 'escHtml']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'function t(s){return s;}\n';
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

test('spKeysetOptionsHtml lists the keysets and marks an undefined number', () => {
	const html1 = spKeysetOptionsHtml(PRESET, '15', '15');
	assert.match(html1, /value="1" selected/);
	assert.match(html1, /value="2"/);
	assert.ok(html1.indexOf('0000000005') > 0, 'the counters are shown');
	// a version the preset does not define is offered but marked
	const html2 = spKeysetOptionsHtml(PRESET, '35', '35');
	assert.match(html2, /value="3" selected/);
	assert.match(html2, /not defined in the preset/);
	// a preset without keysets: the form's version shows as undefined
	const html3 = spKeysetOptionsHtml({ keysets: [] }, '15', '15');
	assert.match(html3, /not defined in the preset/);
	// no keyset number in the form and no keysets at all
	assert.match(spKeysetOptionsHtml({}, '', ''), /no keyset/);
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
	// the selectors exist in both SCP80 views
	assert.match(html, /id="sp-keyset-sel" onchange="spKeysetChanged\(\)"/);
	assert.match(html, /id="ram-keyset-sel" onchange="ramKeysetChanged\(\)"/);
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

test('ramRender rebuilds the keyset selector from the remembered preset', () => {
	// regression: the auto-selection runs before the RAM select has options, so
	// its keyset list was built with no preset and stayed on "not defined"
	const els = {
		'ram-card-sel': { value: '', innerHTML: '', appendChild() {} },
		'ram-keyset-sel': { value: '', innerHTML: '' },
		'sp-keyset-sel': { value: '', innerHTML: '' },
		'tar-keyset-sel': { value: '', innerHTML: '' },
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
	assert.match(els['ram-keyset-sel'].innerHTML, /value="2" selected/);
	assert.ok(!els['ram-keyset-sel'].innerHTML.includes('not defined'),
		'the keyset selector must not show a stale "not defined" entry');
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

test('the keyset selector is disabled and cleared while no preset is selected', () => {
	const els = {
		'sp-keyset-sel': { value: '', innerHTML: '' },
		'ram-keyset-sel': { value: '', innerHTML: '' },
		'tar-keyset-sel': { value: '', innerHTML: '' },
		'sp-card-sel': { value: '' },
		'ram-card-sel': { value: '' },
		'tar-card-sel': { value: '' },
		'sp-kic-hex': { value: '' },
		'sp-kid-hex': { value: '' },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.cards = [];
	spKeysetSync();
	for (const id of ['sp-keyset-sel', 'ram-keyset-sel', 'tar-keyset-sel']) {
		assert.strictEqual(els[id].disabled, true, id + ' must be disabled');
		assert.match(els[id].innerHTML, /select a card preset/);
		assert.ok(!els[id].innerHTML.includes('not defined'), id);
	}
	// a selected preset enables the selector and lists its keysets
	globalThis.cards = [{ name: 'EP', keysets: [
		{ kic: '15', kid: '15', kicKey: 'AA', kidKey: 'BB', cntr: '0000000001' }] }];
	els['sp-card-sel'].value = '0';
	els['ram-card-sel'].value = '0';
	els['tar-card-sel'].value = '0';
	els['sp-kic-hex'].value = '15';
	els['sp-kid-hex'].value = '15';
	spKeysetSync();
	for (const id of ['sp-keyset-sel', 'ram-keyset-sel', 'tar-keyset-sel']) {
		assert.strictEqual(els[id].disabled, false, id + ' must be enabled');
		assert.match(els[id].innerHTML, /value="1" selected/);
	}
});

test('clearing the preset selection clears the keyset selectors', () => {
	const at = html.indexOf('function cardsApply(');
	assert.ok(at > 0);
	const body = html.slice(at, at + 600);
	assert.ok(body.includes('spKeysetSync();'),
		'cardsApply must sync the keyset selectors when no preset is selected');
});
