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
	'efBytes', 'efHex', 'efDecServiceTable', 'efDecSst', 'efEncServiceTable', 'efEncSst', 'efEncError',
	'servicesDecode', 'servicesUnmodelled', 'servicesSstState', 'servicesBitState', 'servicesFeatureState',
	'servicesSstAllocated', 'servicesSstPreserved', 'servicesBitsPreserved', 'servicesSstValues',
	'servicesTableValues', 'servicesFileBytes', 'pysimServicesColumns', 'pysimServicesGridHtml',
	'pysimServicesSaveRowHtml', 'pysimServicesCollect',
	'servicesFsDeclared', 'servicesFsPresentFids', 'servicesFsFidNames', 'servicesFsReport',
	'servicesFsReportHtml',
];

let code = 'var _pysimServicesCache = null;\nvar _pysimServicesBusy = false;\n';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_EST_SERVICES',
	'SERVICES_CROSS_REF', 'SERVICES_FILES', 'SERVICES_FS_MAP', 'SERVICES_TABLES']) {
	code += extractConst(html, c);
}
code += 'globalThis.esc = s => String(s);\nglobalThis.t = k => k;\n';
eval(code);

// ---- data ----

test('SERVICES_CROSS_REF covers every non-RFU service of the four files exactly once', () => {
	const tables = { sst: EF_SST_SERVICES, ust: EF_UST_SERVICES, est: EF_EST_SERVICES, ist: EF_IST_SERVICES };
	// The doc's §A.4 RFU/reserved slots: SST 8/20/50, UST 26/33/50/98.
	const rfu = { sst: [8, 20, 50], ust: [26, 33, 50, 98], est: [], ist: [] };
	const seen = { sst: [], ust: [], est: [], ist: [] };
	for (const f of SERVICES_CROSS_REF) {
		for (const k of Object.keys(seen)) {
			if (f[k] === null || f[k] === undefined) continue;
			assert.ok(Number.isInteger(f[k]), 'non-integer ' + k + ' in ' + f.f);
			seen[k].push(f[k]);
		}
	}
	for (const k of Object.keys(seen)) {
		assert.strictEqual(new Set(seen[k]).size, seen[k].length, 'duplicate ' + k + ' numbers');
		const expected = Object.keys(tables[k]).map(Number).filter(n => rfu[k].indexOf(n) < 0).sort((a, b) => a - b);
		assert.deepStrictEqual(seen[k].slice().sort((a, b) => a - b), expected, k + ' coverage');
		// the numbers a save copies from the file instead of editing
		assert.deepStrictEqual(servicesUnmodelled(k), rfu[k], k + ' unmodelled slots');
	}
});

test('SERVICES_CROSS_REF has the doc\'s totals and no empty row', () => {
	const counts = { sim: 0, usim: 0, isim: 0, shared: 0 };
	for (const f of SERVICES_CROSS_REF) {
		assert.ok(typeof f.f === 'string' && f.f.length > 0, 'feature name');
		assert.ok(Object.prototype.hasOwnProperty.call(counts, f.cls), 'class: ' + f.cls);
		counts[f.cls]++;
		const nums = ['sst', 'ust', 'est', 'ist'].filter(k => f[k] !== null && f[k] !== undefined);
		assert.ok(nums.length > 0, 'row without a service number: ' + f.f);
		// the class says which applications carry the feature
		if (f.cls === 'sim') assert.deepStrictEqual(nums, ['sst'], f.f);
		else if (f.cls === 'isim') assert.deepStrictEqual(nums, ['ist'], f.f);
		else if (f.cls === 'usim') assert.ok(nums.indexOf('ust') >= 0 && nums.indexOf('sst') < 0 && nums.indexOf('ist') < 0, f.f);
		else assert.ok(nums.length > 1, 'shared row with one file: ' + f.f);
	}
	// docs/services-editor.md: 164 table rows = 161 features + 3 RFU rows
	// (10 SIM-only, 85 USIM-only, 5 ISIM-only, 61 shared).
	assert.deepStrictEqual(counts, { sim: 10, usim: 85, isim: 5, shared: 61 });
	assert.strictEqual(SERVICES_CROSS_REF.length, 161);
});

