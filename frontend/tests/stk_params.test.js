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
for (const fn of ['berLenStr', 'stkParamsBuild', 'buildRcToolkitParams',
	'updateStkParamsHex', 'updateInstallParamsHex', 'adfAidFromFci', 'ramQuotaValue', 'ramQuotaTlvs',
	'composeRamInstallParams', 'ramInstallParamsCompose',
	'parseTlvList', 'parseBerLen']) code += extractFunc(html, fn) + '\n';
eval(code);

const base = { mode: 'ea', priority: '0', timers: '0', textLen: '0', menus: '0',
	firstPos: '0', firstId: '00', lastPos: '0', lastId: '00',
	channels: '0', msl: '00', tar: '', ad: '', services: '0' };
function vals(over) { return Object.assign({}, base, over); }

// ===== install-parameter coding (TS 102 226 8.2.1.3.2.2.1) =====

test('UICC (EA) parameters match the decrypted live packet', () => {
	// The install parameters of the live CAP install (decrypted):
	//   EA 0F | 80 0D <prio 0 timers 0 textLen 0 menus 0> <channels 1>
	//   <MSL len 2 / 01 12> <TAR len 3 / AF 4D 01> <services 0>
	assert.strictEqual(
		stkParamsBuild(vals({ channels: '1', msl: '12', tar: 'AF4D01' })),
		'EA0F800D000000000102011203AF4D0100');
});

test('empty TAR emits a zero-length TAR field, never a B00001 default', () => {
	// B0 00 01 is the allocated ADF RFM TAR (TS 101 220 Annex D), not an
	// applet TAR: the field stays empty and the TAR Value(s) length is 00
	// (TS 102 226 8.2.1.3.2.7 - the card may then use a TAR from the AID,
	// but only when the AID carries one).
	const out = stkParamsBuild(vals({}));
	assert.strictEqual(out, 'EA0A80080000000000000000');
	assert.ok(!out.includes('B00001'), out);
});

test('SIM (CA) parameters carry the access domain first and no services byte', () => {
	assert.strictEqual(stkParamsBuild(vals({ mode: 'ca' })),
		'EF0ACA080000000000000000');
	assert.strictEqual(stkParamsBuild(vals({ mode: 'ca', ad: '00' })),
		'EF0BCA09010000000000000000');
});

test('menu item pairs run first .. last with 0000 fillers between', () => {
	assert.strictEqual(stkParamsBuild(vals({ menus: '1', firstId: '7F' })),
		'EA0C800A00000001007F00000000');
	const out = stkParamsBuild(vals({ menus: '3', firstPos: '1', firstId: '01',
		lastPos: '3', lastId: '03' }));
	assert.ok(out.includes('010100000303'), out);
});

test('long menu lists use BER long-form lengths', () => {
	const out = stkParamsBuild(vals({ menus: '60', firstPos: '1', firstId: '01',
		lastPos: '60', lastId: '3C', msl: '16', tar: 'AF4D01' }));
	assert.ok(out.startsWith('EA8188808185'), out);
});

test('rejects a TAR that is not a whole number of 3-byte values', () => {
	assert.strictEqual(stkParamsBuild(vals({ tar: 'AF4D' })), null);
	assert.strictEqual(stkParamsBuild(vals({ tar: 'AF4D0' })), null);
	assert.strictEqual(stkParamsBuild(vals({ tar: 'AF4D0102' })), null);
	// several TAR values are allowed (3 bytes each)
	assert.ok(stkParamsBuild(vals({ tar: 'AF4D01AFFA01' })).includes('06AF4D01AFFA01'));
});

test('rejects menu item identifiers above 7F (reserved for the toolkit framework)', () => {
	assert.strictEqual(stkParamsBuild(vals({ menus: '1', firstId: '80' })), null);
	assert.strictEqual(stkParamsBuild(vals({ menus: '1', lastId: 'FF' })), null);
});

// ===== form wiring: the STK settings must reach the sent hex =====

test('every toolkit field regenerates the STK parameters hex on edit', () => {
	// v3.6.9: the install used to send the hex computed once when the
	// checkbox was ticked - edits to the fields afterwards were dropped
	// (the live install carried the defaults, not the entered TAR).
	const block = html.slice(html.indexOf('id="rc-toolkit-body"'),
		html.indexOf('id="ram-install-params-hex"'));
	const re = /<(?:input|select)\b[^>]*id="(rc-tk-[^"]+)"[^>]*>/g;
	const seen = [];
	let m;
	while ((m = re.exec(block))) {
		seen.push(m[1]);
		assert.ok(/on(?:input|change)="[^"]*updateStkParamsHex/.test(m[0]),
			m[1] + ' does not refresh the hex: ' + m[0]);
	}
	assert.strictEqual(seen.length, 17, 'expected 17 toolkit fields, got ' + seen.join(', '));
});

