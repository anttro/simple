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

const FNS = [
	'efBytes', 'efHex', 'efSwapNibbles', 'efAllFf', 'efRstripFf', 'efTlv', 'efUcs2Be',
	'efGsm7Octets', 'efAnnexA', 'efBcdAddress', 'efBcdAddressLen', 'efPlmn', 'efPlmnAct', 'efPnnText',
	'efDecIccid', 'efDecImsi', 'efDecLi', 'efDecServiceTable', 'efDecPlmnList', 'efDecFplmn', 'efDecPlmnWact',
	'efDecSpn', 'efDecLoci', 'efDecPsLoci', 'efDecEpsLoci', 'efDecEpsNsc', 'efDecKc', 'efDecAcc',
	'efDecPhase', 'efDecCbmi', 'efDecCbmir', 'efDecEcc', 'efDecOpl', 'efDecAd', 'efDecAcl',
	'efDecSmsp', 'efDecSpdi', 'efDecNai', 'efDecDir', 'efDecArr', 'efDecPnn', 'efDecAdn',
	'efDecExt1', 'efDecSms', 'efDecSume', 'efFindDecoder', 'efFidFromPath', 'efDecodeBytes',
	'efDecodeFile', 'efDecodeHex', 'efIsRawOnly', 'efFieldLabel', 'efPrimitive', 'efFlatten',
	'efDiffData', 'efDataSummary', 'efContentDiff', 'gsm7Decode', 'efRenderFieldsHtml',
	'pysimFsDecodedHtml', 'profilerRenderReport', 'profilerLabelText', 'profilerResultAspects',
	'profilerAspectSummary', 'profilerMatchedRecordsText', 'profilerRawDataCheck', 'profilerCustomNameForPath',
	'pysimCustomNormPath',
];

let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['GSM7_ALPHABET', 'GSM7_EXT_MAP']) {
	code += html.match(new RegExp('const ' + c + ' = [\\s\\S]*?\\n\\];'))[0].replace('const ', 'var ') + '\n';
}
for (const c of ['EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_PHASE_NAMES']) {
	code += html.match(new RegExp('const ' + c + ' = \\{.*\\};'))[0].replace('const ', 'var ') + '\n';
}
code += html.match(/const EF_FIELD_LABELS = \{[\s\S]*?\n\};/)[0].replace('const ', 'var ') + '\n';
code += html.match(/const EF_DECODERS = \[[\s\S]*?\n\];/)[0].replace('const ', 'var ') + '\n';
code += 'var pysimCustomFiles = [];\nvar pysimFsSelected = \'EF.IMSI\';\n';
code += 'function esc(s){return String(s);}\nfunction escHtml(s){return String(s);}\nfunction t(k){return k;}\n';
eval(code);

test('ef helpers: bytes, hex, swap, TLV, PLMN', () => {
	assert.strictEqual(efHex(efBytes('a0ff01')), 'A0FF01');
	assert.strictEqual(efSwapNibbles('1234'), '2143');
	assert.strictEqual(efPlmn(efBytes('22F860')).mcc, '228');
	assert.strictEqual(efPlmn(efBytes('22F860')).mnc, '06');
	assert.ok(efAllFf(efBytes('FFFF')));
	assert.ok(!efAllFf(efBytes('FF00')));
	// 05 03 AA BB CC + 1E 01 DD
	const tlvs = efTlv(efBytes('0503AABBCC1E01DD'));
	assert.deepStrictEqual(tlvs.map(t => t.tag), [0x05, 0x1E]);
	assert.strictEqual(efHex(tlvs[0].value), 'AABBCC');
	assert.strictEqual(efHex(tlvs[1].value), 'DD');
	// long-form length
	assert.strictEqual(efTlv(efBytes('058105' + '00'.repeat(5)))[0].value.length, 5);
});

