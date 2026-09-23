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

// Extract chain builder functions and dependencies
const FNS = ['buildApdu', 'buildSelect', 'escHtml', 'chainInit', 'chainKind', 'chainSimBuildRowHex'];
let code = '';
for (const f of FNS) {
	code += extractFunc(html, f) + '\n';
}
// Also extract _chains
const m = html.match(/const _chains = \{\};/);
if (m) code += m[0].replace(/^const /, 'var ') + '\n';
eval(code);

function setup(mode, opts) {
	const chainId = mode === 'sim' ? 'chain-sim' : 'chain-usim';
	_chains[chainId] = { rows: [] };

	const cmd = opts.cmd;
	const fields = {};

	if (cmd === 'select') {
		fields.method = opts.selMethod || 'fid';
		if (opts.fid) fields.fid = opts.fid;
		if (opts.path) fields.path = opts.path;
		if (opts.dfname) fields.dfname = opts.dfname;
		if (opts.chain) fields.chain = opts.chain;
		if (opts.silent) fields.silent = opts.silent;
		if (opts.base) fields.base = opts.base;
	} else if (cmd === 'read-record') {
		fields.record = opts.record || '1';
		fields.recMode = opts.recMode || '04';
		fields.le = opts.le || '00';
	} else if (cmd === 'read-binary') {
		fields.offset = opts.offset || '0000';
		fields.le = opts.le || '00';
	} else if (cmd === 'update-record') {
		fields.record = opts.record || '1';
		fields.recMode = opts.recMode || '04';
		fields.data = opts.data || '';
	} else if (cmd === 'update-binary') {
		fields.offset = opts.offset || '0000';
		fields.data = opts.data || '';
	} else if (cmd === 'erase-binary') {
		fields.offset = opts.offset || '0000';
	} else if (cmd === 'activate-file' || cmd === 'deactivate-file') {
		fields.target = opts.actTarget || 'current';
		fields.file = opts.actFile || '';
	} else if (cmd === 'verify' || cmd === 'disable' || cmd === 'enable') {
		fields.pin = opts.pin || '01';
		fields.pinVal = opts.pinVal || '';
	} else if (cmd === 'change' || cmd === 'unblock') {
		fields.pin = opts.pin || '01';
		fields.pinOld = opts.pinOld || '';
		fields.pinNew = opts.pinNew || '';
	}

	_chains[chainId].rows.push({ cmd: cmd, fields: fields });

	if (opts.doSelect) {
		const selFields = {};
		selFields.method = opts.selMethod || 'fid';
		if (opts.fid) selFields.fid = opts.fid;
		if (opts.path) selFields.path = opts.path;
		if (opts.dfname) selFields.dfname = opts.dfname;
		if (opts.chain) selFields.chain = opts.chain;
		if (opts.silent) selFields.silent = opts.silent;
		if (opts.base) selFields.base = opts.base;
		_chains[chainId].rows.unshift({ cmd: 'select', fields: selFields });
	}

	const rowIdx = opts.doSelect ? 1 : 0;
	return chainSimBuildRowHex(chainId, rowIdx, _chains[chainId].rows[rowIdx]);
}

function setupMulti(mode, rows) {
	const chainId = mode === 'sim' ? 'chain-sim' : 'chain-usim';
	_chains[chainId] = { rows: [] };
	for (const r of rows) {
		_chains[chainId].rows.push({ cmd: r.cmd, fields: r.fields || {} });
	}
	let hex = '';
	for (let i = 0; i < _chains[chainId].rows.length; i++) {
		hex += chainSimBuildRowHex(chainId, i, _chains[chainId].rows[i]);
	}
	return hex;
}

test('VERIFY PIN FF-pads to 8 bytes (Lc=08)', () => {
	const apdu = setup('usim', { cmd: 'verify', pin: '01', pinVal: '1234' });
	assert.strictEqual(apdu, '002000010831323334FFFFFFFF');
});

test('CHANGE PIN emits two 8-byte fields (Lc=10)', () => {
	const apdu = setup('usim', { cmd: 'change', pin: '01', pinOld: '1234', pinNew: '5678' });
	assert.strictEqual(apdu, '002400011031323334FFFFFFFF35363738FFFFFFFF');
});

test('DISABLE PIN single padded PIN (INS 26)', () => {
	const apdu = setup('usim', { cmd: 'disable', pin: '02', pinVal: '9999' });
	assert.strictEqual(apdu, '002600020839393939FFFFFFFF');
});

test('ENABLE PIN single padded PIN (INS 28)', () => {
	const apdu = setup('usim', { cmd: 'enable', pin: '01', pinVal: '1111' });
	assert.strictEqual(apdu, '002800010831313131FFFFFFFF');
});

test('UNBLOCK PIN two padded PINs (INS 2C)', () => {
	const apdu = setup('usim', { cmd: 'unblock', pin: '02', pinOld: '12345678', pinNew: '4321' });
	assert.strictEqual(apdu, '002C000210313233343536373834333231FFFFFFFF');
});

