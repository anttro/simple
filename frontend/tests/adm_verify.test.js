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
for (const fn of ['pysimAdmRetryPrompt', 'pysimAdmSecuritySw']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'globalThis.t = s => s;\n';
eval(code);

test('pysimAdmRetryPrompt is silent on the first try', () => {
	assert.strictEqual(pysimAdmRetryPrompt(null), null);
	assert.strictEqual(pysimAdmRetryPrompt(undefined), null);
});

test('pysimAdmRetryPrompt warns with the remaining attempts', () => {
	const msg = pysimAdmRetryPrompt(3);
	assert.ok(msg.includes('3'), msg);
	assert.ok(msg.includes('attempt(s) left'), msg);
	assert.ok(msg.includes('block'), msg);
	assert.ok(msg.includes('Try again?'), msg);
});

test('pysimAdmRetryPrompt uses the stronger last-attempt text', () => {
	for (const left of [1, 0]) {
		const msg = pysimAdmRetryPrompt(left);
		assert.ok(msg.includes('last attempt'), left + ': ' + msg);
		assert.ok(msg.includes('Try again?'), msg);
	}
});

test('pysimAdmSecuritySw flags only the access-condition SWs', () => {
	for (const sw of ['6982', '9804']) assert.ok(pysimAdmSecuritySw(sw), sw);
	for (const sw of ['9000', '6985', '63C2', '', null, undefined]) {
		assert.ok(!pysimAdmSecuritySw(sw), String(sw));
	}
});

test('file-manager security failures route through the ADM hint', () => {
	// no raw SW error rendering is left (all three sites use the hint helper)
	assert.ok(!/statusEl\.textContent = 'SW: ' \+ \(data\.sw/.test(html), 'raw SW error rendering is gone');
	assert.strictEqual((html.match(/pysimFsShowError\(statusEl, data\.sw/g) || []).length, 3);
	assert.ok(html.includes("btn.textContent = t('Verify ADM')"));
	assert.ok(html.includes("el.setAttribute('onclick', 'pysimVerifyAdm()')"));
});