test('efAnnexA decodes GSM7 octets, UCS2 and packed variants', () => {
	assert.strictEqual(efAnnexA(efBytes('47534D2D52204348')), 'GSM-R CH');       // one octet per char
	assert.strictEqual(efAnnexA(efBytes('8000470053004D')), 'GSM');               // UCS2 prefix
	assert.strictEqual(efAnnexA(efBytes('')), '');
	assert.strictEqual(efAnnexA(efBytes('FFFFFFFF')), '');
});

test('efPnnText uses the spare-bit count (TS 24.008 10.5.3.5a)', () => {
	// Live card EF.PNN records: coding octet + packed GSM-7 text.  The spare
	// bits in the coding octet give the exact character count, so the zero
	// padding must not decode as a trailing '@' (record 2 was 'Miranda@').
	const cases = [
		['8441b6390c', 'Alfa'],
		['87cdb43cec268701', 'Miranda'],
		['83d7b41b', 'Win'],
		['83cdb41c442db3cbeb771b', 'Mir Telekom'],
		['82ab1b885a6697d7ef36', '+7 Telekom'],
		['85c6b23b8d07', 'Fenix'],
		['83cde514', 'MKS'],
	];
	for (const [hex, text] of cases) {
		assert.strictEqual(efPnnText(efBytes(hex)), text, hex);
	}
	// UCS2 coding scheme (octet 3 bits 5-7 = 001)
	assert.strictEqual(efPnnText(efBytes('91004d006900720061006e00640061')), 'Miranda');
	// a final <CR> used as padding is removed (TS 23.038 6.1.2.1.1)
	assert.strictEqual(gsm7Decode(efBytes('cdb43cec26871b'), 8), 'Miranda');
});

test('EF decoders match the pinned spec / pySim test vectors', () => {
	assert.deepStrictEqual(efDecIccid(efBytes('988812010000400310f0')), { iccid: '8988211000000430010' });
	assert.deepStrictEqual(efDecImsi(efBytes('082982608200002080')), { imsi: '228062800000208' });
	assert.deepStrictEqual(efDecLi(efBytes('6465')), { languages: ['de'] });
	assert.deepStrictEqual(efDecLi(efBytes('FFFF')), { languages: [] });
	assert.deepStrictEqual(efDecSpn(efBytes('0147534d2d52204348ffffffffffffffff')), { show_in_hplmn: true, hide_in_oplmn: false, name: 'GSM-R CH' });
	assert.deepStrictEqual(efDecAcc(efBytes('0000')), { access_classes: [] });
	assert.deepStrictEqual(efDecAcc(efBytes('8000')), { access_classes: [0] });
	assert.deepStrictEqual(efDecEcc(efBytes('19f101')), { number: '911', esc: '0x01' });
	assert.deepStrictEqual(efDecKc(efBytes('837d783609a3858f05')), { kc: '837D783609A3858F', ksi: 0 });
	assert.deepStrictEqual(efDecPlmnList(efBytes('22F860')), { plmns: [{ mcc: '228', mnc: '06' }] });
	// EF.FPLMN: a FFFFFF gap in any position is skipped, not a terminator
	assert.deepStrictEqual(efDecFplmn(efBytes('22F860FFFFFF62F210')),
		{ plmns: [{ mcc: '228', mnc: '06' }, { mcc: '262', mnc: '01' }] });
	assert.deepStrictEqual(efDecFplmn(efBytes('FFFFFFFF')), { plmns: [] });
	assert.strictEqual(efFindDecoder('EF.EHPLMN', null).fn, efDecPlmnList);
	assert.strictEqual(efFindDecoder('EF.FPLMN', null).fn, efDecFplmn);
	assert.deepStrictEqual(efDecCbmi(efBytes('0010FFFF0020')), { message_ids: [16, 32] });
	assert.deepStrictEqual(efDecCbmir(efBytes('0000FFFEFFFF')), { ranges: [['0000', 'FFFE']] });
	assert.deepStrictEqual(efDecPhase(efBytes('03')), { phase: 'phase 2 and higher' });
	assert.deepStrictEqual(efDecNai(efBytes('8021696d732e6d6e633030302e6d63633733382e336770706e6574776f726b2e6f7267')),
		{ text: 'ims.mnc000.mcc738.3gppnetwork.org' });
	assert.deepStrictEqual(efDecImsi(efBytes('FFFF')), { empty: true });
	assert.deepStrictEqual(efDecIccid(efBytes('FFFFFFFFFFFF')), { empty: true });
});

