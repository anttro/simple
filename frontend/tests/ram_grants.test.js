const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name) {
	const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
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
for (const fn of ['updateRcPriv']) code += extractFunc(html, fn) + '\n';
eval(code);

// The install form's grants are per-session choices: the privileges are an
// aggregate of the checkboxes, derived at send time (a browser-restored
// Receipt Generation bit produced 6985 on the live card, 2026-09-28).
function fakeEnv(checked) {
	const els = { 'rc-priv': { value: '' } };
	const boxes = checked.map(([cls, val]) => ({ className: cls, value: val, checked: true }));
	globalThis.document = {
		getElementById: id => els[id] || null,
		querySelectorAll: sel => boxes.filter(b => sel.indexOf('.' + b.className) >= 0),
	};
	return els;
}

test('updateRcPriv aggregates the checked privilege boxes', () => {
	let els = fakeEnv([]);
	updateRcPriv();
	assert.strictEqual(els['rc-priv'].value, '00');
	els = fakeEnv([['rc-priv-b3', '80']]);
	updateRcPriv();
	assert.strictEqual(els['rc-priv'].value, '000080', 'Receipt Generation is the third byte');
	els = fakeEnv([['rc-priv-b1', '80'], ['rc-priv-b3', '80']]);
	updateRcPriv();
	assert.strictEqual(els['rc-priv'].value, '800080');
	els = fakeEnv([['rc-priv-b1', '04'], ['rc-priv-b2', '80']]);
	updateRcPriv();
	assert.strictEqual(els['rc-priv'].value, '048000');
	els = fakeEnv([['rc-priv-b1', '80'], ['rc-priv-b1', '40'], ['rc-priv-b2', '80']]);
	updateRcPriv();
	assert.strictEqual(els['rc-priv'].value, 'C08000', 'bits within a byte are OR-ed');
});

test('the install derives the privileges at send time', () => {
	const fn = extractFunc(html, 'ramInstallCap');
	const iPriv = fn.indexOf('updateRcPriv();');
	const iBody = fn.indexOf('const body = {');
	assert.ok(iPriv >= 0, 'ramInstallCap must call updateRcPriv()');
	assert.ok(iBody >= 0, 'ramInstallCap body not found');
	assert.ok(iPriv < iBody, 'the privileges must be derived before the request body');
});

test('the form resets the restored grants on load', () => {
	assert.ok(/function ramResetGrants\(\)/.test(html), 'ramResetGrants is missing');
	assert.ok(/querySelectorAll\('\.rc-priv-b1, \.rc-priv-b2, \.rc-priv-b3'\)/.test(html),
		'the privilege checkboxes must be cleared');
	assert.ok(/chainInit\('chain-ram'\);\s*\n\s*ramResetGrants\(\);/.test(html),
		'ramResetGrants must run on load');
	assert.ok(/getElementById\('rc-toolkit-enable'\)\.checked = false;/.test(html),
		'the toolkit enable must be cleared');
	assert.ok(/getElementById\('rc-tk-fsaccess'\)\.checked = false;/.test(html),
		'the file-access grant must be cleared');
});
