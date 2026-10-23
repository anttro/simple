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
	'efDecIccid', 'efDecImsi', 'efDecLi', 'efDecServiceTable', 'efDecSst', 'efDecPlmnList', 'efDecFplmn', 'efDecPlmnWact',
	'efDecSpn', 'efDecLoci', 'efDecPsLoci', 'efDecEpsLoci', 'efDecEpsNsc', 'efDecKc', 'efDecAcc',
	'efDecPhase', 'efDecCbmi', 'efDecCbmir', 'efDecHpplmn', 'efDecEcc', 'efDecOpl', 'efDecAd', 'efDecAcl',
	'efSmspDaNumber', 'efDecSmsp', 'efDecSpdi', 'efDecNai', 'efDecDir', 'efDecArr', 'efDecPnn', 'efDecAdn',
	'pinKeyRefName', 'efArrAmLabel', 'efArrSc', 'efArrAmDo',
	'efDecExt1', 'efDecSms', 'efDecSume', 'efPbrHex', 'efDecPbr', 'efFindDecoder', 'efFidFromPath', 'efDecodeBytes',
	'efDecodeFile', 'efDecodeHex', 'efIsRawOnly', 'efFieldLabel', 'efPrimitive', 'efFlatten',
	'efDiffData', 'efDataSummary', 'efContentDiff', 'gsm7Decode', 'efRenderFieldsHtml',
	'pysimFsDecodedHtml', 'profilerRenderReport', 'profilerLabelText', 'profilerResultAspects',
	'profilerAspectSummary', 'profilerMatchedRecordsText', 'profilerRawDataCheck', 'profilerCustomNameForPath',
	'pysimCustomNormPath',
];