test('the four files are addressed by their canonical (DF, FID) path', () => {
	assert.deepStrictEqual(SERVICES_FILES.map(f => f.key), ['sst', 'ust', 'est', 'ist']);
	const paths = {};
	for (const f of SERVICES_FILES) paths[f.key] = f.path;
	assert.deepStrictEqual(paths, {
		sst: 'DF.GSM/6F38', ust: 'ADF.USIM/6F38', est: 'ADF.USIM/6F56', ist: 'ADF.ISIM/6F07',
	});
});

// ---- the edit-state helper ----

const dec = obj => obj;
function sstDec(pairs) {
	// pairs: { <n>: [allocated, activated] } - a deleted service is absent
	const services = [];
	for (const k of Object.keys(pairs)) {
		const n = Number(k);
		if (!pairs[k][0] && !pairs[k][1]) continue;
		services.push({ n, name: '', allocated: !!pairs[k][0], activated: !!pairs[k][1] });
	}
	return { services };
}
function bitDec(nums) {
	return { services: nums.map(n => ({ n, name: '' })) };
}

test('servicesFeatureState: shared feature on/off/deleted', () => {
	const adn = SERVICES_CROSS_REF.find(f => f.f === 'Local Phone Book (ADN)');
	assert.strictEqual(adn.sst, 2);
	assert.strictEqual(adn.ust, 1);
	// allocated + activated, UST bit set
	let st = servicesFeatureState(adn, sstDec({ 2: [1, 1] }), bitDec([1, 3]), null, null);
	assert.deepStrictEqual(st, { sst: 'on', ust: true, est: null, ist: null });
	// allocated but not activated -> 'off' (the editor never de-allocates)
	st = servicesFeatureState(adn, sstDec({ 2: [1, 0] }), bitDec([]), null, null);
	assert.deepStrictEqual(st, { sst: 'off', ust: false, est: null, ist: null });
	// deleted on the SIM (the decoder drops the service entirely) -> 'off'
	st = servicesFeatureState(adn, sstDec({ 3: [1, 1] }), bitDec([]), null, null);
	assert.strictEqual(st.sst, 'off');
});

test('servicesFeatureState: sim-only, usim-only, isim-only and EST', () => {
	const chv1 = SERVICES_CROSS_REF.find(f => f.f === 'CHV1 disable function');
	assert.strictEqual(chv1.cls, 'sim');
	assert.deepStrictEqual(servicesFeatureState(chv1, sstDec({ 1: [1, 1] }), null, null, null),
		{ sst: 'on', ust: null, est: null, ist: null });

	const oci = SERVICES_CROSS_REF.find(f => f.f === 'Outgoing Call Information (OCI and OCT)');
	assert.deepStrictEqual(servicesFeatureState(oci, null, bitDec([8]), null, null),
		{ sst: null, ust: true, est: null, ist: null });

	const pcscf = SERVICES_CROSS_REF.find(f => f.f === 'P-CSCF address');
	assert.deepStrictEqual(servicesFeatureState(pcscf, null, null, null, bitDec([1])),
		{ sst: null, ust: null, est: null, ist: true });

	const fdn = SERVICES_CROSS_REF.find(f => f.f === 'Fixed Dialling Numbers (FDN)');
	assert.strictEqual(fdn.est, 1);
	assert.deepStrictEqual(servicesFeatureState(fdn, sstDec({ 3: [1, 1] }), bitDec([2]), bitDec([]), null),
		{ sst: 'on', ust: true, est: false, ist: null });

	// a feature without a number in a file is null even when the file is there
	const ccp2 = SERVICES_CROSS_REF.find(f => f.f === 'Capability Configuration Parameters 2 (CCP2)');
	assert.deepStrictEqual(servicesFeatureState(ccp2, sstDec({ 1: [1, 1] }), bitDec([14]), null, null).sst, null);
	// an absent file leaves its column null
	const adn = SERVICES_CROSS_REF.find(f => f.f === 'Local Phone Book (ADN)');
	assert.strictEqual(servicesFeatureState(adn, null, bitDec([1]), null, null).sst, null);
});

