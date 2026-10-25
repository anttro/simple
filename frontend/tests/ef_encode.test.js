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

// Pull a const's initializer by bracket depth (the schemas/registries are
// nested objects and arrays, so the line-based extraction used elsewhere does
// not work for them).
function extractConst(src, name) {
	const m = new RegExp('const ' + name + ' = ').exec(src);
	if (!m) throw new Error('const ' + name + ' not found');
	const begin = m.index + m[0].length;
	const opener = src[begin];
	const closer = opener === '[' ? ']' : (opener === '{' ? '}' : null);
	if (!closer) throw new Error('unsupported const ' + name);
	let depth = 0, i = begin;
	for (; i < src.length; i++) {
		if (src[i] === opener) depth++;
		else if (src[i] === closer) {
			depth--;
			if (depth === 0) { i++; break; }
		}
	}
	return 'var ' + name + ' = ' + src.slice(begin, i) + ';\n';
}

const FNS = [
	'efBytes', 'efHex', 'efSwapNibbles', 'efAllFf', 'efRstripFf', 'efUcs2Be',
	'efGsm7Octets', 'efAnnexA', 'efBcdAddress', 'efBcdAddressLen', 'efPlmn', 'efPlmnAct',
	'efDecSpn', 'efDecPlmnWact', 'efDecAdn', 'efDecPnn', 'efDecSmsp', 'efFindDecoder',
	'efDecodeBytes', 'efTlv', 'efPnnText', 'efDecItemsFromHex',
	'efGsm7PlainText', 'efEncTextBytes', 'efPlmnEnc', 'efActEnc', 'efEncSpn',
	'efEncPlmnList', 'efListRows', 'efPlmnRowOrig', 'efEncAdn', 'efEncAdnExt', 'efPnnNameBytes', 'efPnnHexBytes', 'efPnnOrigTlv', 'efEncPnn',
	'efFindEncoder', 'efHasEncoder', 'gsm7Decode', 'gsm7TextToSeptets',
	'gsm7Encode', 'efEncError', 'efDecModelValues',
	'efDecServiceTable', 'efDecSst', 'efDecAcc', 'efDecPhase', 'efDecHpplmn', 'efDecAd', 'efDecCbmi', 'efDecCbmir', 'efDecIccid', 'efDecImsi', 'efDecPlmnList', 'efDecFplmn', 'efEncServiceTable', 'efEncAcc', 'efPhaseValue', 'efPhaseModel', 'efEncPhase', 'efEncHpplmn', 'efAdOpValue', 'efAdModel', 'efEncAd', 'efEncPlmnOnly', 'efEncIccid', 'efEncImsi', 'efCbmiModel', 'efEncCbmi', 'efCbmirModel', 'efEncCbmir', 'efSmspAddressModel', 'efSmspModel', 'efSmspTonOptions', 'efSmspParamRow', 'efSmspHtml', 'efSmspCollect', 'pysimFsSmspToggle', 'pysimFsSmspHexInput', 'efSmspHexByte', 'efSmspAddressBytes', 'efSmspAddrUnchanged', 'efSmspByteUnchanged', 'efEncSmsp', 'smspT', 'smspDcsHint', 'smspVpHint', 'smspDurationText', 'smspPluralIndex', 'smspCount', 'efSmspDaNumber', 
];

let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['GSM7_ALPHABET', 'GSM7_EXT_MAP', 'EF_ACT_TECHS', 'EF_SPN_FIELDS',
	'EF_ADN_FIELDS', 'EF_PLMN_LIST', 'EF_PNN_ITEMS', 'EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_EST_SERVICES', 'EF_PHASE_NAMES', 'EF_ACC_CLASSES', 'EF_UST_FIELDS', 'EF_IST_FIELDS', 'EF_EST_FIELDS', 'EF_ACC_FIELDS', 'EF_PHASE_FIELDS', 'EF_HPPLMN_FIELDS', 'EF_AD_OP_MODES', 'EF_AD_FIELDS', 'EF_PLMN_ONLY_LIST', 'EF_ICCID_FIELDS', 'EF_IMSI_FIELDS', 'EF_CBMI_LIST', 'EF_CBMIR_LIST', 'EF_READONLY_NAMES', 'EF_SMSP_PARAMS', 'EF_SMSP_CUSTOM', 'EF_ENCODERS']) {
	code += extractConst(html, c);
}
eval(code);

test('efPlmnEnc builds the TS 24.008 PLMN and round-trips through efPlmn', () => {
	assert.strictEqual(efPlmnEnc('262', '01'), '62F210');
	assert.strictEqual(efPlmnEnc('228', '06'), '22F860');
	assert.deepStrictEqual(efPlmn(efBytes('22F860')), { mcc: '228', mnc: '06' });
	assert.deepStrictEqual(efPlmn(efBytes(efPlmnEnc('310', '260'))), { mcc: '310', mnc: '260' });
	assert.deepStrictEqual(efPlmn(efBytes(efPlmnEnc('250', '99'))), { mcc: '250', mnc: '99' });
	assert.throws(() => efPlmnEnc('26', '01'), /MCC/);
	assert.throws(() => efPlmnEnc('262', '1'), /MNC/);
	assert.throws(() => efPlmnEnc('26a', '01'), /MCC/);
	assert.throws(() => efPlmnEnc('262', '01a'), /MNC/);
});

test('efActEnc encodes every technology name and the spec bit tables', () => {
	assert.strictEqual(efActEnc('UTRAN'), '8000');
	assert.strictEqual(efActEnc('NG-RAN'), '0800');
	assert.strictEqual(efActEnc('E-UTRAN WB-S1, E-UTRAN NB-S1'), '4000');
	assert.strictEqual(efActEnc('E-UTRAN NB-S1'), '5000');
	assert.strictEqual(efActEnc('E-UTRAN WB-S1'), '6000');
	assert.strictEqual(efActEnc('GSM'), '0084');
	assert.strictEqual(efActEnc('EC-GSM-IoT'), '0088');
	assert.strictEqual(efActEnc('GSM, EC-GSM-IoT'), '0080');
	assert.strictEqual(efActEnc('GSM COMPACT'), '0040');
	assert.strictEqual(efActEnc('cdma2000 HRPD'), '0020');
	assert.strictEqual(efActEnc('cdma2000 1xRTT'), '0010');
	// every name the decoder prints survives an encode/decode round trip
	for (const name of EF_ACT_TECHS) {
		const act = parseInt(efActEnc(name), 16);
		assert.ok(efPlmnAct(act).indexOf(name) !== -1, name + ' round trip');
	}
	assert.throws(() => efActEnc(''), /at least one/);
	assert.throws(() => efActEnc([]), /at least one/);
	assert.throws(() => efActEnc('BOGUS'), /Unknown access technology/);
});

test('efPlmnAct decodes the EC-GSM-IoT-only 0x0088 (the old 0x0086 branch was unreachable)', () => {
	// Table 4.2.5.2: b8=1, b4=1, b3=0 = 0x0088; the mask 0x008C makes 0x0086
	// collapse to 0x0084 (GSM), so the EC-only branch could never fire.
	assert.deepStrictEqual(efPlmnAct(0x0088), ['EC-GSM-IoT']);
	assert.deepStrictEqual(efPlmnAct(0x008C), ['EC-GSM-IoT', 'GSM']);
	assert.deepStrictEqual(efPlmnAct(0x0080), ['EC-GSM-IoT', 'GSM']);
	assert.deepStrictEqual(efPlmnAct(0x0084), ['GSM']);
	assert.deepStrictEqual(efPlmnAct(0x0040), ['GSM COMPACT']);
	assert.deepStrictEqual(efPlmnAct(0x0020), ['cdma2000 HRPD']);
	assert.deepStrictEqual(efPlmnAct(0x0010), ['cdma2000 1xRTT']);
	assert.deepStrictEqual(efPlmnAct(0x8000), ['UTRAN']);
	assert.deepStrictEqual(efPlmnAct(0x0800), ['NG-RAN']);
	assert.deepStrictEqual(efPlmnAct(0x4000), ['E-UTRAN NB-S1', 'E-UTRAN WB-S1']);
	assert.deepStrictEqual(efPlmnAct(0x5000), ['E-UTRAN NB-S1']);
	assert.deepStrictEqual(efPlmnAct(0x6000), ['E-UTRAN WB-S1']);
});

const SPN_ORIG = '0147534d2d52204348ffffffffffffffff';

