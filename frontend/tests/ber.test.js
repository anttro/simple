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

class StubEl {
	constructor(value = '') {
		this.value = value;
		this.checked = false;
		this.disabled = false;
		this.children = [];
		this.parentElement = null;
		this.nextElementSibling = null;
	}
	querySelector(sel) {
		return this._find(sel);
	}
	querySelectorAll(sel) {
		const out = [];
		this._findAll(sel, out);
		return out;
	}
	_find(sel) {
		for (const c of this.children) {
			if (c._matches(sel)) return c;
			const r = c._find(sel);
			if (r) return r;
		}
		return null;
	}
	_findAll(sel, out) {
		for (const c of this.children) {
			if (c._matches(sel)) out.push(c);
			c._findAll(sel, out);
		}
	}
	_matches(sel) {
		if (sel.startsWith('.')) return this.cls === sel.slice(1);
		if (sel.startsWith('input[name="')) {
			const m = sel.match(/^input\[name="([^"]+)"\]\[value="([^"]+)"\]$/);
			if (m) return this.name === m[1] && this.value === m[2];
			const m2 = sel.match(/^input\[name="([^"]+)"\]$/);
			if (m2) return this.name === m2[1];
		}
		return false;
	}
}

function el(cls, value = '') {
	const e = new StubEl(value);
	e.cls = cls;
	return e;
}

function buildRow() {
	const row = new StubEl();
	row.cls = 'ber-row';
	row.dataset = { berIdx: '0' };
	const body = el('error-action-body');
	const type = el('error-action-type', 'proactive');
	type.nextElementSibling = body;
	body.parentElement = row;
	row.children.push(type, body);
	return row;
}

function buildPcHtml(extraFields) {
	// Build the .ber-pc tree expected by genBerPcValue
	const pc = el('ber-pc');
	const type = el('ber-pc-type', 'display');
	const num = el('ber-pc-num', '1');
	const qual = el('ber-pc-qual', '01');
	const src = el('ber-pc-src', '81');
	const dst = el('ber-pc-dst', '82');
	const text = el('ber-pc-text', 'HI');
	const enc = el('ber-pc-enc', 'ucs2');
	const alpha = el('ber-pc-alpha', '');
	const durEnable = el('ber-pc-dur-enable');
	durEnable.checked = true;
	const durUnit = el('ber-pc-dur-unit', '01');
	const durVal = el('ber-pc-dur-val', '30');
	pc.children.push(type, num, qual, src, dst, text, enc, alpha, durEnable, durUnit, durVal);
	if (extraFields) extraFields(pc);
	return pc;
}

const FNS = ['berLenStr', 'gsm7TextToSeptets', 'gsm7Encode', 'genBerPcValue', 'genErrorActionValue',
	'genScriptChainingValue', 'chainKind', 'chainIsEmbedded', 'chainCommands', 'berCmdChainId', 'genBerCmdApdu', 'genBerRowValue'];
let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
const berTagsMatch = html.match(/const BER_TAGS = \{[^}]*\};/);
const berQualMatch = html.match(/const BER_QUAL = \{[\s\S]*?\n\};/);
const berDevMatch = html.match(/const BER_DEVICES = \[[\s\S]*?\];/);
const berTonesMatch = html.match(/const BER_TONES = \[[\s\S]*?\];/);
const gsm7AlphaMatch = html.match(/const GSM7_ALPHABET = \[[\s\S]*?\n\];/);
const gsm7ExtMatch = html.match(/const GSM7_EXT_MAP = \{[\s\S]*?\n\};/);
for (const m of [berTagsMatch, berQualMatch, berDevMatch, berTonesMatch, gsm7AlphaMatch, gsm7ExtMatch]) {
	if (!m) throw new Error('constant not found');
	code = m[0] + '\n' + code;
}
for (const c of ['CHAIN_CMDS_SIM', 'CHAIN_CMDS_USIM', 'CHAIN_CMDS_RAM']) {
	const m = html.match(new RegExp('const ' + c + ' = \\[[\\s\\S]*?\\n\\];'));
	if (!m) throw new Error('catalog not found: ' + c);
	code += m[0].replace('const ', 'var ') + '\n';
}
code += html.match(/const _chains = \{\};/)[0].replace('const ', 'var ') + '\n';
code += html.match(/const CHAIN_LISTENERS = \{\};/)[0].replace('const ', 'var ') + '\n';
// The embedded command chain is built by the shared command builders, which
// are covered by ts102226.test.js; here only the C-APDU row wiring matters.
code += 'function chainBuildRowHex(chainId) { return chainId === \'ber-ram-7\' ? \'80D0010101AA\' : \'\'; }\n';
code += '\nvar __berConsts = {BER_TAGS, BER_QUAL, BER_DEVICES, BER_TONES, GSM7_ALPHABET, GSM7_EXT_MAP};';
eval(code);
const {BER_TAGS, BER_QUAL, BER_DEVICES, BER_TONES, GSM7_ALPHABET, GSM7_EXT_MAP} = __berConsts;