// ---- render ----

test('the grid renders a column per present file and the right cell states', () => {
	const adn = SERVICES_CROSS_REF.findIndex(f => f.f === 'Local Phone Book (ADN)');
	const files = {
		sst: { present: true, hex: '00300000', dec: sstDec({ 2: [1, 1] }) },
		ust: { present: true, hex: '02000000', dec: bitDec([1]) },
		est: { present: false },
		ist: { present: false },
	};
	const out = pysimServicesGridHtml(files, {});
	assert.ok(out.includes('>SST<'), out);
	assert.ok(out.includes('>UST<'), out);
	assert.ok(!out.includes('>EST<'), out);
	assert.ok(!out.includes('>IST<'), out);
	// the ADN row: SST select on 'on', UST checkbox checked
	assert.ok(out.includes('data-svc="sst" data-row="' + adn + '"'));
	assert.match(out, new RegExp('<option value="on" selected>[^<]*</option>'));
	const adnRow = out.split('<tr').find(r => r.includes('Local Phone Book (ADN)'));
	assert.ok(adnRow.includes('checked'), adnRow);
	assert.ok(adnRow.includes('<select data-svc="sst"'), adnRow);
	// an ISIM-only feature is hidden while EF.IST is absent
	assert.ok(!out.includes('P-CSCF address'), out);
	// and a UST-only feature is hidden while EF.UST is absent
	const sstOnly = pysimServicesGridHtml({ sst: files.sst, ust: { present: false } }, {});
	assert.ok(sstOnly.includes('>SST<'), sstOnly);
	assert.ok(!sstOnly.includes('>UST<'), sstOnly);
	assert.ok(!sstOnly.includes('Outgoing Call Information'), sstOnly);
	assert.ok(sstOnly.includes('Local Phone Book (ADN)'), sstOnly);
	// a feature without a number in a present file shows an empty cell, no input
	const plmnRow = out.split('<tr').find(r => r.includes('>PLMN selector<'));
	assert.ok(plmnRow, 'the PLMN selector row renders');
	assert.ok(plmnRow.includes('data-svc="sst"'), plmnRow);
	assert.ok(!plmnRow.includes('data-svc="ust"'), plmnRow);
	assert.ok(plmnRow.includes('\u2014'), plmnRow);
	// an EST column renders a checkbox per EST-numbered feature (FDN = 1)
	const withEst = pysimServicesGridHtml({ ust: files.ust, est: { present: true, dec: bitDec([]) } }, {});
	assert.ok(withEst.includes('>EST<'), withEst);
	const fdnRow = withEst.split('<tr').find(r => r.includes('>Fixed Dialling Numbers (FDN)<'));
	assert.ok(fdnRow.includes('data-svc="est"'), fdnRow);
	assert.ok(!fdnRow.includes('data-svc="est" data-row="2" checked'), fdnRow);
	// ... and is checked from the card's decode
	const estOn = pysimServicesGridHtml({ ust: files.ust, est: { present: true, dec: bitDec([1]) } }, {});
	const fdnOn = estOn.split('<tr').find(r => r.includes('>Fixed Dialling Numbers (FDN)<'));
	assert.match(fdnOn, /data-svc="est" data-row="\d+" checked/);
});