test('EF.SPN round-trips, and an unchanged name keeps the original coding', () => {
	const dec = efDecSpn(efBytes(SPN_ORIG));
	assert.deepStrictEqual(dec, { show_in_hplmn: true, hide_in_oplmn: false, name: 'GSM-R CH' });
	assert.strictEqual(efEncSpn(dec, SPN_ORIG), SPN_ORIG.toUpperCase());
	// only the condition byte changes: the text bytes are copied verbatim
	const flags = efEncSpn({ show_in_hplmn: false, hide_in_oplmn: true, name: 'GSM-R CH' }, SPN_ORIG);
	assert.strictEqual(flags, '02' + SPN_ORIG.slice(2).toUpperCase());
	// a different name is re-coded (GSM-7 octets, FF padding to 16 bytes)
	const text = efEncSpn({ show_in_hplmn: true, hide_in_oplmn: false, name: 'GSM-R CH2' }, SPN_ORIG);
	assert.strictEqual(text.slice(0, 2), '01');
	assert.strictEqual(text.length, 34);
	assert.strictEqual(text.slice(2), '47534D2D5220434832FFFFFFFFFFFFFF');
	assert.strictEqual(efDecSpn(efBytes(text)).name, 'GSM-R CH2');
	// a character outside the GSM 7-bit alphabet goes UCS-2BE behind 0x80
	const ucs = efEncSpn({ name: '\u0416' }, SPN_ORIG);
	assert.strictEqual(ucs, '00800416' + 'FF'.repeat(13));
	assert.strictEqual(efDecSpn(efBytes(ucs)).name, '\u0416');
	assert.throws(() => efEncSpn({ name: 'A'.repeat(17) }, SPN_ORIG), /too long/);
});

const PLMN_ORIG = '62F2100084';

test('EF.PLMNwAcT round-trips, adds/removes rows and refuses an overflow', () => {
	const dec = efDecPlmnWact(efBytes(PLMN_ORIG));
	assert.deepStrictEqual(dec.plmns, [{ mcc: '262', mnc: '01', access_tech: 'GSM' }]);
	assert.strictEqual(efEncPlmnList(dec.plmns, PLMN_ORIG), PLMN_ORIG);
	assert.strictEqual(efEncPlmnList([], PLMN_ORIG), 'FFFFFFFFFF');
	// two entries in a ten-byte file
	const ten = PLMN_ORIG + '22F8608000';
	const rows = [
		{ mcc: '262', mnc: '01', access_tech: 'GSM' },
		{ mcc: '228', mnc: '06', access_tech: 'UTRAN' },
	];
	assert.strictEqual(efEncPlmnList(rows, ten), ten);
	assert.strictEqual(efEncPlmnList([rows[1]], ten), '22F8608000' + 'FF'.repeat(5));
	// Table 4.2.5.1 both-form round trip through the list encoder
	const both = efEncPlmnList([{ mcc: '262', mnc: '01', access_tech: 'E-UTRAN WB-S1, E-UTRAN NB-S1' }], ten);
	assert.strictEqual(both.slice(0, 10), '62F2104000');
	assert.deepStrictEqual(efDecPlmnWact(efBytes(both)).plmns[0].access_tech, 'E-UTRAN NB-S1, E-UTRAN WB-S1');
	assert.throws(() => efEncPlmnList(rows, PLMN_ORIG), /Too many entries/);
	assert.throws(() => efEncPlmnList([{ mcc: '26', mnc: '01', access_tech: 'GSM' }], ten), /MCC/);
});

const ADN_1 = '42204841203120536963FFFFFFFFFFFF06810628560810FFFFFFFFFFFFFF';
const ADN_2 = 'ffffffffffffffffffffffffffffffffffffffff04b12143f5ffffffffffffffffff';

test('EF.ADN round-trips the decoder vectors byte-for-byte', () => {
	const d1 = efDecAdn(efBytes(ADN_1));
	assert.strictEqual(d1.name, 'B HA 1 Sic');
	assert.strictEqual(d1.number, '6082658001');
	assert.strictEqual(efEncAdn(d1, ADN_1), ADN_1.toUpperCase());
	const d2 = efDecAdn(efBytes(ADN_2));
	assert.strictEqual(d2.name, '');
	assert.strictEqual(d2.number, '12345');
	assert.strictEqual(efEncAdn(d2, ADN_2), ADN_2.toUpperCase());
	assert.strictEqual(efEncAdn(d2, ADN_2).slice(-4), 'FFFF');
});

test('EF.ADN re-encodes an edited name and number in the record length', () => {
	const out = efEncAdn({ name: 'Test', number: '+79001234567' }, ADN_1);
	assert.strictEqual(out.length, ADN_1.length);
	assert.strictEqual(out.slice(0, 8), '54657374'); // 'Test' as GSM-7 octets
	assert.strictEqual(out.slice(32, 36), '0791');   // len 7 (1 + 6 bytes), TON/NPI +
	const dec = efDecAdn(efBytes(out));
	assert.strictEqual(dec.name, 'Test');
	assert.strictEqual(dec.number, '+79001234567');
	assert.strictEqual(dec.ext1, undefined);
	// spaces are ignored
	assert.strictEqual(efEncAdn({ name: 'Test', number: '+7 900 123 45 67' }, ADN_1), out);
	// an emptied number: length 0, TON/NPI FF, digits FF
	const empty = efEncAdn({ name: 'Test', number: '' }, ADN_1);
	assert.strictEqual(empty.slice(32, 36), '00FF');
	assert.strictEqual(efDecAdn(efBytes(empty)).number, '');
	// a name-only edit of an all-FF record fills that record (32-byte record)
	const fill = efEncAdn({ name: 'Ann', number: '123' }, 'FF'.repeat(32));
	assert.strictEqual(fill.length, 64);
	assert.strictEqual(efDecAdn(efBytes(fill)).name, 'Ann');
	assert.strictEqual(efDecAdn(efBytes(fill)).number, '123');
});

test('EF.ADN refuses a number extension, a short record and a bad number', () => {
	const ext1 = ADN_1.slice(0, -2) + '03';
	assert.strictEqual(efEncAdn({ name: 'B HA 1 Sic', number: '6082658001' }, ext1).slice(-4), 'FF03');
	assert.throws(() => efEncAdn({ name: 'B HA 1 Sic', number: '6082658002' }, ext1), /EF\.EXT1/);
	assert.throws(() => efEncAdn({}, '00'), /too short/);
	assert.throws(() => efEncAdn({ number: '+' }, ADN_1), /digits/);
	assert.throws(() => efEncAdn({ number: '12a' }, ADN_1), /digits/);
	assert.throws(() => efEncAdn({ number: '1'.repeat(21) }, ADN_1), /too long/);
});

test('EF.SPN and the ADN family share the encoder registry with the PLMN list', () => {
	assert.strictEqual(efFindEncoder('EF.SPN', null).fid, '6f46');
	assert.strictEqual(efFindEncoder('ef.adn', null).fid, '6f3a');
	assert.strictEqual(efFindEncoder('ADF.USIM/EF.MSISDN', null).name, 'EF.MSISDN');
	assert.strictEqual(efFindEncoder('EF.ALIAS', '6f60').enc, efEncPlmnList);
	assert.strictEqual(efFindEncoder('EF.NOPE', 'abcd'), null);
	assert.strictEqual(efHasEncoder('EF.ADN', null), true);
	assert.strictEqual(efHasEncoder('EF.LOCI', null), false);
	// one shared schema for the ADN family and one for the three PLMN files
	assert.strictEqual(efFindEncoder('EF.ADN', null).fields, efFindEncoder('EF.BDN', null).fields);
	assert.strictEqual(efFindEncoder('EF.PLMNwAcT', null).list, efFindEncoder('EF.HPLMNwAcT', null).list);
});

test('the encoder resolver is DF-first: every EF.AD form shares one encoder', () => {
	// EF.AD collides in name AND FID: one entry per DF (DF.GSM 3+X,
	// ADF.USIM 4+X, ADF.ISIM 3+X), all over the same length-preserving codec
	const sim = efFindEncoder('EF.AD', '6fad', '7F20');
	const usim = efFindEncoder('EF.AD', '6fad', 'ADF.USIM');
	const isim = efFindEncoder('EF.AD', '6fad', 'ADF.ISIM');
	for (const e of [sim, usim, isim]) {
		assert.strictEqual(e.enc, efEncAd);
		assert.strictEqual(e.model, efAdModel);
		assert.strictEqual(e.fields, EF_AD_FIELDS);
	}
	assert.notStrictEqual(sim, isim);
	// without a DF the historical name-first lookup answers (DF.GSM's entry)
	assert.strictEqual(efFindEncoder('EF.AD', '6fad'), sim);
	assert.strictEqual(efFindEncoder('EF.AD', '6fad', 'ADF.NOPE'), sim);
	// EF.ECC stays read-only in either DF, EF.SST in the SIM layout, and the
	// shared 6F38 FID must not hand SST the UST encoder
	assert.strictEqual(efHasEncoder('EF.ECC', '6fb7', '7F20'), false);
	assert.strictEqual(efHasEncoder('EF.ECC', '6fb7', 'ADF.USIM'), false);
	assert.strictEqual(efHasEncoder('EF.SST', '6f38', '7F20'), false);
	assert.strictEqual(efHasEncoder('EF.UST', '6f38', 'ADF.USIM'), true);
	assert.strictEqual(efFindEncoder('EF.NOPE', 'abcd', 'ADF.USIM'), null);
	// the SIM 3+X and USIM 4+X forms keep their length on an untouched save
	for (const hex of ['00' + '0000', '00' + '0000' + '02' + 'AABB', AD_HEX, 'FF'.repeat(6)]) {
		for (const e of [sim, usim]) {
			assert.strictEqual(e.enc(e.model(efDecAd(efBytes(hex))), hex), hex.toUpperCase(), hex);
		}
	}
});

test('encoder validation errors go through t() when the runtime provides it', () => {
	globalThis.t = k => (k === 'Select at least one access technology' ? 'Выберите технологию' : k);
	try {
		assert.throws(() => efActEnc(''), /Выберите технологию/);
	} finally {
		delete globalThis.t;
	}
});