test('the applet TAR field has no B00001 default or placeholder', () => {
	const m = /<input id="rc-tk-tar"[^>]*>/.exec(html);
	assert.ok(m, 'rc-tk-tar not found');
	assert.ok(!/value="B00001"/.test(m[0]), m[0]);
	assert.ok(!/placeholder="B00001"/.test(m[0]), m[0]);
	assert.ok(/oninput="updateStkParamsHex\(\)"/.test(m[0]), m[0]);
	assert.ok(!html.includes("f.tkTar || 'B00001'"), 'the chain toolkit default must stay empty');
});

test('adfAidFromFci extracts the DF name (tag 84) from an FCI', () => {
	// the live ADF.USIM FCI (read from the card, 2026-09-28): the DF name is
	// 12 bytes - the base A0000000871002 is only a prefix, and a shorter AID
	// makes the card reject the access entry with 6A80.
	assert.strictEqual(
		adfAidFromFci('622882027821840CA0000000871002FF49FF05898A01058B032F0617C60C90016083010183010A830181'),
		'A0000000871002FF49FF0589');
	// an FCI without a DF name, and garbage
	assert.strictEqual(adfAidFromFci('6207820278218A0105'), '');
	assert.strictEqual(adfAidFromFci(''), '');
	assert.strictEqual(adfAidFromFci('ZZZZ'), '');
});

test('composeRamInstallParams composes C9 + EF (C7/C8) like the vendor scripts', () => {
	// The vendor's working install (samples/uicc/applets/STK3):
	//   C9 22 <config>  EF 08 C7 02 0000 C8 02 0000  EA ...
	// C9 is mandatory (GP Table 11-49): an empty field is sent as C9 00 -
	// without it the card rejects the data field with 6A80 (live 2026-09-28).
	assert.strictEqual(composeRamInstallParams('', '', '', '', '').install_params, 'C900');
	assert.strictEqual(
		composeRamInstallParams('082905112000012066', '', '', '', '').install_params,
		'C909082905112000012066');
	assert.strictEqual(composeRamInstallParams('', '0', '0', '', '').install_params,
		'C900EF08C7020000C8020000');
	assert.strictEqual(composeRamInstallParams('', '32768', '', '', '').install_params,
		'C900EF06C70400008000');
	assert.strictEqual(composeRamInstallParams('AA', '', '1234', '', '').install_params,
		'C901AAEF04C80204D2');
	// extra raw TLVs follow the generated prefix
	assert.strictEqual(composeRamInstallParams('', '', '', 'CB02AABB', '').install_params,
		'C900CB02AABB');
});

test('an EF-form STK part takes the quotas into its own EF (one EF, TS 102 226 8.2.1.3.2.1)', () => {
	// Two EFs made the card read the quotas and ignore the CA (live
	// 2026-10-05: por_ok but no STK menu): the CA and the quotas share one
	// System Specific Parameters EF (GPD_SPE_013 Table 6-5).
	const ca = stkParamsBuild(vals({ mode: 'ca' }));      // EF0ACA08...
	assert.strictEqual(ca, 'EF0ACA080000000000000000');
	const c = composeRamInstallParams('', '100', '', '', ca);
	assert.strictEqual(c.install_params, 'C900');
	assert.strictEqual(c.stk_params, 'EF0EC7020064CA080000000000000000');
	assert.strictEqual(c.quota_in_stk, true);
	// without quotas the EF passes through unchanged
	const c2 = composeRamInstallParams('', '', '', '', ca);
	assert.strictEqual(c2.install_params, 'C900');
	assert.strictEqual(c2.stk_params, ca);
	assert.strictEqual(c2.quota_in_stk, false);
	// an EA-form STK part is a sibling of EF: the quotas keep their own EF
	const ea = stkParamsBuild(vals({ mode: 'ea' }));
	const c3 = composeRamInstallParams('', '100', '', '', ea);
	assert.strictEqual(c3.install_params, 'C900EF04C7020064');
	assert.strictEqual(c3.stk_params, ea);
	assert.strictEqual(c3.quota_in_stk, false);
});

