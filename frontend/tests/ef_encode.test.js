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
	'efGsm7Octets', 'efAnnexA', 'efBcdAddress', 'efPlmn', 'efPlmnAct',
	'efDecSpn', 'efDecPlmnWact', 'efDecAdn', 'efDecPnn', 'efFindDecoder',
	'efDecodeBytes', 'efTlv', 'efPnnText', 'efDecItemsFromHex',
	'efGsm7PlainText', 'efEncTextBytes', 'efPlmnEnc', 'efActEnc', 'efEncSpn',
	'efEncPlmnList', 'efPlmnRowOrig', 'efEncAdn', 'efEncAdnExt', 'efPnnNameBytes', 'efPnnHexBytes', 'efPnnOrigTlv', 'efEncPnn',
	'efFindEncoder', 'efHasEncoder', 'gsm7Decode', 'gsm7TextToSeptets',
	'gsm7Encode', 'efEncError',
];

let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['GSM7_ALPHABET', 'GSM7_EXT_MAP', 'EF_ACT_TECHS', 'EF_SPN_FIELDS',
	'EF_ADN_FIELDS', 'EF_PLMN_LIST', 'EF_PNN_ITEMS', 'EF_ENCODERS']) {
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
	assert.strictEqual(efHasEncoder('EF.IMSI', null), false);
	// one shared schema for the ADN family and one for the three PLMN files
	assert.strictEqual(efFindEncoder('EF.ADN', null).fields, efFindEncoder('EF.BDN', null).fields);
	assert.strictEqual(efFindEncoder('EF.PLMNwAcT', null).list, efFindEncoder('EF.HPLMNwAcT', null).list);
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
	const decode = (name, hex) => {
		const bytes = efBytes(hex);
		if (name === 'EF.SPN') return efDecSpn(bytes);
		if (name === 'EF.PLMNwAcT') return efDecPlmnWact(bytes).plmns;
		if (name === 'EF.BDN') return efDecAdn(bytes, 15);
		if (name === 'EF.ADN') return efDecAdn(bytes);
		if (name === 'EF.PNN') return efDecItemsFromHex(efFindEncoder('EF.PNN', null), hex);
		throw new Error('no decoder for ' + name);
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