// EF.PNN (TS 31.102 4.2.58): a record is a list of Network name TLVs.  These
// are pySim's reference records (the 43/45 values decoded by efPnnText).
const PNN_1 = '430a82d432bbbc7eb75de432450a82d432bbbc7eb75de432ffffffff';
const PNN_2 = '430a82c596b34cbfbfe5eb39ffffffffffffffffffffffffffffffffffff';

test('EF.PNN round-trips the reference vectors byte-for-byte', () => {
	assert.deepStrictEqual(efDecPnn(efBytes(PNN_1)), { full: 'Telekom.de', short: 'Telekom.de' });
	assert.deepStrictEqual(efDecPnn(efBytes(PNN_2)), { full: 'E-Networks' });
	const entry = efFindEncoder('EF.PNN', null);
	assert.strictEqual(entry.fid, '6fc5');
	assert.strictEqual(entry.items.addLabel, 'Add TLV');
	for (const hex of [PNN_1, PNN_2]) {
		const items = efDecItemsFromHex(entry, hex);
		assert.strictEqual(efEncPnn(items, hex), hex.toUpperCase());
	}
	// a known name tag is decoded to its text, not kept raw
	assert.deepStrictEqual(efDecItemsFromHex(entry, PNN_1), [
		{ tag: '43', text: 'Telekom.de', orig: '430A82D432BBBC7EB75DE432' },
		{ tag: '45', text: 'Telekom.de', orig: '450A82D432BBBC7EB75DE432' },
	]);
});

test('EF.PNN encodes a non-GSM7 name as UCS-2 behind 0x90', () => {
	const entry = efFindEncoder('EF.PNN', null);
	const out = efEncPnn([{ tag: '43', text: 'Сеть' }], PNN_1);
	assert.strictEqual(out.slice(0, 4), '4309');   // 1 + 4 UCS-2 chars
	assert.strictEqual(out.slice(4, 6), '90');
	assert.deepStrictEqual(efDecItemsFromHex(entry, out), [{ tag: '43', text: 'Сеть', orig: '430990042104350442044C' }]);
	// the empty rest is FF padding up to the record length
	assert.strictEqual(out.length, PNN_1.length);
	assert.match(out.slice((2 + 9) * 2), /^F+$/);
});

test('EF.PNN re-encodes an edited name and keeps the other TLVs', () => {
	const entry = efFindEncoder('EF.PNN', null);
	const items = efDecItemsFromHex(entry, PNN_1).map(i =>
		i.tag === '43' ? { tag: i.tag, text: 'NewNet' } : i);
	const out = efEncPnn(items, PNN_1);
	assert.ok(out.includes('450A82D432BBBC7EB75DE432'), 'the 45 TLV is unchanged');
	assert.deepStrictEqual(efDecItemsFromHex(entry, out), [
		{ tag: '43', text: 'NewNet', orig: '430786CEF2DD59A603' },
		{ tag: '45', text: 'Telekom.de', orig: '450A82D432BBBC7EB75DE432' },
	]);
	assert.strictEqual(out.length, PNN_1.length);
});

test('EF.PNN adds and removes TLVs', () => {
	const entry = efFindEncoder('EF.PNN', null);
	const withInfo = efEncPnn([{ tag: '43', text: 'E-Networks' }, { tag: '80', hex: '00410042' }], PNN_2);
	assert.deepStrictEqual(efDecItemsFromHex(entry, withInfo), [
		{ tag: '43', text: 'E-Networks', orig: '430A82C596B34CBFBFE5EB39' },
		{ tag: '80', hex: '00410042', orig: '800400410042' },
	]);
	const onlyFull = efEncPnn([{ tag: '43', text: 'E-Networks' }], PNN_2);
	assert.deepStrictEqual(efDecItemsFromHex(entry, onlyFull), [{ tag: '43', text: 'E-Networks', orig: '430A82C596B34CBFBFE5EB39' }]);
});

test('EF.PNN encodes tag 80 as raw bytes and preserves an unknown tag verbatim', () => {
	const entry = efFindEncoder('EF.PNN', null);
	const out = efEncPnn([{ tag: '80', hex: '00410042' }], 'FF'.repeat(16));
	assert.strictEqual(out.slice(0, 12), '800400410042');
	assert.deepStrictEqual(efDecItemsFromHex(entry, out), [{ tag: '80', hex: '00410042', orig: '800400410042' }]);
	// an unknown tag parsed from the card is read-only and re-emitted as-is
	const hex = '430a82d432bbbc7eb75de4325a02aabb' + 'ff'.repeat(14);
	const items = efDecItemsFromHex(entry, hex);
	assert.deepStrictEqual(items, [
		{ tag: '43', text: 'Telekom.de', orig: '430A82D432BBBC7EB75DE432' },
		{ tag: '5A', hex: 'AABB', raw: true, orig: '5A02AABB' },
	]);
	assert.strictEqual(efEncPnn(items, hex), hex.toUpperCase());
});

test('EF.PNN refuses duplicate names, bad hex and an overflowing record', () => {
	assert.throws(() => efEncPnn([{ tag: '43', text: 'A' }, { tag: '43', text: 'B' }], PNN_1), /Only one full name/);
	assert.throws(() => efEncPnn([{ tag: '45', text: 'A' }, { tag: '45', text: 'B' }], PNN_1), /Only one full name/);
	assert.throws(() => efEncPnn([{ tag: '80', hex: '0G' }], PNN_1), /even number of hex/);
	assert.throws(() => efEncPnn([{ tag: '80', hex: '0' }], PNN_1), /even number of hex/);
	assert.throws(() => efEncPnn([{ tag: '4', hex: '00' }], PNN_1), /one hex byte/);
	assert.throws(() => efEncPnn([{ tag: '43', text: 'X'.repeat(40) }], 'FF'.repeat(10)), /too short for these items/);
	assert.throws(() => efEncPnn([{ tag: '80', hex: '00'.repeat(20) }], 'FF'.repeat(10)), /too short for these items/);
	// an empty name omits its TLV (nothing but FF padding)
	assert.strictEqual(efEncPnn([{ tag: '43', text: '' }], 'FF'.repeat(8)), 'FF'.repeat(8));
});

test('EF.PNN writes spare 0 when the packed text fills the last octet', () => {
	const entry = efFindEncoder('EF.PNN', null);
	// 8 septets = 56 bits = 7 octets exactly -> coding octet 0x80
	const out = efEncPnn([{ tag: '43', text: 'Vodafone' }], 'FF'.repeat(16));
	assert.strictEqual(out.slice(0, 4), '4308');
	assert.strictEqual(out.slice(4, 6), '80');
	assert.deepStrictEqual(efDecItemsFromHex(entry, out), [{ tag: '43', text: 'Vodafone', orig: '430880D637396C7EBBCB' }]);
});

// EF.BDN (TS 31.102 4.2.44) is the X+15 layout: alpha, len, TON/NPI, BCD(10),
// CCP, EXT4, then the EF.CMI comparison-method pointer.
const BDN = '54455354' + '06' + '91' + '2143658709' + 'FF'.repeat(7) + '03';

test('EF.BDN round-trips the X+15 layout and preserves the CMI pointer', () => {
	const bytes = efBytes(BDN);
	assert.strictEqual(bytes.length, 19);
	const dec = efDecAdn(bytes, 15);
	assert.deepStrictEqual(dec, { name: 'TEST', number: '+1234567890', cmi: 3 });
	assert.strictEqual(efEncAdn(dec, BDN, 15), BDN);
	// the registry's BDN entry applies the 15-byte layout
	const entry = efFindEncoder('EF.BDN', null);
	assert.strictEqual(entry.fid, '6f4d');
	assert.strictEqual(entry.enc(dec, BDN), BDN);
	// editing the number keeps the alpha and the trailing CMI pointer
	const edited = entry.enc({ name: 'TEST', number: '+79001234567' }, BDN);
	assert.strictEqual(edited.slice(0, 8), '54455354');
	assert.strictEqual(edited.slice(-2), '03');
	const back = efDecAdn(efBytes(edited), 15);
	assert.strictEqual(back.name, 'TEST');
	assert.strictEqual(back.number, '+79001234567');
	assert.strictEqual(back.cmi, 3);
	assert.strictEqual(back.ext1, undefined);
});

test('the ADN family stays on the 14-byte layout', () => {
	// the default is the old 14-byte layout: the registry entry and the
	// 14-byte round trip are unchanged
	assert.strictEqual(efFindEncoder('EF.ADN', null).enc(efDecAdn(efBytes(ADN_1)), ADN_1), ADN_1.toUpperCase());
	assert.strictEqual(efEncAdn(efDecAdn(efBytes(ADN_1)), ADN_1), ADN_1.toUpperCase());
	assert.notDeepStrictEqual(efDecAdn(efBytes(ADN_1)), efDecAdn(efBytes(ADN_1), 15));
	// a BDN record decoded with the default layout is not the BDN result; a
	// number EDIT through the 14-byte layout is refused (its EXT slot is the
	// BDN's EF.CMI pointer), while the BDN layout writes it
	assert.notDeepStrictEqual(efDecAdn(efBytes(BDN)), efDecAdn(efBytes(BDN), 15));
	assert.throws(() => efEncAdn({ name: 'TEST', number: '+7900' }, BDN), /EF\.EXT1/);
	assert.ok(efEncAdn({ name: 'TEST', number: '+7900' }, BDN, 15));
});