test('no present file reports why, and the save row lists only present files', () => {
	const out = pysimServicesGridHtml({}, {});
	assert.ok(out.includes('No service table found on this card'), out);
	const row = pysimServicesSaveRowHtml({
		sst: { present: true }, ust: { present: true }, est: { present: false }, ist: { present: false },
	});
	assert.ok(row.includes('pysimServicesSave(\'sst\')'), row);
	assert.ok(row.includes('pysimServicesSave(\'ust\')'), row);
	assert.ok(!row.includes('pysimServicesSave(\'est\')'), row);
	assert.ok(!row.includes('pysimServicesSave(\'ist\')'), row);
	assert.ok(row.includes('Save to EF.SST'), row);
	assert.ok(row.includes('Save to EF.UST'), row);
	// the per-button status spans the writes report into
	assert.ok(row.includes('id="pysim-services-save-status-sst"'), row);
});

test('pysimServicesCollect reads the form controls into per-row state', () => {
	const grid = {
		querySelectorAll: () => [
			{ getAttribute: k => ({ 'data-svc': 'sst', 'data-row': '3' })[k], type: 'select-one', value: 'off' },
			{ getAttribute: k => ({ 'data-svc': 'ust', 'data-row': '3' })[k], type: 'checkbox', checked: true },
			{ getAttribute: k => ({ 'data-svc': 'ust', 'data-row': '4' })[k], type: 'checkbox', checked: false },
		],
	};
	globalThis.document = { getElementById: id => (id === 'pysim-services-grid' ? grid : null) };
	assert.deepStrictEqual(pysimServicesCollect(), { 3: { sst: 'off', ust: true }, 4: { ust: false } });
	globalThis.document = { getElementById: () => null };
	assert.deepStrictEqual(pysimServicesCollect(), {});
});

// ---- save ----

test('SST bytes: on allocates and activates, off never de-allocates', () => {
	// service 3 (FDN) allocated but not activated -> 0x10 in byte 0
	const files = { sst: { present: true, hex: '10000000' } };
	const fdn = SERVICES_CROSS_REF.findIndex(f => f.f === 'Fixed Dialling Numbers (FDN)');
	assert.strictEqual(efBytes('10000000')[0] & 0x30, 0x10);   // sanity: pair 1
	// "off" on an allocated-off service keeps it allocated (pair 1, not 0)
	let out = servicesFileBytes('sst', { [fdn]: { sst: 'off' } }, files);
	assert.strictEqual(out, '10000000');
	// "on" allocates and activates (pair 3)
	out = servicesFileBytes('sst', { [fdn]: { sst: 'on' } }, files);
	assert.strictEqual(out, '30000000');
	// switching an allocated+activated service to "off" de-activates but keeps
	// the allocation (pair 1)
	out = servicesFileBytes('sst', { [fdn]: { sst: 'off' } }, { sst: { present: true, hex: '30000000' } });
	assert.strictEqual(out, '10000000');
	// a deleted service stays deleted when the form renders it as "off"
	out = servicesFileBytes('sst', { [fdn]: { sst: 'off' } }, { sst: { present: true, hex: '00000000' } });
	assert.strictEqual(out, '00000000');
	// ... and is allocated by an explicit "on"
	out = servicesFileBytes('sst', { [fdn]: { sst: 'on' } }, { sst: { present: true, hex: '00000000' } });
	assert.strictEqual(out, '30000000');
});

test('SST bytes preserve the unmodelled RFU slots and the file length', () => {
	// service 8 = RFU, in byte 1 (services 5-8), pair 4 -> bits 6+7 = 0xC0
	const files = { sst: { present: true, hex: '00C000' } };
	assert.deepStrictEqual(servicesUnmodelled('sst'), [8, 20, 50]);
	assert.strictEqual(servicesFileBytes('sst', {}, files), '00C000',
		'the RFU pair and the trailing byte survive');
});