let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['GSM7_ALPHABET', 'GSM7_EXT_MAP', 'FCP_ARR_AM_BITS']) {
	code += html.match(new RegExp('const ' + c + ' = [\\s\\S]*?\\n\\];'))[0].replace('const ', 'var ') + '\n';
}
for (const c of ['EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_EST_SERVICES', 'EF_PHASE_NAMES', 'EF_PBR_KINDS']) {
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
	assert.deepStrictEqual(efDecAcc(efBytes('8000')), { access_classes: [15] });
	assert.deepStrictEqual(efDecAcc(efBytes('0001')), { access_classes: [0] });
	assert.deepStrictEqual(efDecAcc(efBytes('8001')), { access_classes: [15, 0] });
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
	assert.deepStrictEqual(efDecPhase(efBytes('03')), { phase: 'phase 2 and PROFILE DOWNLOAD required' });
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

test('EF.PBR decodes the phonebook structure containers (TS 31.102 4.4.2.1)', () => {
	// pySim's EF_PBR fixture: A8 { C0 03 4F3A 02, C5 03 4F09 04 }
	// + AA { CB 03 4F3D 07, C2 03 4F4A 06 } - all with an SFI.
	const d = efDecPbr(efBytes('A80AC0034F3A02C5034F0904AA0ACB034F3D07C2034F4A06'));
	assert.strictEqual(d.master, '4F3A');
	assert.strictEqual(d.iap, null);
	assert.strictEqual(d.note, undefined);
	assert.deepStrictEqual(d.links, [
		{ link: 1, tag: 'C0', kind: 'EF.ADN', fid: '4F3A', sfi: '02' },
		{ link: 1, tag: 'C5', kind: 'EF.PBC', fid: '4F09', sfi: '04' },
		{ link: 3, tag: 'CB', kind: 'EF.CCP1', fid: '4F3D', sfi: '07' },
		{ link: 3, tag: 'C2', kind: 'EF.EXT1', fid: '4F4A', sfi: '06' },
	]);
	assert.deepStrictEqual(d.files, ['4F3A', '4F09', '4F3D', '4F4A']);
});

test('EF.PBR: the no-SFI form and the type-2 (A9) container with EF_IAP', () => {
	// A8 { C0 02 4F3A (FID only, no SFI), C1 02 4F09 (EF_IAP) }
	// + A9 { C6 03 4F0B 06, C7 03 4F0C 07 } - the type-2 files are addressed
	// through the EF_IAP pointers, in the order of the FIDs after 'A9'.
	const d = efDecPbr(efBytes('A808C0024F3AC1024F09A90AC6034F0B06C7034F0C07'));
	assert.strictEqual(d.master, '4F3A');
	assert.strictEqual(d.iap, '4F09');
	assert.deepStrictEqual(d.links, [
		{ link: 1, tag: 'C0', kind: 'EF.ADN', fid: '4F3A', sfi: null },
		{ link: 1, tag: 'C1', kind: 'EF.IAP', fid: '4F09', sfi: null },
		{ link: 2, tag: 'C6', kind: 'EF.GRP', fid: '4F0B', sfi: '06' },
		{ link: 2, tag: 'C7', kind: 'EF.AAS', fid: '4F0C', sfi: '07' },
	]);
	assert.deepStrictEqual(d.files, ['4F3A', '4F09', '4F0B', '4F0C']);
	assert.strictEqual(d.note, undefined);
});

test('EF.PBR: FF padding is not a bogus link; a truncated TLV is a note, not a throw', () => {
	const padded = efDecPbr(efBytes('A80AC0034F3A02C5034F0904AA0ACB034F3D07C2034F4A06' + 'FF'.repeat(8)));
	assert.strictEqual(padded.links.length, 4);
	assert.strictEqual(padded.note, undefined);
	assert.deepStrictEqual(padded.files, ['4F3A', '4F09', '4F3D', '4F4A']);
	// a trailing 'C5' tag with no length byte: the parsed links are kept
	const trunc = efDecPbr(efBytes('A80AC0034F3A02C5034F0904C5'));
	assert.strictEqual(trunc.master, '4F3A');
	assert.strictEqual(trunc.links.length, 2);
	assert.match(trunc.note, /truncated TLV/);
	// a container whose value runs past the record: the earlier links are kept
	const bad = efDecPbr(efBytes('A80AC0034F3A02C5034F0904AA0ACB034F3D07'));
	assert.strictEqual(bad.links.length, 2);
	assert.match(bad.note, /truncated TLV/);
});

test('EF.PBR: unknown primitive tags are preserved raw with a note', () => {
	// A8 { C0 03 4F3A 02 (master), CE 03 4F09 04 (not a phonebook kind) }
	const d = efDecPbr(efBytes('A80AC0034F3A02CE034F0904'));
	assert.strictEqual(d.master, '4F3A');
	assert.deepStrictEqual(d.links, [
		{ link: 1, tag: 'C0', kind: 'EF.ADN', fid: '4F3A', sfi: '02' },
		{ link: 1, tag: 'CE', kind: null, fid: '4F09', sfi: '04', raw: '4F0904' },
	]);
	assert.deepStrictEqual(d.files, ['4F3A']);
	assert.match(d.note, /CE/);
});

test('EF.PBR: the AA non-primitive fallback keeps the raw bytes and notes it', () => {
	const d = efDecPbr(efBytes('AA03C0FF01'));
	assert.deepStrictEqual(d.links, [
		{ link: 3, tag: 'AA', kind: null, fid: null, sfi: null, raw: 'C0FF01' },
	]);
	assert.match(d.note, /not a primitive list/);
	// an all-FF record is the unused marker
	assert.deepStrictEqual(efDecPbr(efBytes('FFFFFFFF')), { empty: true });
});

test('EF.PBR matches the shared decode vectors (drift guard with the server parser)', () => {
	// frontend/tests/pbr_vectors.json is asserted by tests/test_pbr.py too:
	// the PWA decoder and the server-side PBR parser must agree.
	const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'pbr_vectors.json'), 'utf8'));
	assert.ok(vectors.cases.length >= 5);
	for (const c of vectors.cases) {
		const d = efDecPbr(efBytes(c.records[0]));
		assert.strictEqual(d.master, c.master, c.name + ': master');
		assert.deepStrictEqual(d.links, c.links, c.name + ': links');
		assert.deepStrictEqual(d.files, c.files, c.name + ': files');
	}
});