// A control-string/extended-BCD dialling number (SSC): the decoder only sees
// the digit fragment, so an untouched number must not be rebuilt from it.
const SSC_ADN = '54455354' + 'FF'.repeat(12) + '04' + 'FF' + 'BA01B0' + 'FF'.repeat(7) + 'FF' + 'FF';

test('F1: an untouched SSC number block is copied verbatim', () => {
	const hex = SSC_ADN;
	assert.strictEqual(efBytes(hex).length, 30);
	assert.deepStrictEqual(efDecAdn(efBytes(hex)), { name: 'TEST', number: '100' });
	// name-only edit: the [length, TON/NPI, 10 BCD/SSC] block is untouched
	const out = efEncAdn({ name: 'NEW', number: '100' }, hex);
	assert.strictEqual(out.slice(0, 6), '4E4557');
	assert.strictEqual(out.slice(32, 56), hex.toUpperCase().slice(32, 56));
	// no edit at all: byte-identical
	assert.strictEqual(efEncAdn(efDecAdn(efBytes(hex)), hex), hex.toUpperCase());
	// an all-FF record stays all-FF (the old encoder wrote a 00 length byte)
	assert.strictEqual(efEncAdn({}, 'FF'.repeat(64)), 'FF'.repeat(64));
});

test('F4: an untouched all-FF SPN stays all-FF', () => {
	const ff = 'FF'.repeat(17);
	// the model exposes the FF display bits (both set), not a blank object
	assert.deepStrictEqual(efDecSpn(efBytes(ff)), { show_in_hplmn: true, hide_in_oplmn: true });
	assert.strictEqual(efEncSpn(efDecSpn(efBytes(ff)), ff), ff);
});

test('F5: unchanged PLMN rows keep unmodelled ACT bits and codings', () => {
	// 0x8C00: UTRAN + NG-RAN + the Rel-18 satellite bit the form does not model
	const rec = '62F2108C00';
	const dec = efDecPlmnWact(efBytes(rec));
	assert.strictEqual(dec.plmns[0].access_tech, 'NG-RAN, UTRAN');
	assert.strictEqual(efEncPlmnList(dec.plmns, rec), rec);
	// editing the MCC keeps the original ACT bytes (not 0x8800)
	assert.strictEqual(efEncPlmnList([{ mcc: '250', mnc: '01', access_tech: 'NG-RAN, UTRAN' }], rec),
		'52F010' + '8C00');
	// equal codings are not canonicalised either
	assert.strictEqual(efEncPlmnList(efDecPlmnWact(efBytes('62F210008C')).plmns, '62F210008C'), '62F210008C');
	assert.strictEqual(efEncPlmnList(efDecPlmnWact(efBytes('62F2107000')).plmns, '62F2107000'), '62F2107000');
	// a changed ACT is encoded canonically
	assert.strictEqual(efEncPlmnList([{ mcc: '262', mnc: '01', access_tech: 'NG-RAN' }], rec), '62F2100800');
});

test('F9: an emptied full name is refused while other TLVs remain', () => {
	assert.throws(() => efEncPnn([{ tag: '43', text: '' }, { tag: '45', text: 'Short' }], PNN_1),
		/full name \(43\) is required/);
	assert.throws(() => efEncPnn([{ tag: '43', text: '' }, { tag: '80', hex: '00' }], PNN_1),
		/full name/);
	// the empty omission is only for a record that ends up with no items
	assert.strictEqual(efEncPnn([{ tag: '43', text: '' }], 'FF'.repeat(8)), 'FF'.repeat(8));
});

test('F10: a TLV value longer than 255 bytes is refused', () => {
	const big = '00'.repeat(256);
	assert.throws(() => efEncPnn([{ tag: '80', hex: big }], 'FF'.repeat(40)), /longer than 255/);
	assert.throws(() => efEncPnn([{ tag: '5A', hex: big }], 'FF'.repeat(40)), /longer than 255/);
});

test('F14: the encoder messages go through t() (unknown technology, too many rows)', () => {
	globalThis.t = k => (k === 'Unknown access technology' ? 'U'
		: (k === 'Too many entries for this file' ? 'T' : k));
	try {
		assert.throws(() => efActEnc('BOGUS'), /^Error: U: BOGUS$/);
		assert.throws(() => efEncPlmnList([
			{ mcc: '262', mnc: '01', access_tech: 'GSM' },
			{ mcc: '228', mnc: '06', access_tech: 'GSM' },
		], '62F2100084'), /^Error: T \(1 max\)$/);
	} finally {
		delete globalThis.t;
	}
});

test('unchanged content re-encodes byte-identically (all fixtures)', () => {
	const tables = { 'EF.UST': EF_UST_SERVICES, 'EF.IST': EF_IST_SERVICES,
		'EF.EST': EF_EST_SERVICES };
	const decode = (name, hex) => {
		const entry = efFindEncoder(name, null);
		const bytes = efBytes(hex);
		let dec;
		if (name === 'EF.SPN') dec = efDecSpn(bytes);
		else if (tables[name]) {
			// the form renders only the table's services; other set bits stay
			// in the original bytes
			dec = { services: efDecServiceTable(bytes, tables[name]).services
				.filter(s => Object.prototype.hasOwnProperty.call(tables[name], s.n)) };
		} else if (name === 'EF.ACC') dec = efDecAcc(bytes);
		else if (name === 'EF.PHASE') dec = efDecPhase(bytes);
		else if (name === 'EF.HPPLMN') dec = efDecHpplmn(bytes);
		else if (name === 'EF.AD') dec = efDecAd(bytes);
		else if (name === 'EF.PLMNsel' || name === 'EF.EHPLMN') dec = efDecPlmnList(bytes);
		else if (name === 'EF.FPLMN') dec = efDecFplmn(bytes);
		else if (name === 'EF.ICCID') dec = efDecIccid(bytes);
		else if (name === 'EF.IMSI') dec = efDecImsi(bytes);
		else if (name === 'EF.CBMI' || name === 'EF.CBMID') dec = efDecCbmi(bytes);
		else if (name === 'EF.CBMIR') dec = efDecCbmir(bytes);
		else if (name === 'EF.PLMNwAcT') dec = efDecPlmnWact(bytes).plmns;
		else if (name === 'EF.BDN') dec = efDecAdn(bytes, 15);
		else if (name === 'EF.ADN') dec = efDecAdn(bytes);
		else if (name === 'EF.PNN') dec = efDecItemsFromHex(efFindEncoder('EF.PNN', null), hex);
		else if (name === 'EF.SMSP') dec = efSmspModel(hex);
		else throw new Error('no decoder for ' + name);
		// the model's empty/raw marker yields the form's empty value set
		if (dec && (dec.empty === true || (dec.raw !== undefined && Object.keys(dec).length === 1))) dec = {};
		return entry.model ? entry.model(dec) : dec;
	};
	const cases = [
		{ name: 'EF.SPN', fid: '6f46', hex: SPN_ORIG },
		{ name: 'EF.SPN', fid: '6f46', hex: 'FF'.repeat(17) },
		{ name: 'EF.PLMNwAcT', fid: '6f60', hex: PLMN_ORIG },
		{ name: 'EF.PLMNwAcT', fid: '6f60', hex: PLMN_ORIG + '22F8608000' },
		{ name: 'EF.PLMNwAcT', fid: '6f60', hex: '62F2108C00' },
		{ name: 'EF.PLMNwAcT', fid: '6f60', hex: '62F210008C' },
		{ name: 'EF.PLMNwAcT', fid: '6f60', hex: '62F2107000' },
		{ name: 'EF.ADN', fid: '6f3a', hex: ADN_1 },
		{ name: 'EF.ADN', fid: '6f3a', hex: ADN_2 },
		{ name: 'EF.ADN', fid: '6f3a', hex: SSC_ADN },
		{ name: 'EF.ADN', fid: '6f3a', hex: 'FF'.repeat(64) },
		{ name: 'EF.BDN', fid: '6f4d', hex: BDN },
		{ name: 'EF.BDN', fid: '6f4d', hex: 'FF'.repeat(38) },
		{ name: 'EF.PNN', fid: '6fc5', hex: PNN_1 },
		{ name: 'EF.PNN', fid: '6fc5', hex: PNN_2 },
		{ name: 'EF.PNN', fid: '6fc5', hex: '800400410042' + 'FF'.repeat(16) },
		{ name: 'EF.PNN', fid: '6fc5', hex: '430a82d432bbbc7eb75de4325a02aabb' + 'ff'.repeat(14) },
		// wave 2
		{ name: 'EF.UST', fid: '6f38', hex: UST_ONE },
		{ name: 'EF.UST', fid: '6f38', hex: UST_MULTI },
		{ name: 'EF.UST', fid: '6f38', hex: UST_UNMODELLED },
		{ name: 'EF.UST', fid: '6f38', hex: 'FF'.repeat(19) },
		{ name: 'EF.IST', fid: '6f07', hex: '01' },
		{ name: 'EF.EST', fid: '6f56', hex: '07' },
		{ name: 'EF.EST', fid: '6f56', hex: EST_FF },
		{ name: 'EF.ACC', fid: '6f78', hex: ACC_ONE },
		{ name: 'EF.PHASE', fid: '6fae', hex: '00' },
		{ name: 'EF.PHASE', fid: '6fae', hex: '02' },
		{ name: 'EF.PHASE', fid: '6fae', hex: '03' },
		{ name: 'EF.PHASE', fid: '6fae', hex: '0F' },
		{ name: 'EF.HPPLMN', fid: '6f31', hex: '00' },
		{ name: 'EF.HPPLMN', fid: '6f31', hex: '2A' },
		{ name: 'EF.AD', fid: '6fad', hex: AD_HEX },
		{ name: 'EF.AD', fid: '6fad', hex: '55' + '0000' + '03' + 'AABB' },
		{ name: 'EF.AD', fid: '6fad', hex: 'FF'.repeat(6) },
		{ name: 'EF.AD', fid: '6fad', hex: '00' + '0000' },
		{ name: 'EF.AD', fid: '6fad', hex: '00' + '0000' + '00' + 'AABB' },
		{ name: 'EF.PLMNsel', fid: '6f30', hex: PLMN_ONLY },
		{ name: 'EF.FPLMN', fid: '6f7b', hex: FPLMN_HOLES },
		{ name: 'EF.EHPLMN', fid: '6fd9', hex: PLMN_ONLY },
		{ name: 'EF.ICCID', fid: '2fe2', hex: ICCID_HEX },
		{ name: 'EF.ICCID', fid: '2fe2', hex: 'FF'.repeat(10) },
		{ name: 'EF.IMSI', fid: '6f07', hex: IMSI_HEX },
		{ name: 'EF.IMSI', fid: '6f07', hex: '0811325476981032F4' },
		{ name: 'EF.IMSI', fid: '6f07', hex: 'FF'.repeat(9) },
		{ name: 'EF.CBMI', fid: '6f45', hex: CBMI_HEX },
		{ name: 'EF.CBMID', fid: '6f48', hex: CBMI_HEX },
		{ name: 'EF.CBMIR', fid: '6f50', hex: CBMIR_HEX },
		{ name: 'EF.SMSP', fid: '6f42', hex: SMSP_FIX },
		{ name: 'EF.SMSP', fid: '6f42', hex: SMSP_VEC },
		{ name: 'EF.CBMIR', fid: '6f50', hex: 'FF'.repeat(4) },
	];
	for (const c of cases) {
		const entry = efFindEncoder(c.name, c.fid);
		assert.strictEqual(entry.enc(decode(c.name, c.hex), c.hex), c.hex.toUpperCase(),
			c.name + ' ' + c.hex);
	}
});