test('UST/EST/IST bytes come from efEncServiceTable over the original bytes', () => {
	// UST service 1 (ADN) set in byte 0; service 26 (RFU) in byte 3 bit 1
	const files = { ust: { present: true, hex: '01000002' } };
	const adn = SERVICES_CROSS_REF.findIndex(f => f.f === 'Local Phone Book (ADN)');
	const sms = SERVICES_CROSS_REF.findIndex(f => f.f === 'Short Message Storage (SMS)');
	let out = servicesFileBytes('ust', { [adn]: { ust: true }, [sms]: { ust: true } }, files);
	// service 10 (SMS) -> byte 1 bit 1 = 0x02
	assert.strictEqual(out, '01020002');
	out = servicesFileBytes('ust', { [adn]: { ust: false }, [sms]: { ust: false } }, files);
	assert.strictEqual(out, '00000002', 'the RFU bit is preserved, the modelled bits cleared');
	// EST has its own numbering: FDN = 1
	const estFiles = { est: { present: true, hex: '00' } };
	const fdn = SERVICES_CROSS_REF.findIndex(f => f.f === 'Fixed Dialling Numbers (FDN)');
	assert.strictEqual(servicesFileBytes('est', { [fdn]: { est: true } }, estFiles), '01');
	assert.strictEqual(servicesFileBytes('est', { [fdn]: { est: false } }, estFiles), '00');
	// ISIM IST: P-CSCF address = 1
	const istFiles = { ist: { present: true, hex: '00' } };
	const pcscf = SERVICES_CROSS_REF.findIndex(f => f.f === 'P-CSCF address');
	assert.strictEqual(servicesFileBytes('ist', { [pcscf]: { ist: true } }, istFiles), '01');
});

test('an absent file yields no bytes', () => {
	assert.strictEqual(servicesFileBytes('sst', {}, { sst: { present: false } }), null);
	assert.strictEqual(servicesFileBytes('ust', {}, {}), null);
});

// ---- filesystem check: the feature -> FID table (docs/services-editor.md §B) ----

const UICC_FILES = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'uicc_files.json'), 'utf8'));

test('SERVICES_FS_MAP covers every feature exactly once and carries 4-hex FIDs', () => {
	const names = SERVICES_CROSS_REF.map(f => f.f);
	assert.deepStrictEqual(Object.keys(SERVICES_FS_MAP), names,
		'one entry per cross-reference row, in table order');
	let backed = 0, capability = 0;
	for (const [name, fids] of Object.entries(SERVICES_FS_MAP)) {
		assert.ok(Array.isArray(fids), 'not an array: ' + name);
		assert.strictEqual(new Set(fids).size, fids.length, 'duplicate FID: ' + name);
		for (const fid of fids) assert.match(fid, /^[0-9A-F]{4}$/, name + ': ' + fid);
		if (fids.length) backed++; else capability++;
	}
	// docs/services-editor.md §B.1 lists 34 `capability` cells; the remaining
	// 127 rows declare at least one file.
	assert.strictEqual(capability, 34);
	assert.strictEqual(backed, 127);
	assert.strictEqual(SERVICES_CROSS_REF.length, backed + capability);
});

test('the capability features carry no file check', () => {
	for (const name of [
		'CHV1 disable function', 'Data download via SMS-CB', 'Data download via SMS-PP',
		'Menu selection', 'Call control (by SIM/USIM)', 'Proactive SIM',
		'MO-SMS control (by SIM/USIM)', 'RUN AT COMMAND command', 'Call control on GPRS by USIM',
		'Additional TERMINAL PROFILE after UICC activation', 'Terminal Applications',
		'URI support by UICC', 'HTTP Digest', 'Support of P-CSCF discovery for IMS Local Break Out',
	]) {
		assert.deepStrictEqual(SERVICES_FS_MAP[name], [], name);
	}
});