test('the RAM form composes the install parameters from its fields', () => {
	const els = fakeForm({ 'rc-c9': 'AA', 'rc-quota-c7': '0' });
	assert.deepStrictEqual(ramInstallParamsCompose(),
		{ install_params: 'C901AAEF04C7020000', stk_params: '', quota_in_stk: false });
	els['rc-quota-c8'].value = '5';
	assert.strictEqual(ramInstallParamsCompose().install_params, 'C901AAEF08C7020000C8020005');
	// a CA-form STK field takes the quotas into its EF
	els['ram-stk-params'].value = 'EF0ACA080000000000000000';
	const c = ramInstallParamsCompose();
	assert.strictEqual(c.install_params, 'C901AA');
	assert.strictEqual(c.stk_params, 'EF12C7020000C8020005CA080000000000000000');
	assert.strictEqual(c.quota_in_stk, true);
});

test('updateStkParamsHex names the rejection reason', () => {
	globalThis.t = s => s;
	const els = fakeForm({ 'rc-toolkit-enable': true, 'rc-tk-mode': 'ea',
		'rc-tk-tar': 'AF4D' });   // not a multiple of 3 bytes
	updateStkParamsHex();
	assert.strictEqual(els['ram-stk-params'].value, '');
	assert.ok(els['ram-stk-hint'].textContent.includes('STK parameters not generated'),
		els['ram-stk-hint'].textContent);
	// a valid form clears the hint
	els['rc-tk-tar'].value = 'AF4D01';
	updateStkParamsHex();
	assert.ok(els['ram-stk-params'].value.startsWith('EA'), els['ram-stk-params'].value);
	assert.strictEqual(els['ram-stk-hint'].textContent, '');
	delete globalThis.t;
});

test('the install body composes the C9/quota prefix', () => {
	const fn = extractFunc(html, 'ramInstallCap');
	assert.ok(/const composed = ramInstallParamsCompose\(\)/.test(fn), fn);
	assert.ok(/install_params: composed\.install_params/.test(fn), fn);
	assert.ok(/stk_params: composed\.stk_params/.test(fn), fn);
	const app = extractFunc(html, 'ramInstallApp');
	assert.ok(/const composed = ramInstallParamsCompose\(\)/.test(app), app);
	assert.ok(/install_params: composed\.install_params/.test(app), app);
});

test('the ADF AID From-card button is wired', () => {
	assert.ok(/id="rc-tk-adfaid-from-card"[^>]*onclick="ramAdfAidFromCard\(\)"/.test(html),
		'the ADF AID From-card button is missing');
	assert.ok(/async function ramAdfAidFromCard\(\)/.test(html), 'ramAdfAidFromCard is missing');
	assert.ok(/pysimFetch\('\/api\/select', \{ name: 'ADF.USIM' \}\)/.test(html),
		'the button must read the ADF.USIM FCI');
});

test('the RAM install recomputes the STK hex at send time unless hand-edited', () => {
	assert.ok(/oninput="this\.dataset\.manual='1'"/.test(html),
		'the hex field must flag manual edits');
	assert.ok(/rc-toolkit-enable'\)\.checked && !\(stkEl\.dataset && stkEl\.dataset\.manual\)/.test(html),
		'the send-time recompute guard is missing');
});

// ===== form -> hex (the live regression) =====

function fakeForm(values) {
	const ids = ['rc-toolkit-enable', 'rc-tk-mode', 'rc-tk-priority', 'rc-tk-timers',
		'rc-tk-textlen', 'rc-tk-menus', 'rc-tk-firstpos', 'rc-tk-firstid',
		'rc-tk-lastpos', 'rc-tk-lastid', 'rc-tk-channels', 'rc-tk-msl',
		'rc-tk-tar', 'rc-tk-ad', 'rc-tk-services', 'rc-tk-fsaccess',
		'rc-tk-adfaccess', 'rc-tk-adfaid', 'rc-c9', 'rc-quota-c7',
		'rc-quota-c8', 'ram-stk-params', 'ram-stk-hint'];
	const checks = ['rc-toolkit-enable', 'rc-tk-fsaccess', 'rc-tk-adfaccess'];
	const els = {};
	for (const id of ids) els[id] = { value: '', checked: false, dataset: {},
		classList: { add() {}, remove() {} } };
	for (const [id, v] of Object.entries(values || {})) {
		if (checks.indexOf(id) >= 0) els[id].checked = !!v;
		else els[id].value = v;
	}
	globalThis.document = { getElementById: id => els[id] || null };
	return els;
}