test('F2: low-byte packed, AddCI and UCS-2-padded names round-trip unchanged', () => {
	const entry = efFindEncoder('EF.PNN', null);
	// 0x82 spare 2, packed 41 21 = 'AB' (the old all-low heuristic read 'A!')
	assert.strictEqual(efPnnText(efBytes('824121')), 'AB');
	const records = [
		'4303824121' + 'FF'.repeat(28),             // low-byte packed 'AB'
		'43038A4121' + 'FF'.repeat(28),             // the same with the AddCI bit
		'43079000410042FFFF' + 'FF'.repeat(20),     // UCS-2 'AB' with FF padding
	];
	for (const hex of records) {
		const items = efDecItemsFromHex(entry, hex);
		assert.strictEqual(efEncPnn(items, hex), hex, hex);
	}
});

test('F2: editing one PNN name leaves the other TLV byte-identical', () => {
	const entry = efFindEncoder('EF.PNN', null);
	// 43 carries the AddCI-bit packing; 45 is a plain packed name
	const hex = '43038A4121' + '4503824222' + 'FF'.repeat(24);
	const items = efDecItemsFromHex(entry, hex);
	assert.deepStrictEqual(items.map(i => i.text), ['AB', 'BD']);
	const edited = items.map(i => i.tag === '43' ? { tag: i.tag, text: 'Z', orig: i.orig } : i);
	const out = efEncPnn(edited, hex);
	assert.ok(out.includes('4503824222'), 'the 45 TLV is unchanged');
	assert.strictEqual(efDecItemsFromHex(entry, out)[1].text, 'BD');
});

test('F8: the ADN-family entries name their extension EF and use it in refusals', () => {
	const names = ['EF.ADN', 'EF.FDN', 'EF.SDN', 'EF.MSISDN', 'EF.LND', 'EF.BDN'];
	assert.deepStrictEqual(names.map(n => efFindEncoder(n, null).ext),
		['EXT1', 'EXT2', 'EXT3', 'EXT5', 'EXT1', 'EXT4']);
	// a non-FF extension slot refuses a changed number with its own EF name
	const fdn = ADN_1.slice(0, -2) + '03';           // EXT2 set
	assert.throws(() => efFindEncoder('EF.FDN', null).enc(
		{ name: 'B HA 1 Sic', number: '6082658002' }, fdn), /EF\.EXT2/);
	const sdn = ADN_1.slice(0, -2) + '03';
	assert.throws(() => efFindEncoder('EF.SDN', null).enc(
		{ name: 'B HA 1 Sic', number: '6082658002' }, sdn), /EF\.EXT3/);
	const msisdn = ADN_1.slice(0, -2) + '03';
	assert.throws(() => efFindEncoder('EF.MSISDN', null).enc(
		{ name: 'B HA 1 Sic', number: '6082658002' }, msisdn), /EF\.EXT5/);
	// BDN: the slot before the CMI pointer is EXT4
	const bdnExt4 = '54455354' + '06' + '91' + '2143658709' + 'FF'.repeat(6) + '04' + '03';
	assert.throws(() => efEncAdn({ name: 'TEST', number: '+79001234567' }, bdnExt4, 15, 'EXT4'), /EF\.EXT4/);
	// the unchanged number still copies the extension slots verbatim
	assert.strictEqual(efEncAdn(efDecAdn(efBytes(fdn)), fdn, 14, 'EXT2'), fdn.toUpperCase());
});

// Wave 2 fixtures.
const UST_ONE = '00000010';                  // service 29
const UST_MULTI = '010204';                  // services 1, 10, 19
const UST_UNMODELLED = '000000' + '10' + '00'.repeat(14) + 'C0'; // 29 + bits 151-152 (above the 150-service table)
const EST_FF = 'FFFF';
const ACC_ONE = '8001';                      // classes 15 and 0
const AD_HEX = '00' + '0000' + '03' + 'AABB'; // op normal, info 0000, MNC 3, RFU
const PLMN_ONLY = '62F21022F860';
const FPLMN_HOLES = '22F860FFFFFF62F210';
const ICCID_HEX = '988812010000400310f0';
const IMSI_HEX = '082982608200002080';
const CBMI_HEX = '0010FFFF0020';
const CBMIR_HEX = '0000FFFE';

test('service tables: encode/decode round-trips and unmodelled bits', () => {
	const label = n => EF_UST_SERVICES[n] || 'RFU';
	assert.deepStrictEqual(efDecServiceTable(efBytes(UST_MULTI), EF_UST_SERVICES).services,
		[{ n: 1, name: label(1) }, { n: 10, name: label(10) }, { n: 19, name: label(19) }]);
	for (const [name, hex] of [['EF.UST', UST_ONE], ['EF.UST', UST_MULTI],
		['EF.IST', '01'], ['EF.EST', '07']]) {
		const entry = efFindEncoder(name, null);
		const table = name === 'EF.UST' ? EF_UST_SERVICES
			: (name === 'EF.IST' ? EF_IST_SERVICES : EF_EST_SERVICES);
		const services = efDecServiceTable(efBytes(hex), table).services;
		assert.strictEqual(entry.enc({ services: services }, hex), hex, name + ' ' + hex);
	}
	// EF.SST has no editor (its 2-bit SIM layout needs a dedicated UI)
	assert.strictEqual(efFindEncoder('EF.SST', '6f38'), null);
	assert.strictEqual(efHasEncoder('EF.SST', '6f38'), false);
	assert.strictEqual(efFindEncoder('EF.UST', '6f38').name, 'EF.UST');
	// EST defines services 1-3; all-FF keeps the other bits too
	assert.deepStrictEqual(efDecServiceTable(efBytes('07'), EF_EST_SERVICES).services,
		[{ n: 1, name: 'Fixed Dialling Numbers (FDN)' },
			{ n: 2, name: 'Barred Dialling Numbers (BDN)' },
			{ n: 3, name: 'APN Control List (ACL)' }]);
	assert.strictEqual(efFindEncoder('EF.EST', null).enc(
		{ services: efDecServiceTable(efBytes(EST_FF), EF_EST_SERVICES).services
			.filter(s => Object.prototype.hasOwnProperty.call(EF_EST_SERVICES, s.n)) }, EST_FF), EST_FF);
	// service bits outside the table are preserved from the original bytes
	const entry = efFindEncoder('EF.UST', null);
	const unmodelled = efDecServiceTable(efBytes(UST_UNMODELLED), EF_UST_SERVICES).services
		.filter(s => Object.prototype.hasOwnProperty.call(EF_UST_SERVICES, s.n));
	assert.strictEqual(entry.enc({ services: unmodelled }, UST_UNMODELLED), UST_UNMODELLED);
	// unchecking everything clears the table bits but keeps the unmodelled byte
	assert.strictEqual(entry.enc({ services: [] }, UST_UNMODELLED),
		'000000' + '00' + '00'.repeat(14) + 'C0');
	// length is kept
	assert.strictEqual(entry.enc({ services: efDecServiceTable(efBytes('00000010' + '00'), EF_UST_SERVICES).services },
		'0000001000'), '0000001000');
});

