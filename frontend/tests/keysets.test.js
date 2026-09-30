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
	'spKeysetOptionsHtml', 'esc', 'escHtml']) {
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

test('spKeysetKvnOf reads the key version from the high nibble', () => {
	assert.strictEqual(spKeysetKvnOf('15'), 1);
	assert.strictEqual(spKeysetKvnOf('29'), 2);
	assert.strictEqual(spKeysetKvnOf('3A'), 3);
	assert.strictEqual(spKeysetKvnOf('05'), 0);
	assert.strictEqual(spKeysetKvnOf('ff'), 15);
	// not a byte: no key version
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

test('spKeysetFor finds a keyset by key version', () => {
	assert.strictEqual(spKeysetFor(PRESET, 1).cntr, '0000000001');
	assert.strictEqual(spKeysetFor(PRESET, 2).cntr, '0000000005');
	assert.strictEqual(spKeysetFor(PRESET, 3), null);
	assert.strictEqual(spKeysetFor(null, 1), null);
});

test('spKeysetCheck enforces the key-version rules', () => {
	// a defined key version passes
	assert.strictEqual(spKeysetCheck(PRESET, '15', '15'), '');
	assert.strictEqual(spKeysetCheck(PRESET, '29', '29'), '');
	// '00'/'00' means no security: no keyset needed (TS 102 225 A.2)
	assert.strictEqual(spKeysetCheck(PRESET, '00', '00'), '');
	// ... and one zero byte with a defined version passes too
	assert.strictEqual(spKeysetCheck(PRESET, '15', '00'), '');
	// KIc/KID versions must agree when both are non-zero (A.2)
	assert.match(spKeysetCheck(PRESET, '15', '25'), /same key version/);
	// the key version must be defined in the preset
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

test('spKeysetOptionsHtml lists the keysets and marks an undefined version', () => {
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
	// no key version in the form and no keysets at all
	assert.match(spKeysetOptionsHtml({}, '', ''), /no keyset/);
});

test('the Cards form wires the keyset editor', () => {
	assert.match(html, /id="cards-keysets"/);
	assert.match(html, /onclick="cardsKeysetAdd\(\)"/);
	assert.match(html, /oninput="cardsKeysetKvnUpdate\(/);
	assert.match(html, /data-l10n="Add keyset"/);
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