test('EF.PBR is registered by name and FID; efFlatten renders link rows', () => {
	assert.strictEqual(efFindDecoder('EF.PBR', null).fid, '4f30');
	assert.strictEqual(efFindDecoder(null, '4f30').name, 'EF.PBR');
	assert.strictEqual(efFindDecoder('MF/7F10/5F3A/EF.PBR', null).fn, efDecPbr);
	assert.deepStrictEqual(efFlatten({ master: '4F3A', iap: '4F09', links: [
		{ link: 1, tag: 'C0', kind: 'EF.ADN', fid: '4F3A', sfi: '02' },
		{ link: 3, tag: 'AA', kind: null, fid: null, sfi: null, raw: 'C0FF01' },
	], files: ['4F3A'] }), [
		['Master EF', '4F3A'],
		['IAP (EF.IAP)', '4F09'],
		['Links 1', 'Type 1 \u00b7 EF.ADN \u2014 4F3A (SFI 02)'],
		['Links 2', 'Type 3 \u00b7 tag AA \u2014 C0FF01'],
		['Files', '4F3A'],
	]);
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
	// EF.ARR: AM_DO 01 (read) guarded by ADM1 (TS 102 221 9.2.7 / ISO 7816-4 5.4.3.2)
	const arr = efDecArr(efBytes('800101a40683010a950108'));
	assert.deepStrictEqual(arr.rules, ['AM 0x01 (READ/SEARCH (EF) / DELETE FILE child (DF)): ADM1 (verify)']);
	// Two rules: update needs PIN app1 or app2, read is always
	assert.deepStrictEqual(
		efDecArr(efBytes('800102a010a406830101950108a4068301029501088001019000')).rules,
		['AM 0x02 (UPDATE/ERASE (EF) / CREATE FILE EF (DF)): (PIN app1 (verify) OR PIN app2 (verify))',
			'AM 0x01 (READ/SEARCH (EF) / DELETE FILE child (DF)): always']);
	// INCREASE / RESIZE ride AM_DO 0x84 with the command INS as value
	assert.deepStrictEqual(efDecArr(efBytes('8401329000')).rules,
		['INCREASE (AM_DO 0x84, INS 0x32): always']);
	assert.deepStrictEqual(efDecArr(efBytes('8401D49000')).rules,
		['RESIZE FILE (AM_DO 0x84, INS 0xD4): always']);
	// b8=1: bits 7-4 are proprietary
	assert.deepStrictEqual(efDecArr(efBytes('8001819000')).rules,
		['AM 0x81 (bits 7-4 proprietary + READ/SEARCH (EF) / DELETE FILE child (DF)): always']);
	const impi = efDecNai(efBytes('803137333830303630303030303031303140696d732e6d6e633030302e6d63633733382e336770706e6574776f726b2e6f7267'));
	assert.strictEqual(impi.text, '738006000000101@ims.mnc000.mcc738.3gppnetwork.org');
	const smsp = efDecSmsp(efBytes('534d5343ffffffffffffffffffffffffe1ffffffffffffffffffffffff0891945197109099f9ffffff0000a9'));
	assert.strictEqual(smsp.alpha, 'SMSC');
	assert.strictEqual(smsp.sca, '+4915790109999');
	assert.strictEqual(smsp.tp_pid, '0x00');
	assert.strictEqual(smsp.tp_dcs, '0x00');
	assert.strictEqual(smsp.tp_vp, '0xA9');
	assert.strictEqual(smsp.indicators, '0xE1');
	assert.strictEqual(smsp.tp_da, undefined);
	// EF.SMS status byte + skip SMSC + TPDU
	const sms = efDecSms(efBytes('030891945197109099f90000'));
	assert.strictEqual(sms.direction, 'MT');
	assert.strictEqual(sms.status, 'message to be read');
	assert.strictEqual(sms.tpdu, '0000');
});