test('EF.SUME decodes the SET UP MENU title and icon TLVs (TS 51.011 10.3.34)', () => {
	// 05 05 "Menu!" + 1E 02 01 23 + FF padding
	const d = efDecSume(efBytes('05054D656E75211E020123FFFFFFFF'));
	assert.deepStrictEqual(d, { title: 'Menu!', icon: '0123' });
	assert.deepStrictEqual(efDecSume(efBytes('FFFFFFFF')), { empty: true });
	// UCS2 title: 05 09 80 00 4D 00 65 00 6E 00 75
	assert.deepStrictEqual(efDecSume(efBytes('050980004D0065006E0075FFFFFFFF')),
		{ title: 'Menu' });
});

test('ADN-format records: alpha + BCD number + CCP/EXT1', () => {
	const d = efDecAdn(efBytes('42204841203120536963FFFFFFFFFFFF06810628560810FFFFFFFFFFFFFF'));
	assert.strictEqual(d.name, 'B HA 1 Sic');
	assert.strictEqual(d.number, '6082658001');
	assert.strictEqual(d.ccp, undefined);
	assert.strictEqual(d.ext1, undefined);
	// international number in EF.MSISDN
	const m = efDecAdn(efBytes('ffffffffffffffffffffffffffffffffffffffff04b12143f5ffffffffffffffffff'));
	assert.strictEqual(m.name, '');
	assert.strictEqual(m.number, '12345');
	// EXT1 per TS 51.011 10.5.10: type 02 (additional data) + count 03 + BCD
	assert.deepStrictEqual(efDecExt1(efBytes('02032143F5' + 'FF'.repeat(8))), { number: '12345' });
});

test('EF record decoders: SMS, OPL, DIR, ARR, IMPI/SMSP', () => {
	const opl = efDecOpl(efBytes('62f2100000fffe01'));
	assert.deepStrictEqual(opl, { mcc: '262', mnc: '01', lac_range: ['0000', 'FFFE'], pnn_record: 1 });
	const dir = efDecDir(efBytes('61294f10a0000000871002ffffffff890709000050055553696d31730ea00c80011781025f608203454150'));
	assert.strictEqual(dir.applications.length, 1);
	assert.strictEqual(dir.applications[0].aid, 'A0000000871002FFFFFFFF8907090000');
	assert.strictEqual(dir.applications[0].label, 'USim1');
	const arr = efDecArr(efBytes('800101a40683010a950108'));
	assert.deepStrictEqual(arr.rules, [{ tag: '0x80', hex: '01' }, { tag: '0xA4', hex: '83010A950108' }]);
	const impi = efDecNai(efBytes('803137333830303630303030303031303140696d732e6d6e633030302e6d63633733382e336770706e6574776f726b2e6f7267'));
	assert.strictEqual(impi.text, '738006000000101@ims.mnc000.mcc738.3gppnetwork.org');
	const smsp = efDecSmsp(efBytes('534d5343ffffffffffffffffffffffffe1ffffffffffffffffffffffff0891945197109099f9ffffff0000a9'));
	assert.strictEqual(smsp.alpha, 'SMSC');
	assert.strictEqual(smsp.sca, '+4915790109999');
	assert.strictEqual(smsp.tp_pid, '0x00');
	assert.strictEqual(smsp.tp_dcs, '0x00');
	// EF.SMS status byte + skip SMSC + TPDU
	const sms = efDecSms(efBytes('030891945197109099f90000'));
	assert.strictEqual(sms.direction, 'MT');
	assert.strictEqual(sms.status, 'message to be read');
	assert.strictEqual(sms.tpdu, '0000');
});

