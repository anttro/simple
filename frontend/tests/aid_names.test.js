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

// Rewrite top-level const -> var so the tables leak out of sloppy-mode eval.
eval(extractBlock('const JC_AID_NAMES = {', 'const JC_AID_RIDS = {').replace(/^const /gm, 'var '));
eval(extractBlock('const JC_AID_RIDS = {', 'function jcAidName').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'jcAidNorm'));
eval(extractFunc(html, 'jcAidName'));
eval(extractFunc(html, 'jcAidSuffix'));
eval(extractFunc(html, 'jcAidFamily'));
eval(extractFunc(html, 'jcAidHtml'));
globalThis.esc = s => s;

test('the table resolves each standard library family', () => {
	// Oracle / JavaCard, ETSI (SIM and UICC era), 3GPP, GlobalPlatform
	assert.strictEqual(jcAidName('A0000000620101'), 'javacard.framework');
	assert.strictEqual(jcAidName('A000000062010101'), 'javacard.framework.service');
	assert.strictEqual(jcAidName('A0000000090003FFFFFFFF8910710002'), 'sim.toolkit');
	assert.strictEqual(jcAidName('A0000000090005FFFFFFFF8912000000'), 'uicc.toolkit');
	assert.strictEqual(jcAidName('A0000000871005FFFFFFFF8913200000'), 'uicc.usim.toolkit');
	assert.strictEqual(jcAidName('A00000015100'), 'org.globalplatform');
	// JAVACARD.md section 5: recognisable, not linkable libraries
	assert.strictEqual(jcAidName('A00000006203010F'), 'com.sun.javacard.crypto');
	assert.strictEqual(jcAidName('A0000000030000'), 'visa/openplatform');
	assert.strictEqual(jcAidName('123456654011'), 'com.denis');
});

test('the default ISD resolves and unknown AIDs get a RID hint only', () => {
	assert.strictEqual(jcAidName('A000000151000000'), 'GlobalPlatform Issuer Security Domain');
	assert.strictEqual(jcAidName('A0000001515350'), 'GlobalPlatform RID');
	assert.strictEqual(jcAidName('A0000000871002FFFFFFFF8907090000'), '3GPP RID');
	// no pinned name for the legacy ISD AID, and vendor AIDs stay unnamed
	assert.strictEqual(jcAidName('A000000003000000'), '');
	assert.strictEqual(jcAidName('D276000005AAFFCAFE0010'), '');
	assert.strictEqual(jcAidName('A1130001180001'), '');
});

test('the resolver normalizes case and spacing and rejects malformed input', () => {
	assert.strictEqual(jcAidName('a0 00 00 00 62 01 01'), 'javacard.framework');
	assert.strictEqual(jcAidName('a0000000620101'), 'javacard.framework');
	assert.strictEqual(jcAidName(''), '');
	assert.strictEqual(jcAidName(null), '');
	assert.strictEqual(jcAidName(undefined), '');
	assert.strictEqual(jcAidName('AB'), '');
	assert.strictEqual(jcAidName('A000000062010'), '');   // odd hex length
});

test('the plain-text and HTML suffixes name known AIDs only', () => {
	assert.strictEqual(jcAidSuffix('A0000000620101'), ' (javacard.framework)');
	assert.strictEqual(jcAidSuffix('A0000001515350'), ' (GlobalPlatform RID)');
	assert.strictEqual(jcAidSuffix('D276000005AAFFCAFE0010'), '');
	globalThis.esc = s => 'E(' + s + ')';
	assert.strictEqual(jcAidHtml('A0000000620101'),
		'E(A0000000620101) <span class="text-gray-400 dark:text-slate-500">(E(javacard.framework))</span>');
	assert.strictEqual(jcAidHtml('D276000005AAFFCAFE0010'), 'E(D276000005AAFFCAFE0010)');
	globalThis.esc = s => s;
});

test('the AID family follows the RID groups (JAVACARD.md section 6)', () => {
	assert.strictEqual(jcAidFamily('A0000000620101'), 'Oracle JavaCard API');
	assert.strictEqual(jcAidFamily('A0000000090003FFFFFFFF8910710002'), 'ETSI SIM (2G) API');
	assert.strictEqual(jcAidFamily('A0000000090005FFFFFFFF8912000000'), 'ETSI UICC API');
	assert.strictEqual(jcAidFamily('A0000000871005FFFFFFFF8913200000'), '3GPP USIM/ISIM API');
	assert.strictEqual(jcAidFamily('A00000015100'), 'GlobalPlatform API');
	// vendor/applet AIDs stay unlabelled
	assert.strictEqual(jcAidFamily('D276000005AAFFCAFE0010'), '');
	assert.strictEqual(jcAidFamily(''), '');
});

test('jcAidNorm validates and normalises', () => {
	assert.strictEqual(jcAidNorm('a0 00 00 00 62 01 01'), 'A0000000620101');
	assert.strictEqual(jcAidNorm('AB'), '');
	assert.strictEqual(jcAidNorm('A000000062010'), '');   // odd hex length
	assert.strictEqual(jcAidNorm(null), '');
});

test('the package table is well-formed', () => {
	const keys = Object.keys(JC_AID_NAMES);
	assert.ok(keys.length >= 45, 'entries: ' + keys.length);
	keys.forEach(k => {
		assert.match(k, /^[0-9A-F]{10,32}$/, k);
		assert.strictEqual(k.length % 2, 0, k);
		assert.ok(typeof JC_AID_NAMES[k] === 'string' && JC_AID_NAMES[k].length > 0, k);
	});
	Object.keys(JC_AID_RIDS).forEach(k => assert.match(k, /^[0-9A-F]{10}$/, k));
});
