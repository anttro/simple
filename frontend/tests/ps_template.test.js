// The PS template DO ('C6') decoder - TS 102 221 §11.1.1.4.10 / §9.5.2: the
// PS_DO bitmap over the following key reference ('83') objects, MSB first
// (b8 = the first reference), an optional '95' applying to the next '83'.
// The vectors are shared with the server (simple_server/fcp.py's
// ps_reference_enabled), so the two implementations cannot drift apart.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const vectors = JSON.parse(fs.readFileSync(
	path.join(__dirname, 'ps_template_vectors.json'), 'utf8'));

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

eval(['parseBerLen', 'fcpParseTlvs', 'pinKeyRefName', 'fcpPsTemplate']
	.map(f => extractFunc(html, f)).join('\n'));

test('the PWA PS template decoder matches the shared vectors', () => {
	assert.ok(vectors.length >= 10, 'the fixture is too small');
	for (const v of vectors) {
		assert.strictEqual(fcpPsTemplate(v.ps_template), v.decoded, v.name);
	}
});

test('a reference with no defined bit reads "unknown", never "disabled"', () => {
	// No PS_DO: the specs define no answer, so the reference is unknown.
	assert.match(fcpPsTemplate('830101'), /PIN app1: unknown/);
	// A bit beyond a one-byte PS_DO.
	const nine = ['01', '81', '82', '83', '84', '85', '86', '87', '88']
		.map(r => '8301' + r).join('');
	assert.match(fcpPsTemplate('9001C0' + nine), /PIN2 app8: unknown/);
});

test('the decoder still resolves the ordinary enabled/disabled bits', () => {
	assert.strictEqual(fcpPsTemplate('9001C0830101'),
		'PIN app1: enabled, always verified');
	assert.strictEqual(fcpPsTemplate('900100830111'),
		'universal PIN: disabled, always verified');
	assert.strictEqual(fcpPsTemplate('9001C0950108830101'),
		'PIN app1: enabled, verify');
});