test('service tables: unknown services and a too-short table are refused', () => {
	assert.throws(() => efEncServiceTable({ services: [{ n: 151, name: 'RFU' }] },
		'FF'.repeat(19), EF_UST_SERVICES), /Unknown service/);
	assert.throws(() => efEncServiceTable({ services: [{ n: 9, name: 'MSISDN' }] },
		'00', EF_UST_SERVICES), /too short for service/);
	assert.throws(() => efEncServiceTable({ services: [{ n: 0 }] }, '00', EF_UST_SERVICES), /Unknown service/);
});

test('F-ACC: the class numbering and the ACC10/encoder rules', () => {
	assert.deepStrictEqual(efDecAcc(efBytes('8000')), { access_classes: [15] });
	assert.deepStrictEqual(efDecAcc(efBytes('0001')), { access_classes: [0] });
	assert.deepStrictEqual(efDecAcc(efBytes('8001')), { access_classes: [15, 0] });
	assert.strictEqual(efEncAcc({ access_classes: [{ n: 15 }] }, '0000'), '8000');
	assert.strictEqual(efEncAcc({ access_classes: [{ n: 0 }] }, '0000'), '0001');
	assert.strictEqual(efEncAcc({ access_classes: efDecAcc(efBytes(ACC_ONE)).access_classes }, ACC_ONE), ACC_ONE);
	// ACC 10 is not editable but the encoder always forces it to 0
	assert.strictEqual(efEncAcc({ access_classes: [{ n: 10 }] }, '0000'), '0000');
	assert.strictEqual(efEncAcc({ access_classes: [{ n: 15 }, { n: 10 }] }, '0000'), '8000');
	assert.throws(() => efEncAcc({ access_classes: [{ n: 16 }] }, '0000'), /Unknown access class/);
	assert.throws(() => efEncAcc({ access_classes: [] }, '00'), /too short/);
});

test('PHASE: known and unknown values round-trip through the model', () => {
	for (const hex of ['00', '02', '03', '0F']) {
		const model = efPhaseModel(efDecPhase(efBytes(hex)));
		assert.strictEqual(efEncPhase(model, hex), hex, hex);
	}
	assert.deepStrictEqual(efPhaseModel(efDecPhase(efBytes('03'))), { phase_hex: '03' });
	assert.deepStrictEqual(efPhaseModel(efDecPhase(efBytes('0F'))), { phase_hex: '0F' });
	assert.strictEqual(efDecPhase(efBytes('0F')).phase, '0x0F');
	assert.throws(() => efEncPhase({ phase_hex: 'ZZ' }, '00'), /hex byte/);
});

test('HPPLMN: the raw interval byte round-trips', () => {
	assert.deepStrictEqual(efDecHpplmn(efBytes('2A')), { interval: '0x2A' });
	for (const hex of ['00', '2A', 'FF']) {
		assert.strictEqual(efEncHpplmn(efDecHpplmn(efBytes(hex)), hex), hex, hex);
	}
	assert.strictEqual(efEncHpplmn({ interval: '2A' }, '2A'), '2A');
	assert.throws(() => efEncHpplmn({ interval: 'XYZ' }, '00'), /hex byte/);
});

test('AD: the whole-byte operation mode, additional info, MNC length and RFU tail', () => {
	const dec = efDecAd(efBytes(AD_HEX));
	assert.deepStrictEqual(dec, { op_mode: 'normal operation', additional_info: '0000', mnc_length: 3 });
	assert.deepStrictEqual(efAdModel(dec), { op_mode: '00', additional_info: '0000', mnc_length: '3' });
	assert.strictEqual(efEncAd(efAdModel(dec), AD_HEX), AD_HEX);
	for (const [op, name] of [['80', 'type approval'], ['01', 'normal operation + specific facilities'],
		['81', 'type approval + specific facilities'], ['02', 'maintenance (off line)'],
		['04', 'cell test operation']]) {
		const hex = op + '0000' + '03';
		assert.strictEqual(efDecAd(efBytes(hex)).op_mode, name, op);
		assert.strictEqual(efEncAd(efAdModel(efDecAd(efBytes(hex))), hex), hex, op);
	}
	// an edited op mode / info / MNC keeps the RFU tail
	assert.strictEqual(efEncAd({ op_mode: '80', additional_info: '1234', mnc_length: '2' }, AD_HEX),
		'80123402AABB');
	// the SIM 3-byte form (no MNC-length byte) and the valid MNC length 0
	assert.strictEqual(efEncAd({ op_mode: '00', additional_info: '1234' }, '00' + '0000'), '001234');
	assert.strictEqual(efEncAd({ op_mode: '00', additional_info: '1234', mnc_length: '' }, '00' + '0000'), '001234');
	assert.throws(() => efEncAd({ op_mode: '00', additional_info: '1234', mnc_length: '2' }, '00' + '0000'),
		/no MNC length byte/);
	assert.strictEqual(efEncAd({ op_mode: '00', additional_info: '0000', mnc_length: '0' }, AD_HEX),
		'000000' + '00' + 'AABB');
	// an unknown op byte is shown and written back
	const unk = '55' + '0000' + '03' + 'AABB';
	assert.strictEqual(efDecAd(efBytes(unk)).op_mode, '0x55');
	assert.strictEqual(efAdModel(efDecAd(efBytes(unk))).op_mode, '55');
	assert.strictEqual(efEncAd(efAdModel(efDecAd(efBytes(unk))), unk), unk);
	// an all-FF record stays all-FF (the model is empty)
	assert.strictEqual(efEncAd(efAdModel({}), 'FF'.repeat(6)), 'FF'.repeat(6));
	assert.throws(() => efEncAd({ op_mode: 'ZZ' }, AD_HEX), /hex byte/);
	assert.throws(() => efEncAd({ op_mode: '00', additional_info: '123' }, AD_HEX), /4 hex digits/);
	assert.throws(() => efEncAd({ op_mode: '00', mnc_length: '4' }, AD_HEX), /0, 2 or 3/);
	assert.throws(() => efEncAd({ op_mode: '00' }, '00'), /too short/);
});

test('PLMN-only lists: round-trip, holes, add/remove and capacity', () => {
	assert.deepStrictEqual(efDecPlmnList(efBytes(PLMN_ONLY)).plmns,
		[{ mcc: '262', mnc: '01' }, { mcc: '228', mnc: '06' }]);
	assert.strictEqual(efEncPlmnOnly({ plmns: efDecPlmnList(efBytes(PLMN_ONLY)).plmns }, PLMN_ONLY), PLMN_ONLY);
	// FPLMN FFFFFF holes anywhere are kept for an unchanged list
	assert.deepStrictEqual(efDecFplmn(efBytes(FPLMN_HOLES)).plmns,
		[{ mcc: '228', mnc: '06' }, { mcc: '262', mnc: '01' }]);
	assert.strictEqual(efEncPlmnOnly({ plmns: efDecFplmn(efBytes(FPLMN_HOLES)).plmns }, FPLMN_HOLES), FPLMN_HOLES);
	// an edit compacts and FF-pads
	assert.strictEqual(efEncPlmnOnly({ plmns: [{ mcc: '262', mnc: '01' }] }, FPLMN_HOLES),
		'62F210' + 'FF'.repeat(6));
	assert.strictEqual(efEncPlmnOnly({ plmns: [
		{ mcc: '262', mnc: '01' }, { mcc: '228', mnc: '06' }, { mcc: '250', mnc: '99' },
	] }, '62F21022F860' + 'FFFFFF'), '62F21022F86052F099');
	assert.throws(() => efEncPlmnOnly({ plmns: [{ mcc: '262', mnc: '01' }, { mcc: '228', mnc: '06' }] },
		'62F210'), /Too many entries/);
	assert.throws(() => efEncPlmnOnly({ plmns: [{ mcc: '26', mnc: '01' }] }, '62F210'), /MCC/);
});