test('the well-known files map to their documented FIDs', () => {
	assert.deepStrictEqual(SERVICES_FS_MAP['Fixed Dialling Numbers (FDN)'], ['6F3B', '6F4B']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Service Provider Name (SPN)'], ['6F46']);
	assert.deepStrictEqual(SERVICES_FS_MAP['GSM Access'], ['5F3B']);
	// ADN: the EF plus the USIM's DF.PHONEBOOK and EF.PBR
	assert.ok(SERVICES_FS_MAP['Local Phone Book (ADN)'].includes('6F3A'));
	assert.ok(SERVICES_FS_MAP['Short Message Storage (SMS)'].includes('6F3C'));
	assert.deepStrictEqual(SERVICES_FS_MAP['Image (IMG)'], ['5F50', '4F20']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Advice of Charge (AoC)'], ['6F39', '6F37', '6F41']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Extension 4'], ['6F4E', '6F55'],
		'both trees\' FIDs, per the doc\'s "(SIM) / 6F55 (USIM)"');
});

test('every mapped FID is a known file (pySim\'s cardless list or the documented gap)', () => {
	const known = new Set(UICC_FILES.files.map(e => e.fid));
	const mapped = new Set();
	for (const fids of Object.values(SERVICES_FS_MAP)) for (const fid of fids) mapped.add(fid);
	// docs/services-editor.md §5: files UICC_FILES.md documents but pySim's
	// model does not carry (the FS check probes the card, so they are not a
	// blocker there).  6FDC/6FF0/6FF2 are also outside pySim's profile.
	const gap = new Set(['4F31', '4F40', '5F3C', '5F3F', '5F70', '5F80', '5FA0', '5FB0',
		'6FDC', '6FF0', '6FF2', '6FF9', '6FFB']);
	assert.deepStrictEqual(Array.from(mapped).filter(fid => !known.has(fid)).sort(),
		Array.from(gap).sort(), 'mapped FIDs outside the cardless list');
	assert.ok(mapped.size >= 150, 'distinct mapped FIDs: ' + mapped.size);
});

// ---- filesystem check: the report ----

const svcFiles = (sst, ust, est, ist) => ({
	sst: sst ? { present: true, dec: sst } : { present: false },
	ust: ust ? { present: true, dec: ust } : { present: false },
	est: est ? { present: true, dec: est } : { present: false },
	ist: ist ? { present: true, dec: ist } : { present: false },
});

test('servicesFsPresentFids collects the probed nodes\' FIDs, recursively', () => {
	const root = {
		children: [
			{ name: 'MF/7F20', fid: '7f20', isDir: true, exists: true, children: [
				{ name: 'EF.SPN', fid: '6f46', exists: true },
				{ name: 'EF.LND', fid: '6F44', exists: false },
				{ name: 'EF.ADN', fid: '6F3A', exists: true },
			] },
			{ name: 'EF.ICCID', fid: '2FE2', exists: null },      // never probed
			{ name: 'DF.GSM-ACCESS', fid: '5F3B', isDir: false, exists: true },
			{ name: 'no fid', exists: true },
			{ name: 'MF/2F06', fid: '2F06', isDir: true, exists: false, children: [
				{ name: 'EF.SST', fid: '6F38', exists: true },   // under an absent DF
			] },
		],
	};
	assert.deepStrictEqual(servicesFsPresentFids(root), ['7F20', '6F46', '6F3A', '5F3B']);
	assert.deepStrictEqual(servicesFsPresentFids(null), []);
	// the same walk supplies the report's file names, one list per collided FID
	assert.deepStrictEqual(servicesFsFidNames(root), {
		'7F20': ['MF/7F20'], '6F46': ['EF.SPN'], '6F44': ['EF.LND'], '6F3A': ['EF.ADN'],
		'2FE2': ['EF.ICCID'], '5F3B': ['DF.GSM-ACCESS'], '2F06': ['MF/2F06'],
	});
	assert.deepStrictEqual(servicesFsFidNames(null), {});
});

