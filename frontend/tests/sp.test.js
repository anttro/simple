const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const des = require('des.js');
global.des = des;
const aesjs = require('aes-js');
global.aesjs = aesjs;

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

const FNS = ['hexToBytes', 'bytesToHex', 'des3Keys', 'des3EncryptBlock', 'des3CbcEncrypt',
	'xorBytes', 'zeroPad', 'crc32Bytes', 'cbcMac', 'aesCbcEncrypt', 'aesShiftLeft1', 'aesCmacSubkeys',
	'aesCmac', '_genSpBuild', 'genSp', 'spNextCntr', 'scp80SegmentInfo', 'spSizeInfoText',
	'spShowSizeInfo'];
let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['SCP80_MAX_SMS', 'SCP80_SINGLE_BYTES', 'SCP80_FIRST_BYTES', 'SCP80_NEXT_BYTES']) {
	code += html.match(new RegExp('const ' + c + ' = \\d+;'))[0].replace('const ', 'var ') + '\n';
}
code += 'function t(s){return s;}\n';

// All test vectors are computed with the synthetic dummy key material below
// (no live/sample card keys, no ICCIDs). They are cross-checked byte-for-byte
// against pySim's OtaDialectSms.encode_cmd reference implementation.
const K = '00112233445566778899AABBCCDDEEFF';

const DEFAULTS = {
	'sp-apdu': '00A40000023F00',
	'sp-spi1': '06',
	'sp-spi2-hex': '09',
	'sp-kic-hex': '15',
	'sp-kid-hex': '15',
	'sp-tar': 'B00000',
	'sp-cntr': '0000000001',
	'sp-kic-key': K,
	'sp-kid-key': K,
	'sp-padding': '00',
};

const values = {};
const elements = {};
global.document = {
	getElementById(id) {
		if (!elements[id]) elements[id] = { value: values[id] || '' };
		return elements[id];
	},
};

eval(code);

function run() {
	genSp();
	return (elements['sp-result'] || { value: '' }).value;
}

function makeRun(overrides) {
	for (const [id, v] of Object.entries(DEFAULTS)) {
		values[id] = v;
		if (elements[id]) elements[id].value = v;
	}
	for (const [id, v] of Object.entries(overrides || {})) {
		values[id] = v;
		if (elements[id]) elements[id].value = v;
	}
	return run();
}

test('ciphered + CC SPI 06/09', () => {
	assert.strictEqual(
		makeRun({}),
		'00201506091515B00000C08F58C38860ACB3A362FFFE670AD13759A2A6B4C1A91116');
});

test('ciphered + CC SPI 16/01 (counter_must_be_higher, plaintext PoR)', () => {
	assert.strictEqual(
		makeRun({ 'sp-spi1': '16', 'sp-spi2-hex': '01' }),
		'00201516011515B00000E42573469E68A8462A57A505B0E2B1C09C1928C7A182311F');
});

test('unciphered + CC SPI 02/09 carries the CPL', () => {
	const out = makeRun({ 'sp-spi1': '02', 'sp-spi2-hex': '09' });
	assert.strictEqual(out,
		'001D1502091515B0000000000000010085A8CA1A9828B0BB00A40000023F00');
	assert.strictEqual(parseInt(out.slice(0, 4), 16), out.length / 2 - 2);
});

test('unprotected packet (SPI 00) keeps the pySim CHL-first form', () => {
	assert.strictEqual(
		makeRun({ 'sp-spi1': '00', 'sp-spi2-hex': '09' }),
		'0D00091515B0000000000000010000A40000023F00');
});

test('unprotected concatenated packet gains the CPL (TS 31.115 4.3)', () => {
	const out = makeRun({ 'sp-spi1': '00', 'sp-spi2-hex': '09', 'sp-apdu': 'A0'.repeat(200) });
	assert.strictEqual(out.length / 2, 216);
	assert.strictEqual(parseInt(out.slice(0, 4), 16), 214);
	assert.strictEqual(out.slice(4, 6), '0D');
	assert.strictEqual(scp80SegmentInfo(out).segments, 2);
});