test('ICCID / IMSI: BCD round-trips, padding and refusals', () => {
	assert.deepStrictEqual(efDecIccid(efBytes(ICCID_HEX)), { iccid: '8988211000000430010' });
	assert.strictEqual(efEncIccid({ iccid: '8988211000000430010' }, ICCID_HEX), ICCID_HEX.toUpperCase());
	// odd digit count -> trailing F nibble
	assert.strictEqual(efEncIccid({ iccid: '12345' }, 'FF'.repeat(10)), '2143F5' + 'FF'.repeat(7));
	assert.strictEqual(efEncIccid({}, 'FF'.repeat(10)), 'FF'.repeat(10));
	assert.throws(() => efEncIccid({ iccid: '12A' }, 'FF'.repeat(10)), /digits/);
	assert.throws(() => efEncIccid({ iccid: '1'.repeat(21) }, 'FF'.repeat(10)), /too long/);
	assert.throws(() => efEncIccid({ iccid: '1'.repeat(20) }, 'FF'.repeat(9)), /does not fit/);

	assert.deepStrictEqual(efDecImsi(efBytes(IMSI_HEX)), { imsi: '228062800000208' });
	assert.strictEqual(efEncImsi({ imsi: '228062800000208' }, IMSI_HEX), IMSI_HEX);
	// 14 digits (even): the type/parity nibble is 1 and the body is F-padded
	assert.strictEqual(efEncImsi({ imsi: '12345678901234' }, 'FF'.repeat(9)), '0811325476981032F4');
	assert.deepStrictEqual(efDecImsi(efBytes('0811325476981032F4')), { imsi: '12345678901234' });
	assert.strictEqual(efEncImsi({ imsi: '123456789012345' }, 'FF'.repeat(9)), '081932547698103254');
	assert.deepStrictEqual(efDecImsi(efBytes('081932547698103254')), { imsi: '123456789012345' });
	// 6F07 is also EF.IST: a short file is refused, not overwritten
	assert.throws(() => efEncImsi({ imsi: '228062800000208' }, '01'), /service table, not an IMSI/);
	assert.throws(() => efEncImsi({}, '01'), /service table, not an IMSI/);
	assert.strictEqual(efEncImsi({}, 'FF'.repeat(9)), 'FF'.repeat(9));
	assert.throws(() => efEncImsi({ imsi: '12345' }, 'FF'.repeat(9)), /6-15/);
	assert.throws(() => efEncImsi({ imsi: '1'.repeat(16) }, 'FF'.repeat(9)), /6-15/);
	assert.throws(() => efEncImsi({ imsi: '228062800000208' }, 'FF'.repeat(8)), /does not fit/);
});

test('CBMI / CBMIR: message ids and ranges round-trip with FF padding', () => {
	assert.deepStrictEqual(efDecCbmi(efBytes(CBMI_HEX)), { message_ids: [16, 32] });
	const cbmi = efCbmiModel(efDecCbmi(efBytes(CBMI_HEX)));
	assert.deepStrictEqual(cbmi, { message_ids: [{ id: '0010' }, { id: '0020' }] });
	assert.strictEqual(efEncCbmi(cbmi, CBMI_HEX), CBMI_HEX);   // the FFFF hole is kept
	assert.strictEqual(efEncCbmi({ message_ids: [{ id: '0010' }, { id: '0020' }, { id: '0044' }] },
		CBMI_HEX), '001000200044');
	assert.strictEqual(efEncCbmi({ message_ids: [{ id: '0010' }] }, '0010FFFFFFFF'), '0010FFFFFFFF');
	assert.strictEqual(efEncCbmi({ message_ids: [{ id: '0010' }, { id: '20' }] }, '0010FFFFFFFF'),
		'00100020FFFF');
	assert.throws(() => efEncCbmi({ message_ids: [{ id: 'XYZ' }] }, CBMI_HEX), /1-4 hex digits/);
	assert.throws(() => efEncCbmi({ message_ids: [{ id: '0010' }, { id: '0020' }, { id: '0030' }, { id: '0040' }] },
		'0010FFFF'), /Too many entries/);

	assert.deepStrictEqual(efDecCbmir(efBytes(CBMIR_HEX)), { ranges: [['0000', 'FFFE']] });
	const cbmir = efCbmirModel(efDecCbmir(efBytes(CBMIR_HEX)));
	assert.deepStrictEqual(cbmir, { ranges: [{ low: '0000', high: 'FFFE' }] });
	assert.strictEqual(efEncCbmir(cbmir, CBMIR_HEX), CBMIR_HEX);
	assert.strictEqual(efEncCbmir({ ranges: [
		{ low: '0000', high: 'FFFE' }, { low: '0100', high: '02FF' },
	] }, CBMIR_HEX + 'FFFFFFFF'), '0000FFFE010002FF');
	assert.strictEqual(efEncCbmir({ ranges: [{ low: '0000', high: 'FFFE' }] },
		CBMIR_HEX + 'FFFFFFFF'), CBMIR_HEX + 'FFFFFFFF');
	assert.throws(() => efEncCbmir({ ranges: [{ low: '0000', high: 'XYZ' }] }, CBMIR_HEX), /1-4 hex digits/);
	assert.throws(() => efEncCbmir({ ranges: [
		{ low: '0000', high: 'FFFE' }, { low: '0100', high: '02FF' },
	] }, CBMIR_HEX), /Too many entries/);
});

// EF.SMSP (TS 31.102 4.2.27): alpha (Y = len-28), the indicator byte, the
// DA/SCA slots, PID, DCS and VP.  The live fixture and the 44-byte vector.
const SMSP_FIX = 'ffffffffffffffffffffffffffe9ffffffffffffffffffffffff07919733824009f0ffffffff0000ff';
const SMSP_VEC = '534d5343ffffffffffffffffffffffffe1ffffffffffffffffffffffff0891945197109099f9ffffff0000a9';

test('EF.SMSP: the live fixture model and the no-edit round-trip', () => {
	const entry = efFindEncoder('EF.SMSP', null);
	assert.strictEqual(entry.fid, '6f42');
	assert.ok(entry.custom, 'the SMSP entry has a custom builder');
	assert.deepStrictEqual(efSmspModel(SMSP_FIX), {
		alpha: '',
		params: {
			da: { present: false, orig: 'FFFFFFFFFFFFFFFFFFFFFFFF', number: '', ton: 1, alnum: false },
			sca: { present: true, orig: '07919733824009F0FFFFFFFF', number: '+79332804900', ton: 1, alnum: false },
			pid: { present: true, orig: '00', value: '0x00' },
			dcs: { present: false, orig: '00', value: '0x00' },
			vp: { present: true, orig: 'FF', value: '0xFF' },
		},
	});
	assert.strictEqual(efEncSmsp(efSmspModel(SMSP_FIX), SMSP_FIX), SMSP_FIX.toUpperCase());
	assert.strictEqual(efEncSmsp(efSmspModel(SMSP_VEC), SMSP_VEC), SMSP_VEC.toUpperCase());
});

test('EF.SMSP: SCA/DA edits and the two length conventions', () => {
	const base = efSmspModel(SMSP_FIX);
	const clone = () => JSON.parse(JSON.stringify(base));
	// SCA length = TON/NPI + BCD octets
	let v = clone();
	v.params.sca.number = '+4915790109999';
	let out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(52, 56), '0891');   // len 8, TON/NPI 91
	assert.deepStrictEqual(efSmspModel(out).params.sca.number, '+4915790109999');
	// SCA empty = the default SMSC (length 0)
	v = clone();
	v.params.sca.number = '';
	out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(52, 56), '00FF');
	assert.strictEqual(efSmspModel(out).params.sca.number, '');
	// DA length = the digit count; TON 0 -> TON/NPI 01
	v = clone();
	Object.assign(v.params.da, { present: true, number: '12345', ton: 0 });
	out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(26, 28), 'E8');     // DA bit cleared
	assert.strictEqual(out.slice(28, 52), '05812143F5FFFFFFFFFFFFFF');
	assert.deepStrictEqual(efSmspModel(out).params.da.number, '12345');
	// 20 digits fit, 21 are refused
	v = clone();
	Object.assign(v.params.da, { present: true, number: '1'.repeat(20), ton: 1 });
	assert.strictEqual(efEncSmsp(v, SMSP_FIX).slice(28, 30), '14');
	v.params.da.number = '1'.repeat(21);
	assert.throws(() => efEncSmsp(v, SMSP_FIX), /max 20 digits/);
	assert.throws(() => efEncSmsp({ ...clone(), params: { ...base.params,
		da: { present: true, number: '', ton: 1, orig: '' } } }, SMSP_FIX), /TP-DA number is required/);
	assert.throws(() => efEncSmsp({ ...clone(), params: { ...base.params,
		sca: { present: true, number: '12A', ton: 1, orig: '' } } }, SMSP_FIX), /digits/);
});

test('EF.SMSP: presence toggles and the indicator byte', () => {
	const base = efSmspModel(SMSP_FIX);
	const clone = () => JSON.parse(JSON.stringify(base));
	// DCS newly present: E9 -> E1 (bit 4 cleared), the value written
	let v = clone();
	v.params.dcs.present = true;
	v.params.dcs.value = '0x08';
	let out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(26, 28), 'E1');
	assert.strictEqual(out.slice(78, 80), '08');
	// SCA newly absent: the bit set and the slot FF-filled
	v = clone();
	v.params.sca.present = false;
	out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(26, 28), 'EB');
	assert.strictEqual(out.slice(28, 52), 'FF'.repeat(12));
	// PID newly absent: E9 -> ED
	v = clone();
	v.params.pid.present = false;
	out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(26, 28), 'ED');
	assert.strictEqual(out.slice(76, 78), 'FF');
	// an untouched file keeps the original indicator byte (reserved bits too)
	assert.strictEqual(efEncSmsp(base, SMSP_FIX).slice(26, 28), 'E9');
});