test('EF.SMSP decodes the indicator bits (TS 31.102 4.2.27)', () => {
	// live card record: DA absent (0x01), SCA present, PID present, DCS absent
	// (0x08), VP present; bits 6..8 reserved = 1 -> 0xE9
	const live = efDecSmsp(efBytes('ffffffffffffffffffffffffffe9ffffffffffffffffffffffff07919733824009f0ffffffff0000ff'));
	assert.deepStrictEqual(live, {
		indicators: '0xE9',
		sca: '+79332804900',
		tp_pid: '0x00',
		tp_vp: '0xFF',
	});
	assert.strictEqual(live.tp_da, undefined);
	assert.strictEqual(live.tp_dcs, undefined);   // DCS absent, its 00 slot kept raw for the editor
});

test('EF.SMSP reads the TP-DA length as a digit count (00 padding is not ABS)', () => {
	// alpha FF x13 | indicator FE (DA present, rest absent) |
	// DA 04 81 21 43 00 00 FF... ("1234" padded with 00, not FF)
	const hex = 'FF'.repeat(13) + 'FE' + '048121430000FFFFFFFFFFFF'
		+ 'FF'.repeat(12) + 'FF' + 'FF' + 'FF';
	assert.deepStrictEqual(efDecSmsp(efBytes(hex)), {
		indicators: '0xFE',
		tp_da: '1234',
	});
});

test('service tables: UST service bits and IST fallback for 6F07', () => {
	const ust = efDecServiceTable(efBytes('00000010'), EF_UST_SERVICES);
	assert.deepStrictEqual(ust.services, [{ n: 29, name: 'Data download via SMS-CB' }]);
	const ist = efDecImsi(efBytes('01'));
	assert.deepStrictEqual(ist.services, [{ n: 1, name: 'P-CSCF address' }]);
});