test('RC (SPI 01) computes CRC-32 over the CPL frame', () => {
	assert.strictEqual(
		makeRun({ 'sp-spi1': '01', 'sp-spi2-hex': '09' }),
		'00191101091515B0000000000000010050C942DC00A40000023F00');
});

test('DS (SPI 03) is refused instead of building an unsigned packet', () => {
	assert.match(makeRun({ 'sp-spi1': '03' }), /Digital Signature/);
});

test('crc32Bytes known answer (TS 102 225 Annex B)', () => {
	assert.strictEqual(bytesToHex(crc32Bytes(hexToBytes('0102030405'))), '470B99F4');
});

test('sysmocom public reference vector (spi1 04 / spi2 19, cntr=0)', () => {
	assert.strictEqual(
		makeRun({
			'sp-spi1': '04',
			'sp-spi2-hex': '19',
			'sp-kic-hex': '35',
			'sp-kid-hex': '35',
			'sp-cntr': '0000000000',
			'sp-kic-key': 'C21DD66ACAC13CB3BC8B331B24AFB57B',
			'sp-kid-key': '12110C78E678C25408233076AA033615',
		}),
		'00180D04193535B00000E3EC80A849B554421276AF3883927C20');
});

test('missing APDU reports an error', () => {
	assert.strictEqual(makeRun({ 'sp-apdu': '' }), 'Error: specify APDU');
});

test('cbcMac known answer (synthetic key)', () => {
	const input = hexToBytes('001d1502091515b0000000000000010000a40000023f00');
	assert.strictEqual(bytesToHex(cbcMac(input, hexToBytes(K))), '85A8CA1A9828B0BB');
});

// Public synthetic AES keys from pySim tests/unittests/test_ota.py (no live keys).
const KIC_AES = '200102030405060708090a0b0c0d0e0f';
const KID_AES = '201102030405060708090a0b0c0d0e0f';

test('aesCmac known answer (NIST SP 800-38B, truncated to 8)', () => {
	const key = hexToBytes('2b7e151628aed2a6abf7158809cf4f3c');
	assert.strictEqual(bytesToHex(aesCmac(new Uint8Array(0), key)), 'BB1D6929E9593728');
	assert.strictEqual(
		bytesToHex(aesCmac(hexToBytes('6bc1bee22e409f96e93d7e117393172a'), key)),
		'070A16B46B4D4144');
	assert.strictEqual(
		bytesToHex(aesCmac(hexToBytes('6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411'), key)),
		'DFA66747DE9AE630');
});

test('AES ciphered + CC SPI 16/19 (counter higher)', () => {
	assert.strictEqual(
		makeRun({
			'sp-apdu': '00A40004023F00',
			'sp-spi1': '16',
			'sp-spi2-hex': '19',
			'sp-kic-hex': '22',
			'sp-kid-hex': '22',
			'sp-tar': 'B00011',
			'sp-cntr': '0000000011',
			'sp-kic-key': KIC_AES,
			'sp-kid-key': KID_AES,
		}),
		'00281516192222B000115A47655527E96E832F1A5C698655715D4331454A0D83952C0ED35245706976B1');
});

test('AES unciphered + CC SPI 12/09 (counter higher)', () => {
	assert.strictEqual(
		makeRun({
			'sp-apdu': '00A40004023F00',
			'sp-spi1': '12',
			'sp-spi2-hex': '09',
			'sp-kic-hex': '22',
			'sp-kid-hex': '22',
			'sp-tar': 'B00011',
			'sp-cntr': '0000000011',
			'sp-kic-key': KIC_AES,
			'sp-kid-key': KID_AES,
		}),
		'001D1512092222B0001100000000110029826122C7A0B79500A40004023F00');
});

test('AES ciphered + CC SPI 1E/19 (counter +1)', () => {
	assert.strictEqual(
		makeRun({
			'sp-apdu': '00A40004023F00',
			'sp-spi1': '1E',
			'sp-spi2-hex': '19',
			'sp-kic-hex': '22',
			'sp-kid-hex': '22',
			'sp-tar': 'B00011',
			'sp-cntr': '0000000011',
			'sp-kic-key': KIC_AES,
			'sp-kid-key': KID_AES,
		}),
		'0028151E192222B0001118B202EE47A3203E7370861C383B4142E704157B36E5C0EB4BB33EB6036CBAF8');
});