function mkRow(type) {
	const row = new StubEl();
	row.cls = 'ber-row';
	row.dataset = { berIdx: '0' };
	const body = el('error-action-body');
	const actionType = el('error-action-type', type);
	actionType.nextElementSibling = body;
	row.children.push(actionType, body);
	return row;
}

test('BER_QUAL.refresh matches TS 102 223 §8.6 mapping', () => {
	const r = BER_QUAL.refresh;
	assert.strictEqual(r[0][0], '00');
	assert.strictEqual(r[0][1], 'NAA Initialization and Full File Change Notification');
	assert.strictEqual(r[4][0], '04');
	assert.strictEqual(r[4][1], 'UICC Reset');
	assert.strictEqual(r[5][1], 'NAA Application Reset (not for 2G SIM)');
	assert.strictEqual(r[6][1], 'NAA Session Reset (not for 2G SIM)');
	assert.strictEqual(r[7][1], 'Steering of Roaming');
	assert.strictEqual(r.length, 11);
});

test('genBerPcValue DISPLAY TEXT GSM7 "HI"', () => {
	const pc = buildPcHtml();
	pc.children.find(c => c.cls === 'ber-pc-enc').value = 'gsm7';
	pc.children.find(c => c.cls === 'ber-pc-dur-enable').checked = false;
	const val = genBerPcValue({ querySelector: (s) => s === '.ber-pc' ? pc : null }, '81');
	assert.strictEqual(val, '810E8103012101820281828D0300C824');
});

test('genBerPcValue DISPLAY TEXT UCS2 "HI" + 30s duration', () => {
	const pc = buildPcHtml();
	const val = genBerPcValue({ querySelector: (s) => s === '.ber-pc' ? pc : null }, '81');
	// 81 13 8103 01 21 01 8202 81 82 8D 04 0048 0049 84 02 01 1E
	assert.strictEqual(val, '81148103012101820281828D0508004800498402011E');
});

test('genBerPcValue duration disabled omits 84', () => {
	const pc = buildPcHtml();
	pc.children.find(c => c.cls === 'ber-pc-dur-enable').checked = false;
	const val = genBerPcValue({ querySelector: (s) => s === '.ber-pc' ? pc : null }, '81');
	assert.ok(!val.includes('84'));
});

test('genBerPcValue PLAY TONE uses type byte 20', () => {
	const pc = buildPcHtml();
	pc.children.find(c => c.cls === 'ber-pc-type').value = 'tone';
	pc.children.find(c => c.cls === 'ber-pc-qual').value = '00';
	pc.children.push(el('ber-pc-tone', '01'));
	const val = genBerPcValue({ querySelector: (s) => s === '.ber-pc' ? pc : null }, '81');
	assert.strictEqual(val, '810C8103012000820281828E0101');
});

test('genBerPcValue REFRESH uses CR-set file list tag 92', () => {
	const pc = buildPcHtml();
	pc.children.find(c => c.cls === 'ber-pc-type').value = 'refresh';
	pc.children.find(c => c.cls === 'ber-pc-qual').value = '04';
	pc.children.push(el('ber-pc-flist', '3F00'));
	const val = genBerPcValue({ querySelector: (s) => s === '.ber-pc' ? pc : null }, '81');
	assert.strictEqual(val, '810D81030101048202818292023F00');
});

test('genErrorActionValue noaction emits 82 00', () => {
	const row = mkRow('noaction');
	const val = genErrorActionValue(row, '82');
	assert.strictEqual(val, '8200');
});

test('genErrorActionValue reference emits 82 01 ref', () => {
	const row = mkRow('reference');
	row.children[1].children.push(el('error-ref', '05'));
	const val = genErrorActionValue(row, '82');
	assert.strictEqual(val, '820105');
});

test('genErrorActionValue reference rejects out-of-range ref', () => {
	const row = mkRow('reference');
	row.children[1].children.push(el('error-ref', '80'));
	assert.strictEqual(genErrorActionValue(row, '82'), '');
});

test('genErrorActionValue proactive wraps proactive command', () => {
	const row = mkRow('proactive');
	const pc = buildPcHtml();
	row.children[1].children.push(pc);
	const val = genErrorActionValue(row, '82');
	// DISPLAY TEXT UCS2 "HI" + 30s inside Error Action
	assert.strictEqual(val, '82148103012101820281828D0508004800498402011E');
});