test('ACTIVATE FILE case-1 (no data, no Le)', () => {
	const apdu = setup('usim', { cmd: 'activate-file' });
	assert.strictEqual(apdu, '00440000');
});

test('DEACTIVATE FILE case-1', () => {
	const apdu = setup('sim', { cmd: 'deactivate-file' });
	assert.strictEqual(apdu, 'A0040000');
});

test('ACTIVATE FILE by FID (P1=00 + Lc/FID)', () => {
	const apdu = setup('usim', { cmd: 'activate-file', actTarget: 'fid', actFile: '6F3B' });
	assert.strictEqual(apdu, '00440000026F3B');
});

test('ACTIVATE FILE by path from MF (P1=08)', () => {
	const apdu = setup('usim', { cmd: 'activate-file', actTarget: 'mfpath', actFile: '7FFF6FC5' });
	assert.strictEqual(apdu, '00440800047FFF6FC5');
});

test('DEACTIVATE FILE by path from current DF (P1=09, SIM CLA)', () => {
	const apdu = setup('sim', { cmd: 'deactivate-file', actTarget: 'dfpath', actFile: '6F3B' });
	assert.strictEqual(apdu, 'A0040900026F3B');
});

test('READ RECORD next mode forces P1=00', () => {
	const apdu = setup('usim', { cmd: 'read-record', recMode: '02', le: '20' });
	assert.strictEqual(apdu, '00B2000220');
});

test('SELECT by FID USIM: P2=04 requests FCP, Le=00', () => {
	const apdu = setup('usim', { cmd: 'select', selMethod: 'fid', fid: '6FC5' });
	assert.strictEqual(apdu, '00A40004026FC500');
});

test('USIM chain: every hop requests FCP (04 + Le)', () => {
	const apdu = setup('usim', { cmd: 'select', selMethod: 'chain', chain: '3F00,2FE2' });
	assert.strictEqual(apdu, '00A40004023F000000A40004022FE200');
});

test('SIM chain with GET RESPONSE hop', () => {
	const apdu = setup('sim', { cmd: 'select', selMethod: 'chain', chain: '3F00,2FE2,C0' });
	assert.strictEqual(apdu, 'A0A40000023F00A0A40000022FE2A0C0000000');
});

test('GET RESPONSE hop with explicit Le (C0:NN)', () => {
	const apdu = setup('usim', { cmd: 'select', selMethod: 'chain', chain: 'C0:0F' });
	assert.strictEqual(apdu, '00C000000F');
});

test('silent path select + READ RECORD keeps full CLA (live PNN read)', () => {
	const chainId = 'chain-usim';
	_chains[chainId] = { rows: [
		{ cmd: 'select', fields: { method: 'path', path: '6FC5', base: 'df', silent: true } },
		{ cmd: 'read-record', fields: { record: '1', recMode: '04', le: '14' } },
	]};
	let apdu = '';
	for (let i = 0; i < _chains[chainId].rows.length; i++) {
		apdu += chainSimBuildRowHex(chainId, i, _chains[chainId].rows[i]);
	}
	assert.strictEqual(apdu, '00A4090C026FC500B2010414');
});

test('select + READ BINARY emits explicit CLA on second command', () => {
	const chainId = 'chain-usim';
	_chains[chainId] = { rows: [
		{ cmd: 'select', fields: { method: 'path', path: '6FC5', base: 'df', silent: true } },
		{ cmd: 'read-binary', fields: { offset: '0000', le: '0A' } },
	]};
	let apdu = '';
	for (let i = 0; i < _chains[chainId].rows.length; i++) {
		apdu += chainSimBuildRowHex(chainId, i, _chains[chainId].rows[i]);
	}
	assert.strictEqual(apdu, '00A4090C026FC500B000000A');
});

test('USIM silent path-from-current-DF hop (live RFM idiom 09/0C)', () => {
	const apdu = setup('usim', { cmd: 'select', selMethod: 'path', path: '6F46', base: 'df', silent: true });
	assert.strictEqual(apdu, '00A4090C026F46');
});

test('USIM silent FID select (P2=0C, no Le)', () => {
	const apdu = setup('usim', { cmd: 'select', selMethod: 'fid', fid: '6FC5', silent: true });
	assert.strictEqual(apdu, '00A4000C026FC5');
});

test('SELECT by path USIM: P2=04, Le=00', () => {
	const apdu = setup('usim', { cmd: 'select', selMethod: 'path', path: '7FFF6FC5' });
	assert.strictEqual(apdu, '00A40804047FFF6FC500');
});

test('SELECT by FID SIM (CLA A0): no Le anywhere', () => {
	const apdu = setup('sim', { cmd: 'select', selMethod: 'fid', fid: '6FC5' });
	assert.strictEqual(apdu, 'A0A40000026FC5');
});