test('AES rejects no_counter (SPI1 b5b4 = 00)', () => {
	const err = makeRun({
		'sp-apdu': '00A40004023F00',
		'sp-spi1': '06',
		'sp-spi2-hex': '19',
		'sp-kic-hex': '22',
		'sp-kid-hex': '22',
		'sp-tar': 'B00011',
		'sp-cntr': '0000000011',
		'sp-kic-key': KIC_AES,
		'sp-kid-key': KID_AES,
	});
	assert.ok(err.startsWith('Error: AES requires a replay-protected counter'));
});

test('AES rejects 8-byte key', () => {
	const err = makeRun({
		'sp-apdu': '00A40004023F00',
		'sp-spi1': '16',
		'sp-spi2-hex': '19',
		'sp-kic-hex': '22',
		'sp-kid-hex': '22',
		'sp-tar': 'B00011',
		'sp-cntr': '0000000011',
		'sp-kic-key': '0011223344556677',
		'sp-kid-key': KID_AES,
	});
	assert.strictEqual(err, 'Error: AES KIc key must be 16, 24, or 32 bytes');
});

test('spNextCntr increments with carry', () => {
	assert.strictEqual(spNextCntr('0000000001'), '0000000002');
	assert.strictEqual(spNextCntr('00000000FF'), '0000000100');
	assert.strictEqual(spNextCntr('000000FFFF'), '0000010000');
	assert.strictEqual(spNextCntr('0000ABCDEF'), '0000ABCDF0');
});

test('spNextCntr tolerates lower case and separators', () => {
	assert.strictEqual(spNextCntr('00000000 0a'), '000000000B');
	assert.strictEqual(spNextCntr(''), '0000000001');
});

test('scp80SegmentInfo mirrors the server segmentation rules', () => {
	assert.deepStrictEqual(scp80SegmentInfo(''), { bytes: 0, segments: 0 });
	assert.deepStrictEqual(scp80SegmentInfo('AA'.repeat(137)), { bytes: 137, segments: 1 });
	assert.deepStrictEqual(scp80SegmentInfo('AA'.repeat(138)), { bytes: 138, segments: 2 });
	assert.deepStrictEqual(scp80SegmentInfo('AA'.repeat(132 + 134)), { bytes: 266, segments: 2 });
	assert.deepStrictEqual(scp80SegmentInfo('AA'.repeat(132 + 134 * 2)), { bytes: 400, segments: 3 });
});

test('spSizeInfoText reports size, SMS count and the card buffer limit', () => {
	assert.strictEqual(spSizeInfoText('AA'.repeat(18)), '18 bytes · 1 SMS');
	assert.strictEqual(spSizeInfoText('AA'.repeat(266)), '266 bytes · 2 SMS (concatenated)');
	assert.match(spSizeInfoText('AA'.repeat(132 + 134 * 5)),
		/too large for the card concatenation buffer \(5 SMS\)/);
	assert.strictEqual(spSizeInfoText(''), '');
});

test('the SCP80 UI constants match the server segmentation constants', () => {
	const py = fs.readFileSync(
		path.join(__dirname, '..', '..', 'pysim_simple_server', 'server.py'), 'utf8');
	const read = (name) => {
		const m = new RegExp('^' + name + '\\s*=\\s*(\\d+)', 'm').exec(py);
		assert.ok(m, name + ' not found in server.py');
		return parseInt(m[1], 10);
	};
	assert.strictEqual(SCP80_SINGLE_BYTES, read('SCP80_SINGLE_BYTES'));
	assert.strictEqual(SCP80_FIRST_BYTES, read('SCP80_FIRST_BYTES'));
	assert.strictEqual(SCP80_NEXT_BYTES, read('SCP80_NEXT_BYTES'));
	assert.strictEqual(SCP80_MAX_SMS, read('MAX_ENVELOPE_SEGMENTS'));
});

test('a generated single-SMS packet shows its size and SMS count', () => {
	const pkt = makeRun({});
	const info = elements['sp-size-info'] || {};
	assert.strictEqual(info.textContent, (pkt.length / 2) + ' bytes · 1 SMS');
});