test('service tables: UST service bits and IST fallback for 6F07', () => {
	const ust = efDecServiceTable(efBytes('00000010'), EF_UST_SERVICES);
	assert.deepStrictEqual(ust.services, [{ n: 29, name: 'Data download via SMS-CB' }]);
	const ist = efDecImsi(efBytes('01'));
	assert.deepStrictEqual(ist.services, [{ n: 1, name: 'P-CSCF address' }]);
});

test('decoder registry resolves by name first, then FID; unknown returns null', () => {
	assert.strictEqual(efFindDecoder('EF.IMSI', null).fid, '6f07');
	assert.strictEqual(efFindDecoder(null, '2fe2').name, 'EF.ICCID');
	assert.strictEqual(efFindDecoder('ADF.USIM/EF.LOCI', null).name, 'EF.LOCI');
	assert.strictEqual(efFindDecoder('EF.SUME', null).fid, '6f54');
	assert.strictEqual(efFindDecoder('EF.NOPE', 'ABCD'), null);
	// mixed-case registry names resolve case-insensitively without a FID
	assert.strictEqual(efFindDecoder('EF.HPLMNwAcT', null).fid, '6f62');
	assert.strictEqual(efFindDecoder('ef.plmnwact', null).name, 'EF.PLMNwAcT');
	assert.strictEqual(efFidFromPath('ADF.USIM/6F07'), '6F07');
	assert.strictEqual(efFidFromPath('MF/7F10/6F3A'), '6F3A');
	assert.strictEqual(efFidFromPath('ADF.USIM'), null);
});