test('EF.SST is the SIM 2-bit service table (allocated/activated)', () => {
	// TS 51.011 10.3.7: service 2 ADN -> byte 0, pair start bit 2 (0x04
	// allocated, 0x08 activated)
	assert.deepStrictEqual(efDecSst(efBytes('0C')), { services: [
		{ n: 2, name: 'Abbreviated Dialling Numbers (ADN)', allocated: true, activated: true },
	] });
	assert.deepStrictEqual(efDecSst(efBytes('04')), { services: [
		{ n: 2, name: 'Abbreviated Dialling Numbers (ADN)', allocated: true, activated: false },
	] });
	assert.deepStrictEqual(efDecSst(efBytes('08')), { services: [
		{ n: 2, name: 'Abbreviated Dialling Numbers (ADN)', allocated: false, activated: true },
	] });
	// byte 1 pair 3 (bits 6/7) = service 4 SMS; byte 0 pair 1 = service 2
	assert.deepStrictEqual(efDecSst(efBytes('C0')), { services: [
		{ n: 4, name: 'Short Message Storage (SMS)', allocated: true, activated: true },
	] });
	assert.deepStrictEqual(efDecSst(efBytes('00')), { services: [] });
	assert.deepStrictEqual(efFindDecoder('EF.SST', null).fn(efBytes('0C')).services[0].activated, true);
	// the read-only view shows both bits
	assert.ok(efFlatten(efDecSst(efBytes('0C'))).some(r =>
		r[0] === 'Service 2' && r[1].includes('allocated') && r[1].includes('activated')));
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
	assert.deepStrictEqual(efFlatten({ show_in_hplmn: true }), [['Display the network name in HPLMN / an SPDI PLMN', 'yes']]);
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

test('efFlatten skips the all-FF empty marker', () => {
	assert.deepStrictEqual(efFlatten({ empty: true }), []);
});

test('efDiffData names an all-FF side Empty (all FF)', () => {
	assert.deepStrictEqual(efDiffData({ empty: true }, { full: 'Miran' }),
		[['Full name', 'Empty (all FF)', 'Miran']]);
	assert.deepStrictEqual(efDiffData({ full: 'Miran' }, { empty: true }),
		[['Full name', 'Miran', 'Empty (all FF)']]);
	assert.deepStrictEqual(efDiffData({ empty: true }, { empty: true }), []);
	// a field merely missing from a non-empty record stays blank
	assert.deepStrictEqual(efDiffData({ full: 'Miran' }, { full: 'Miran', short: 'Mir' }),
		[['Short name', '', 'Mir']]);
});

test('efContentDiff reports an empty PNN record side', () => {
	const expected = 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';
	const actual = '430887CDB43CEC2687FFFFFFFFFFFFFFFFFFFFFF';
	assert.deepStrictEqual(efDecPnn(efBytes(actual)), { full: 'Miran' });
	const dd = efContentDiff('EF.PNN', '6fc5', expected, actual);
	assert.ok(dd, 'decoded diff expected');
	assert.deepStrictEqual(dd.rows, [['Full name', 'Empty (all FF)', 'Miran']]);
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
	// The view is the client-side decoder's table only (v3.32.7): the
	// server-side pySim JSON disclosure is gone - it was an unrelated second
	// read of the card, not an input to this decode.
	assert.doesNotMatch(out, /pySim JSON/);
	assert.doesNotMatch(out, /\{"imsi"/);
	// the label column is content-sized, not a fixed 10rem block
	assert.match(out, /grid-template-columns:max-content 1fr/);
	assert.doesNotMatch(out, /w-40/);
	// no decoder -> raw fallback with a note
	const raw = pysimFsDecodedHtml({ name: 'EF.NOPE', fid: 'abcd' }, { success: true, data: '0011' });
	assert.match(raw, /No decoder for this file/);
	assert.match(raw, /0011/);
});

test('the file manager never requests the server-side decode', () => {
	// The decoded table is client-side (the raw read's bytes); the pySim JSON
	// lookup - a second /api/read with mode 'decoded' - is gone (v3.32.7).
	assert.ok(!html.includes("mode = 'decoded'"));
	assert.ok(!html.includes('pysimFsLoadPySimJson'));
	assert.ok(!html.includes('pysim-fs-pysim-json'));
	assert.ok(!html.includes('pySim JSON (server)'));
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

test('EF.BDN uses the 15-byte ADN layout (TS 31.102 4.2.44)', () => {
	// alpha 'TEST' | len 06 | TON/NPI 91 | BCD 21 43 65 87 09 + FF padding |
	// CCP FF | EXT4 FF | EF.CMI comparison-method pointer 03
	const hex = '54455354' + '06' + '91' + '2143658709' + 'FF'.repeat(7) + '03';
	const bdn = efBytes(hex);
	const want = { name: 'TEST', number: '+1234567890', cmi: 3 };
	assert.strictEqual(bdn.length, 19);
	assert.deepStrictEqual(efDecAdn(bdn, 15), want);
	assert.deepStrictEqual(efFindDecoder('EF.BDN').fn(bdn), want);
	// the 14-byte default and the EF.ADN registry entry are unchanged
	const adn = efBytes('42204841203120536963FFFFFFFFFFFF06810628560810FFFFFFFFFFFFFF');
	const adnWant = { name: 'B HA 1 Sic', number: '6082658001' };
	assert.deepStrictEqual(efDecAdn(adn), adnWant);
	assert.deepStrictEqual(efFindDecoder('EF.ADN').fn(adn), adnWant);
	assert.notDeepStrictEqual(efDecAdn(adn, 15), adnWant);
});

test('the BDN extension and CMI fields carry readable labels', () => {
	assert.deepStrictEqual(efFlatten({ cmi: 3 }), [['Comparison method pointer', '3']]);
	assert.deepStrictEqual(efFlatten({ ext1: 2 }), [['Extension record', '2']]);
	const bdn = efDecAdn(efBytes('54455354' + '06' + '91' + '2143658709' +
		'FF'.repeat(7) + '03'), 15);
	assert.ok(efFlatten(bdn).some(r => r[0] === 'Comparison method pointer' && r[1] === '3'));
});