test('genScriptChainingValue first emits 01', () => {
	const row = new StubEl();
	row.dataset = { berIdx: '0' };
	const first = el('chaining-first', 'first');
	first.name = 'chaining-0';
	first.checked = true;
	const interm = el('chaining-inter', 'intermediary');
	interm.name = 'chaining-0';
	const last = el('chaining-last', 'last');
	last.name = 'chaining-0-last';
	const keep = el('chaining-keep', 'keep');
	keep.name = 'chaining-0-keep';
	row.children.push(first, interm, last, keep, el('chaining-script-id'), el('chaining-additional'));
	const val = genScriptChainingValue(row, '83');
	assert.strictEqual(val, '830101');
});

test('genScriptChainingValue first with keep emits 11', () => {
	const row = new StubEl();
	row.dataset = { berIdx: '0' };
	const first = el('chaining-first', 'first');
	first.name = 'chaining-0';
	first.checked = true;
	const interm = el('chaining-inter', 'intermediary');
	interm.name = 'chaining-0';
	const last = el('chaining-last', 'last');
	last.name = 'chaining-0-last';
	const keep = el('chaining-keep', 'keep');
	keep.name = 'chaining-0-keep';
	keep.checked = true;
	row.children.push(first, interm, last, keep, el('chaining-script-id'), el('chaining-additional'));
	const val = genScriptChainingValue(row, '83');
	assert.strictEqual(val, '830111');
});

test('genScriptChainingValue intermediary last emits 03', () => {
	const row = new StubEl();
	row.dataset = { berIdx: '0' };
	const first = el('chaining-first', 'first');
	first.name = 'chaining-0';
	const interm = el('chaining-inter', 'intermediary');
	interm.name = 'chaining-0';
	interm.checked = true;
	const last = el('chaining-last', 'last');
	last.name = 'chaining-0-last';
	last.checked = true;
	const keep = el('chaining-keep', 'keep');
	keep.name = 'chaining-0-keep';
	row.children.push(first, interm, last, keep, el('chaining-script-id'), el('chaining-additional'));
	const val = genScriptChainingValue(row, '83');
	assert.strictEqual(val, '830103');
});

test('genScriptChainingValue no position emits empty', () => {
	const row = new StubEl();
	row.dataset = { berIdx: '0' };
	const first = el('chaining-first', 'first');
	first.name = 'chaining-0';
	const interm = el('chaining-inter', 'intermediary');
	interm.name = 'chaining-0';
	const last = el('chaining-last', 'last');
	last.name = 'chaining-0-last';
	const keep = el('chaining-keep', 'keep');
	keep.name = 'chaining-0-keep';
	row.children.push(first, interm, last, keep, el('chaining-script-id'), el('chaining-additional'));
	assert.strictEqual(genScriptChainingValue(row, '83'), '');
});

// ===== Expanded Script C-APDU rows (TS 102 226 5.2.1.0/5.2.1.1) =====

function cmdRow(mode, value) {
	const row = new StubEl();
	row.dataset = { berUid: '7' };
	const modeEl = el('ber-cmd-mode', mode);
	const hexEl = el('ber-hex', value || '');
	const typeEl = el('ber-type', 'c-apdu');
	row.children.push(typeEl, modeEl, hexEl);
	row.querySelector = sel => ({ '.ber-type': typeEl, '.ber-cmd-mode': modeEl, '.ber-hex': hexEl })[sel] || null;
	return row;
}

test('genBerCmdApdu reads pasted hex or the embedded command chain', () => {
	assert.strictEqual(genBerCmdApdu(cmdRow('hex', '80 ca ff21 00')), '80CAFF2100');
	assert.strictEqual(genBerCmdApdu(cmdRow('ram', 'ignored')), '80D0010101AA');
	assert.strictEqual(genBerCmdApdu(cmdRow('hex', '')), '');
});

test('genBerRowValue wraps a C-APDU row in the 22 command TLV', () => {
	assert.strictEqual(genBerRowValue(cmdRow('ram', '')), '220680D0010101AA');
	assert.strictEqual(genBerRowValue(cmdRow('hex', '80C AFF2100')), '220580CAFF2100');
	assert.strictEqual(genBerRowValue(cmdRow('hex', '')), '');
});

test('the embedded pickers offer the RFM/RAM command sets without GET RESPONSE', () => {
	assert.ok(chainCommands('ber-sim-1').some(c => c.value === 'select'));
	assert.ok(chainCommands('ber-usim-1').some(c => c.value === 'create-file'));
	assert.ok(chainCommands('ber-ram-1').some(c => c.value === 'push'));
	assert.ok(!chainCommands('ber-ram-1').some(c => c.value === 'get-response'));
	assert.ok(chainCommands('chain-ram').some(c => c.value === 'get-response'));
});