test('efDecodeFile handles transparent and record content', () => {
	const t = efDecodeFile('EF.IMSI', null, { kind: 'transparent', data: '082982608200002080' });
	assert.strictEqual(typeof t.fn, 'function');
	assert.strictEqual(t.data.imsi, '228062800000208');
	const r = efDecodeFile('EF.ADN', '6f3a', { kind: 'record', records: [
		{ num: 1, data: '42204841203120536963FFFFFFFFFFFF06810628560810FFFFFFFFFFFFFF' },
		{ num: 2, data: 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF' },
	] });
	assert.strictEqual(r.records.length, 2);
	assert.strictEqual(r.records[0].data.name, 'B HA 1 Sic');
	assert.strictEqual(r.records[0].empty, false);
	assert.strictEqual(r.records[1].empty, true);
	assert.strictEqual(efDecodeFile('EF.NOPE', null, { kind: 'transparent', data: '00' }), null);
});

test('efFlatten renders fields, services and PLMNs for display', () => {
	assert.deepStrictEqual(efFlatten({ imsi: '123' }), [['IMSI', '123']]);
	assert.deepStrictEqual(efFlatten({ services: [{ n: 5, name: 'Call Control by USIM' }] }),
		[['Service 5', 'Call Control by USIM']]);
	assert.deepStrictEqual(efFlatten({ plmns: [{ mcc: '262', mnc: '01', access_tech: 'GSM' }] }),
		[['PLMN 1', '262-01 (GSM)']]);
	assert.deepStrictEqual(efFlatten({ show_in_hplmn: true }), [['Show in HPLMN', 'yes']]);
	assert.deepStrictEqual(efFlatten({ title: 'Menu!', icon: '0123' }), [['Title', 'Menu!'], ['Icon', '0123']]);
});

test('efDiffData reports only differing fields; efContentDiff decodes both sides', () => {
	assert.deepStrictEqual(efDiffData({ imsi: '1', kc: 'AA' }, { imsi: '2', kc: 'AA' }), [['IMSI', '1', '2']]);
	assert.deepStrictEqual(efDiffData({ imsi: '1' }, { imsi: '1' }), []);
	const dd = efContentDiff('EF.IMSI', null, '082982608200002080', '082982608200002081');
	assert.ok(dd && dd.rows.length === 1);
	assert.strictEqual(dd.rows[0][0], 'IMSI');
	assert.strictEqual(dd.rows[0][1], '228062800000208');
	const same = efContentDiff('EF.IMSI', null, '082982608200002080', '092982608200002080');
	assert.ok(same && same.sameDecoded);
	assert.strictEqual(efContentDiff('EF.NOPE', null, '00', '01'), null);
});

test('efDataSummary produces one-line summaries', () => {
	assert.strictEqual(efDataSummary(efDecImsi(efBytes('082982608200002080'))), 'IMSI 228062800000208');
	assert.strictEqual(efDataSummary(efDecIccid(efBytes('988812010000400310f0'))), 'ICCID 8988211000000430010');
	assert.strictEqual(efDataSummary(efDecSume(efBytes('05054D656E7521FFFFFFFF'))), 'Menu!');
	assert.strictEqual(efDataSummary({ plmns: [{ mcc: '262', mnc: '01' }] }), '262-01');
});

test('file manager decoded view renders fields, not JSON', () => {
	const out = pysimFsDecodedHtml({ name: 'EF.IMSI', fid: null }, { success: true, data: '082982608200002080' });
	assert.match(out, /IMSI/);
	assert.match(out, /228062800000208/);
	assert.match(out, /pySim JSON \(server\)/);
	assert.doesNotMatch(out, /\{"imsi"/);
	// the label column is content-sized, not a fixed 10rem block
	assert.match(out, /grid-template-columns:max-content 1fr/);
	assert.doesNotMatch(out, /w-40/);
	// no decoder -> raw fallback with a note
	const raw = pysimFsDecodedHtml({ name: 'EF.NOPE', fid: 'abcd' }, { success: true, data: '0011' });
	assert.match(raw, /No decoder for this file/);
	assert.match(raw, /0011/);
});

test('efRenderFieldsHtml sizes the label column to its content', () => {
	const out = efRenderFieldsHtml([['ICCID', '8988'], ['Very long label', 'x']]);
	assert.match(out, /style="display:grid;grid-template-columns:max-content 1fr/);
	assert.doesNotMatch(out, /w-40/);
	assert.ok(out.includes('>ICCID</span>'));
	assert.ok(out.includes('>8988</span>'));
	assert.ok(out.includes('>Very long label</span>'));
	assert.match(efRenderFieldsHtml([]), /No decodable fields/);
});

test('profilerRenderReport shows raw and decoded comparisons for content mismatches', () => {
	const res = {
		path: 'ADF.USIM/6F07', name: 'EF.IMSI', status: 'fail',
		checks: [{ label: 'content', expected: '082982608200002080', actual: '082982608200002081', ok: false }],
	};
	const out = profilerRenderReport([res], null);
	// the raw pair is always shown
	assert.match(out, /Raw comparison/);
	assert.ok(out.includes('value="082982608200002080"'));
	assert.ok(out.includes('value="082982608200002081"'));
	// the decoded per-field diff follows, with expected/actual column headers
	assert.match(out, /Decoded comparison/);
	assert.match(out, /IMSI/);
	assert.match(out, /228062800000208/);
	assert.match(out, /228062800000218/);
	assert.match(out, /<span class="flex-1 min-w-0 text-xs text-gray-500 dark:text-slate-400">expected<\/span><span class="flex-1 min-w-0 text-xs text-gray-500 dark:text-slate-400">actual<\/span>/);
	// custom comparison labels are used for the raw rows and the decoded headers
	const named = profilerRenderReport([res], { expected: 'master', actual: 'checked' });
	assert.ok(named.includes('>master</span>'));
	assert.ok(named.includes('>checked</span>'));
	// matching decoded values -> note instead of the decoded table, raw pair kept
	const same = profilerRenderReport([{
		path: 'ADF.USIM/6F07', name: 'EF.IMSI', status: 'fail',
		checks: [{ label: 'content', expected: '082982608200002080', actual: '092982608200002080', ok: false }],
	}], null);
	assert.match(same, /Raw comparison/);
	assert.match(same, /Decoded values match/);
	assert.doesNotMatch(same, /Decoded comparison/);
});