test('the RAM form fields build the live install parameters end to end', () => {
	// The reported regression: the values were entered after the toolkit
	// checkbox was ticked and never reached the sent hex - the install
	// carried TAR B00001 / MSL 16 / channels 0 and the card answered 6A80.
	fakeForm({ 'rc-toolkit-enable': true, 'rc-tk-mode': 'ea', 'rc-tk-msl': '12',
		'rc-tk-tar': 'AF4D01', 'rc-tk-channels': '1' });
	assert.strictEqual(buildRcToolkitParams(), 'EA0F800DFF0000000102011203AF4D0100');
});

test('UICC file-access parameters (82) are appended in EA mode', () => {
	// TS 102 226 8.2.1.3.2.2/8.2.1.3.2.2.2: the file-access list is the
	// tag '81' field ('82' is the *Administrative* Access field); every entry
	// ends with the "Length of Access Domain DAP" byte (00 = no DAP):
	//   [FS AID len 00 = shared FS][AD len 01][ADP 00 = full][DAP len 00]
	//   [ADF AID len][ADF AID][AD len 01][ADP 00][DAP len 00]
	// The SIM path grants the same rights via the CA Access Domain field; the
	// ADF entry is an extension of the file-system entry.  Under '82' the card
	// rejected the ADF entry with 6A80 (live 2026-09-28).
	const base = vals({ channels: '1', msl: '12', tar: 'AF4D01' });
	assert.strictEqual(stkParamsBuild(Object.assign({}, base, { fsAccess: true })),
		'EA15800D000000000102011203AF4D0100810400010000');
	assert.strictEqual(
		stkParamsBuild(Object.assign({}, base, { fsAccess: true, adfAccess: true })),
		'EA20800D000000000102011203AF4D0100810F0001000007A0000000871002010000');
	assert.strictEqual(
		stkParamsBuild(Object.assign({}, base, { fsAccess: true, adfAccess: true,
			adfAid: 'A0000000871002FF33FFFF89010101' })),
		'EA28800D000000000102011203AF4D01008117000100000FA0000000871002FF33FFFF89010101010000');
	assert.strictEqual(stkParamsBuild(base), 'EA0F800D000000000102011203AF4D0100');
	assert.strictEqual(stkParamsBuild(Object.assign({}, base, { adfAccess: true })),
		'EA0F800D000000000102011203AF4D0100');
	// the ADF AID must be 5..16 bytes
	assert.strictEqual(stkParamsBuild(Object.assign({}, base,
		{ fsAccess: true, adfAccess: true, adfAid: 'A00000' })), null);
	assert.strictEqual(stkParamsBuild(Object.assign({}, base,
		{ fsAccess: true, adfAccess: true, adfAid: 'A0'.repeat(17) })), null);
});

test('the RAM form emits full file access when the checkbox is ticked', () => {
	fakeForm({ 'rc-toolkit-enable': true, 'rc-tk-mode': 'ea', 'rc-tk-msl': '12',
		'rc-tk-tar': 'AF4D01', 'rc-tk-channels': '1', 'rc-tk-fsaccess': true });
	assert.strictEqual(buildRcToolkitParams(),
		'EA15800DFF0000000102011203AF4D0100810400010000');
	fakeForm({ 'rc-toolkit-enable': true, 'rc-tk-mode': 'ea', 'rc-tk-msl': '12',
		'rc-tk-tar': 'AF4D01', 'rc-tk-channels': '1', 'rc-tk-fsaccess': true,
		'rc-tk-adfaccess': true });
	assert.strictEqual(buildRcToolkitParams(),
		'EA20800DFF0000000102011203AF4D0100810F0001000007A0000000871002010000');
});

test('updateStkParamsHex refreshes the field and clears the manual flag', () => {
	const els = fakeForm({ 'rc-toolkit-enable': true, 'rc-tk-mode': 'ea',
		'rc-tk-tar': 'AF4D01' });
	els['ram-stk-params'].dataset.manual = '1';
	updateStkParamsHex();
	assert.ok(els['ram-stk-params'].value.startsWith('EA'), els['ram-stk-params'].value);
	assert.ok(!els['ram-stk-params'].dataset.manual,
		'a regeneration supersedes a manual edit');
});