test('servicesFsDeclared reads the union of the present service files', () => {
	const files = svcFiles(
		sstDec({ 3: [1, 1], 4: [1, 0] }),   // FDN on, SMS allocated-off
		bitDec([2, 10]),                    // UST FDN + SMS
		null,                               // no EF.EST on the card
		bitDec([6]));                       // IST SMS
	const declared = servicesFsDeclared(files);
	assert.ok(declared.includes('Fixed Dialling Numbers (FDN)'), declared.join(' | '));
	assert.ok(declared.includes('Short Message Storage (SMS)'), declared.join(' | '));
	// an allocated-off SST service declares nothing on its own
	const off = servicesFsDeclared(svcFiles(sstDec({ 4: [1, 0] }), null, null, null));
	assert.deepStrictEqual(off, []);
	// no files at all: nothing declared
	assert.deepStrictEqual(servicesFsDeclared(svcFiles(null, null, null, null)), []);
});

test('servicesFsReport: a declared feature with an absent file', () => {
	const rep = servicesFsReport(['Fixed Dialling Numbers (FDN)'], ['6F3B']);
	assert.deepStrictEqual(rep.missing, [{ feature: 'Fixed Dialling Numbers (FDN)', fids: ['6F4B'] }]);
	assert.deepStrictEqual(rep.extra, []);
	// both files gone
	assert.deepStrictEqual(servicesFsReport(['Fixed Dialling Numbers (FDN)'], []).missing,
		[{ feature: 'Fixed Dialling Numbers (FDN)', fids: ['6F3B', '6F4B'] }]);
});

test('servicesFsReport: a present file whose service is undeclared', () => {
	const rep = servicesFsReport([], ['6F3C']);
	assert.deepStrictEqual(rep.missing, []);
	assert.deepStrictEqual(rep.extra, [{ fid: '6F3C', features: ['Short Message Storage (SMS)'] }]);
	// a FID several features map lists all of them, in table order
	assert.deepStrictEqual(servicesFsReport([], ['4F02']).extra,
		[{ fid: '4F02', features: ['V2X', '5GS Mobility Management Information', 'Network Identifier for SNPN (NID)'] }]);
});

test('servicesFsReport: an empty pair reports no mismatch', () => {
	assert.deepStrictEqual(servicesFsReport([], []), { missing: [], extra: [] });
	// a declared feature whose files are all present is not a mismatch either
	assert.deepStrictEqual(servicesFsReport(['Service Provider Name (SPN)'], ['6F46']),
		{ missing: [], extra: [] });
});

test('servicesFsReport: a capability feature is never flagged', () => {
	const rep = servicesFsReport(['CHV1 disable function', 'Proactive SIM', 'Menu selection'], []);
	assert.deepStrictEqual(rep, { missing: [], extra: [] });
});

test('servicesFsReport: a shared feature flags on its own mapped FIDs', () => {
	// SMS is declared and present; ADN is declared, nothing of it is on the card
	const rep = servicesFsReport(['Local Phone Book (ADN)', 'Short Message Storage (SMS)'], ['6F3C']);
	assert.deepStrictEqual(rep.missing, [{ feature: 'Local Phone Book (ADN)', fids: ['6F3A', '5F3A', '4F30'] }]);
	assert.deepStrictEqual(rep.extra, [], 'the present SMS file is explained by its declared service');
	// a present FID any declared feature maps is explained by it
	assert.deepStrictEqual(servicesFsReport(['MCPTT'], ['4F01']),
		{ missing: [{ feature: 'MCPTT', fids: ['5F3D'] }], extra: [] });
	// 4F01 present while nothing that maps it is declared: one extra entry
	assert.deepStrictEqual(servicesFsReport([], ['4F01']).extra[0].fid, '4F01');
	assert.ok(servicesFsReport([], ['4F01']).extra[0].features.includes('ACDC support'));
});

test('servicesFsReport is deterministic and order-stable', () => {
	const declared = ['Service Provider Name (SPN)', 'Fixed Dialling Numbers (FDN)'];
	const present = ['6f3b', '6F46'];
	const a = servicesFsReport(declared, present);
	const b = servicesFsReport(declared.slice().reverse(), present);
	assert.deepStrictEqual(a, b, 'cross-reference order, not input order');
	assert.deepStrictEqual(a.missing, [{ feature: 'Fixed Dialling Numbers (FDN)', fids: ['6F4B'] }]);
	// the lower-case present FIDs matched
	assert.deepStrictEqual(a.extra, []);
	// extra is sorted by FID
	const extra = servicesFsReport(['Service Provider Name (SPN)'], ['6F81', '6F3C', '6F45']).extra;
	assert.deepStrictEqual(extra.map(e => e.fid), ['6F3C', '6F45', '6F81']);
});

