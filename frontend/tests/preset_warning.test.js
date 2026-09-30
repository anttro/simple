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
for (const fn of ['swapNibbles', 'encIccid', 'decIccid', 'cardsNormIccid',
	'spPresetWarning']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'function t(s){return s;}\n';
eval(code);

const DIGITS = '8970119000004600098';
const RAW_HEX = '980711090000640090F8';
const OTHER = '89390100000129506903';
const CARDS = [
	{ name: 'Card A', iccid: DIGITS },
	{ name: 'Card B', iccid: OTHER },
];

test('no warning without a card session or a readable ICCID', () => {
	assert.strictEqual(spPresetWarning(null, CARDS, 0), '');
	assert.strictEqual(spPresetWarning('', CARDS, 0), '');
	assert.strictEqual(spPresetWarning(undefined, CARDS, 0), '');
});

test('no warning when the equipped card matches a preset', () => {
	assert.strictEqual(spPresetWarning(DIGITS, CARDS, 0), '');
	// the preset's ICCID may be stored in any form
	assert.strictEqual(spPresetWarning(DIGITS, [{ name: 'A', iccid: RAW_HEX }], 0), '');
	// the match does not depend on the selection
	assert.strictEqual(spPresetWarning(DIGITS, CARDS, 1) === '', false);
});

test('a card with no matching preset gets the strong warning', () => {
	const text = spPresetWarning('8945001234567890123', CARDS, 0);
	assert.match(text, /8945001234567890123/);
	assert.match(text, /does not match any card preset/);
	assert.match(text, /From card/);
	// no presets at all: the other wording
	const none = spPresetWarning(DIGITS, [], 0);
	assert.match(none, /No card presets defined/);
	assert.match(spPresetWarning(DIGITS, null, -1), /No card presets defined/);
});

test('a manually selected preset of another card gets the softer hint', () => {
	const text = spPresetWarning(DIGITS, CARDS, 1);
	assert.match(text, /selected preset is not the equipped card/);
	assert.match(text, /Card A/);
	// an empty / out-of-range selection is not the softer case (the strong
	// warning above covers a card with no preset)
	assert.strictEqual(spPresetWarning(DIGITS, CARDS, -1), '');
	assert.strictEqual(spPresetWarning(DIGITS, CARDS, 5), '');
});

test('both SCP80 views carry the warning banner', () => {
	assert.match(html, /id="sp-preset-warning" class="hidden mb-3 px-3 py-2 rounded border border-amber-400/);
	assert.match(html, /id="ram-preset-warning" class="hidden mb-3 px-3 py-2 rounded border border-amber-400/);
	// ... and the renderer is wired into the card state, the preset load and
	// the preset selection
	assert.ok(html.includes('spPresetWarningRender();'));
	assert.ok(html.includes("show('sp-preset-warning', spPresetWarning(iccid, cards, spPresetIdx()));"));
	assert.ok(html.includes("show('ram-preset-warning', spPresetWarning(iccid, cards, ramPresetIdx()));"));
});
