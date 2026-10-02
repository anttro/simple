const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractBlock(startMarker, endMarker) {
	const start = html.indexOf(startMarker);
	const end = html.indexOf(endMarker, start);
	if (start < 0 || end < 0) throw new Error('block not found: ' + startMarker);
	return html.slice(start, end);
}

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

// Rewrite top-level const -> var so the maps leak out of sloppy-mode eval.
eval(extractBlock('const CAT_RELEASE = {', '// The display label of an introduction release')
	.replace(/^const /gm, 'var '));
eval(extractFunc(html, 'catReleaseLabel'));
eval(extractBlock('const EVENT_NAMES = {', 'const REJECTION_CAUSES = [').replace(/^const /gm, 'var '));

test('CAT_RELEASE carries the introduction releases of the event list', () => {
	assert.strictEqual(CAT_RELEASE.events[0x10], 6);
	assert.strictEqual(CAT_RELEASE.events[0x12], 8);
	assert.strictEqual(CAT_RELEASE.events[0x1D], 14);
	assert.strictEqual(CAT_RELEASE.events[0x1E], 17);
	assert.strictEqual(CAT_RELEASE.events[0x1F], 16);
	// the Void 0x1A is not an event and the reserved 0x20+ codes are not
	// introduced by any release
	assert.strictEqual(CAT_RELEASE.events[0x1A], undefined);
	assert.strictEqual(CAT_RELEASE.events[0x20], undefined);
});

test('every named event through 0x1F has an introduction release', () => {
	const named = Object.keys(EVENT_NAMES).map(Number).filter(c => c <= 0x1F && c !== 0x1A);
	for (const code of named) {
		assert.strictEqual(typeof CAT_RELEASE.events[code], 'number', '0x' + code.toString(16));
	}
	assert.strictEqual(Object.keys(CAT_RELEASE.events).length, named.length);
});

test('CAT_RELEASE carries commands, objects and PLI qualifiers', () => {
	assert.strictEqual(CAT_RELEASE.commands['OPEN CHANNEL'], 4);
	assert.strictEqual(CAT_RELEASE.commands['SET FRAMES'], 6);
	assert.strictEqual(CAT_RELEASE.commands['GEOGRAPHICAL LOCATION REQUEST'], 8);
	assert.strictEqual(CAT_RELEASE.commands['ACTIVATE'], 13);
	assert.strictEqual(CAT_RELEASE.commands['LSI COMMAND'], 17);
	assert.strictEqual(CAT_RELEASE.objects['67/E7 Frames Information'], 6);
	assert.strictEqual(CAT_RELEASE.objects['9D Data connection status'], 14);
	assert.strictEqual(CAT_RELEASE.objects['55/D5 CAG cell selection status'], 17);
	assert.strictEqual(CAT_RELEASE.pli['08'], 6);
	assert.strictEqual(CAT_RELEASE.pli['14'], 13);
	assert.strictEqual(CAT_RELEASE.pli['15'], 16);
	assert.strictEqual(CAT_RELEASE.pli['16'], 17);
});

test('catReleaseLabel collapses everything below Rel-6', () => {
	assert.strictEqual(catReleaseLabel(0), '');
	assert.strictEqual(catReleaseLabel(undefined), '');
	assert.strictEqual(catReleaseLabel(4), 'pre-Rel-6');
	assert.strictEqual(catReleaseLabel(5), 'pre-Rel-6');
	assert.strictEqual(catReleaseLabel(6), 'Rel-6');
	assert.strictEqual(catReleaseLabel(18), 'Rel-18');
});