test('servicesFsReportHtml renders the two headings, the match line and the note', () => {
	const note = 'FID-level';
	const empty = servicesFsReportHtml(servicesFsReport([], []));
	assert.ok(empty.includes('The filesystem matches the declared services'), empty);
	assert.ok(empty.includes(note), empty);
	const rep = servicesFsReport(['Fixed Dialling Numbers (FDN)'], ['6F3C']);
	const html = servicesFsReportHtml(rep);
	assert.ok(html.includes('Declared services whose files are missing'), html);
	assert.ok(html.includes('Fixed Dialling Numbers (FDN)'), html);
	assert.ok(html.includes('6F4B'), html);
	assert.ok(html.includes('Present files whose service is not declared'), html);
	assert.ok(html.includes('6F3C'), html);
	assert.ok(html.includes('Short Message Storage (SMS)'), html);
	assert.ok(html.includes(note), html);
	assert.ok(!html.includes('The filesystem matches the declared services'), html);
	// a single-sided report renders only its own heading
	const one = servicesFsReportHtml(servicesFsReport(['Fixed Dialling Numbers (FDN)'], ['6F3B']));
	assert.ok(one.includes('Declared services whose files are missing'), one);
	assert.ok(!one.includes('Present files whose service is not declared'), one);
	// the tree's names annotate the FIDs when the check passes its names map
	const named = servicesFsReportHtml(rep, { '6F3C': ['EF.SMS'], '6F4B': ['EF.EXT2'] });
	assert.ok(named.includes('6F4B') && named.includes('(EF.EXT2)'), named);
	assert.ok(named.includes('6F3C') && named.includes('(EF.SMS)'), named);
	// ... and a FID the map lacks stays bare
	assert.ok(!named.includes('6F3A ('), named);
});

// ---- filesystem check: the wiring ----

test('the Services pill carries the Check filesystem action and its report panel', () => {
	const start = html.indexOf('id="pysim-sub-services"');
	const pill = html.slice(start, html.indexOf('id="tab-phone"', start));
	assert.match(pill, /id="pysim-services-fs-btn"[^>]*data-needs="card"[^>]*onclick="pysimServicesFsCheck\(\)"/);
	assert.match(pill, /data-l10n="Check filesystem"/);
	assert.match(pill, /id="pysim-services-fs-status"/);
	assert.match(pill, /id="pysim-services-fs-report"/);
});

test('the check probes the tree once per loaded session and keeps the report across renders', () => {
	// /api/tree lists files without testing them: only a completed probe sets
	// the presence flag, and a refresh (card change) clears it
	assert.match(html, /_pysimFsProbed = !probe\.stop && !error;/);
	assert.match(html, /async function pysimFsRefresh\(\) \{[\s\S]{0,200}_pysimFsProbed = false;/);
	// the check loads the tree if missing, probes when unprobed, and renders
	// the pure report
	const fn = extractFunc(html, 'pysimServicesFsCheck');
	assert.ok(fn.indexOf('await pysimFsRefresh()') > 0, fn);
	assert.ok(fn.indexOf('await pysimFsProbeAll()') > 0, fn);
	assert.ok(fn.indexOf('servicesFsReport(') > 0, fn);
	// the grid's render redraws the kept report (language switch)
	const render = extractFunc(html, 'pysimServicesRender');
	assert.ok(render.indexOf('pysimServicesFsRender()') > 0, render);
	// and a card-session change drops it with the cache
	assert.match(html, /function pysimServicesReset\(\) \{\s*_pysimServicesCache = null;\s*_pysimServicesFsReport = null;/);
});