test('EF.SMSP: alpha, PID/DCS/VP hex and the refusals', () => {
	const base = efSmspModel(SMSP_FIX);
	const clone = () => JSON.parse(JSON.stringify(base));
	let v = clone();
	v.alpha = 'Test';
	let out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(0, 8), '54657374');
	assert.strictEqual(efSmspModel(out).alpha, 'Test');
	assert.throws(() => efEncSmsp({ ...clone(), alpha: 'A'.repeat(14) }, SMSP_FIX), /alpha identifier is too long/);
	v = clone();
	v.params.pid.value = '0x7F';
	v.params.vp.value = 'A8';
	v.params.dcs = { present: true, orig: '', value: 'F4' };
	out = efEncSmsp(v, SMSP_FIX);
	assert.deepStrictEqual([out.slice(76, 78), out.slice(78, 80), out.slice(80, 82)], ['7F', 'F4', 'A8']);
	for (const key of ['pid', 'dcs', 'vp']) {
		const bad = clone();
		bad.params[key] = { ...bad.params[key], present: true, value: 'ZZ' };
		assert.throws(() => efEncSmsp(bad, SMSP_FIX), /2 hex digits/, key);
	}
	assert.throws(() => efEncSmsp({ ...clone(), params: { ...base.params,
		sca: { present: true, number: '12A', ton: 1, orig: '' } } }, SMSP_FIX), /digits/);
	assert.throws(() => efEncSmsp(base, 'FF'.repeat(27)), /too short for the SMSP layout/);
});

test('EF.SMSP: an all-FF 28-byte record round-trips', () => {
	const ff = 'FF'.repeat(28);
	const model = efSmspModel(ff);
	assert.strictEqual(model.alpha, '');
	for (const key of ['da', 'sca', 'pid', 'dcs', 'vp']) assert.strictEqual(model.params[key].present, false, key);
	assert.strictEqual(efEncSmsp(model, ff), ff);
});

test('EF.SMSP: the TS 23.038 DCS hint', () => {
	assert.strictEqual(smspDcsHint('00'), 'general data coding, GSM 7-bit, uncompressed');
	assert.strictEqual(smspDcsHint('10'), 'general data coding, GSM 7-bit, uncompressed, class 0');
	assert.strictEqual(smspDcsHint('08'), 'general data coding, UCS2, uncompressed');
	assert.strictEqual(smspDcsHint('F4'), 'data coding, 8-bit data, class 0');
	assert.strictEqual(smspDcsHint('D0'), 'MWI store, GSM 7-bit, inactive, voicemail');
	assert.strictEqual(smspDcsHint('C0'), 'MWI discard, GSM 7-bit, inactive, voicemail');
	assert.strictEqual(smspDcsHint('88'), 'reserved coding group');
	assert.strictEqual(smspDcsHint('ZZ'), '');
});

test('EF.SMSP: the TS 23.040 relative VP hint', () => {
	const cases = { '00': '5 min', '8F': '12 h', '90': '12 h 30 min', 'A7': '24 h',
		'A8': '2 days', 'C4': '30 days', 'C5': '5 weeks', 'FF': '63 weeks' };
	for (const [hex, want] of Object.entries(cases)) assert.strictEqual(smspVpHint(hex), want, hex);
	assert.strictEqual(smspVpHint('ZZ'), '');
});

// A present DA slot with TON 5 (alphanumeric, TS 23.040 9.1.2.5).
const SMSP_ALNUM = 'FF'.repeat(13) + 'FE' + '0750414243444546FFFFFFFF'
	+ 'FF'.repeat(12) + 'FF' + 'FF' + 'FF';
// The live fixture with the SCA absent (indicator EB, FF slot).
const SMSP_SCA_ABSENT = SMSP_FIX.slice(0, 26) + 'eb' + SMSP_FIX.slice(28, 52)
	+ 'FF'.repeat(12) + SMSP_FIX.slice(76);
// The live fixture with VP absent (indicator F9, FF slot).
const SMSP_VP_ABSENT = SMSP_FIX.slice(0, 26) + 'f9' + SMSP_FIX.slice(28, 80) + 'ff';

test('EF.SMSP: a TON 5 alphanumeric address is kept verbatim and only toggles', () => {
	const model = efSmspModel(SMSP_ALNUM);
	assert.strictEqual(model.params.da.present, true);
	assert.strictEqual(model.params.da.alnum, true);
	assert.strictEqual(model.params.da.ton, 5);
	assert.strictEqual(model.params.da.number, '');
	// an untouched round trip is byte-identical (no BCD re-encode)
	assert.strictEqual(efEncSmsp(model, SMSP_ALNUM), SMSP_ALNUM.toUpperCase());
	// editing another field leaves the alphanumeric slot verbatim
	const edited = JSON.parse(JSON.stringify(model));
	edited.alpha = 'X';
	const out = efEncSmsp(edited, SMSP_ALNUM);
	assert.strictEqual(out.slice(0, 4), '58FF');
	assert.strictEqual(out.slice(28, 52), '0750414243444546FFFFFFFF');
	// toggling it absent FF-fills the slot and sets its indicator bit
	const absent = JSON.parse(JSON.stringify(model));
	absent.params.da.present = false;
	const off = efEncSmsp(absent, SMSP_ALNUM);
	assert.strictEqual(off.slice(26, 28), 'FF');
	assert.strictEqual(off.slice(28, 52), 'FF'.repeat(12));
});

test('EF.SMSP: the VP presence bit (0x10) on decode and encode', () => {
	const dec = efDecSmsp(efBytes(SMSP_VP_ABSENT));
	assert.strictEqual(dec.indicators, '0xF9');
	assert.strictEqual(dec.tp_vp, undefined);
	assert.strictEqual(dec.sca, '+79332804900');
	assert.strictEqual(efEncSmsp(efSmspModel(SMSP_VP_ABSENT), SMSP_VP_ABSENT), SMSP_VP_ABSENT.toUpperCase());
	const model = efSmspModel(SMSP_FIX);
	model.params.vp.present = false;
	const out = efEncSmsp(model, SMSP_FIX);
	assert.strictEqual(out.slice(26, 28), 'F9');
	assert.strictEqual(out.slice(80, 82), 'FF');
});

test('EF.SMSP: a newly present empty SCA writes the default-SMSC slot', () => {
	const model = efSmspModel(SMSP_SCA_ABSENT);
	assert.strictEqual(model.params.sca.present, false);
	model.params.sca.present = true;
	model.params.sca.number = '';
	const out = efEncSmsp(model, SMSP_SCA_ABSENT);
	assert.strictEqual(out.slice(26, 28), 'E9');   // the SCA bit cleared
	assert.strictEqual(out.slice(52, 56), '00FF'); // length 0 + FF fill
});

test('EF.SMSP: a leading + forces TON 1 (international)', () => {
	const base = efSmspModel(SMSP_FIX);
	const v = JSON.parse(JSON.stringify(base));
	v.params.sca = { present: true, number: '+4915790109999', ton: 0 };
	const out = efEncSmsp(v, SMSP_FIX);
	assert.strictEqual(out.slice(54, 56), '91');
	const da = JSON.parse(JSON.stringify(base));
	Object.assign(da.params.da, { present: true, number: '+12345', ton: 2 });
	const out2 = efEncSmsp(da, SMSP_FIX);
	assert.strictEqual(out2.slice(30, 32), '91');  // extension + TON 1 + NPI
});

test('EF.SMSP: the day/week VP ladder boundaries', () => {
	assert.strictEqual(smspVpHint('A8'), '2 days');
	assert.strictEqual(smspVpHint('AD'), '7 days');
	assert.strictEqual(smspVpHint('C2'), '28 days');
	assert.strictEqual(smspVpHint('C4'), '30 days');
	assert.strictEqual(smspVpHint('C5'), '5 weeks');
	assert.strictEqual(smspVpHint('FF'), '63 weeks');
});

test('EF.SMSP: the remaining DCS hint branches', () => {
	assert.strictEqual(smspDcsHint('40'), 'automatic deletion, GSM 7-bit, uncompressed');
	assert.strictEqual(smspDcsHint('0C'), 'general data coding, reserved, uncompressed');
});

test('EF.SMSP: the duration plural helper (Russian 1 / 2-4 / 5+)', () => {
	assert.deepStrictEqual([1, 2, 4, 5, 11, 21, 22, 25].map(smspPluralIndex), [0, 1, 1, 2, 2, 0, 1, 2]);
	globalThis.currentLang = 'ru';
	globalThis.t = k => ({
		day: 'день', 'days-few': 'дня', days: 'дней',
		week: 'неделя', 'weeks-few': 'недели', weeks: 'недель',
	})[k] || k;
	try {
		assert.strictEqual(smspCount(1, 'day', 'days-few', 'days'), '1 день');
		assert.strictEqual(smspCount(2, 'day', 'days-few', 'days'), '2 дня');
		assert.strictEqual(smspCount(5, 'day', 'days-few', 'days'), '5 дней');
		assert.strictEqual(smspCount(2, 'week', 'weeks-few', 'weeks'), '2 недели');
		assert.strictEqual(smspCount(5, 'week', 'weeks-few', 'weeks'), '5 недель');
	} finally {
		delete globalThis.currentLang;
		delete globalThis.t;
	}
	assert.strictEqual(smspCount(2, 'day', 'days-few', 'days'), '2 days');
});
