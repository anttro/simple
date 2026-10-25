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
	'servicesFsDeclaredTrees', 'servicesFsPresentPaths', 'servicesFsReport',
	'servicesFsReportHtml', 'servicesFsPathKey', 'servicesFsNormPath', 'servicesFsRolePath',
	'servicesPathIndexBuild', 'pysimFsNodePath',
];

let code = 'var _pysimServicesCache = null;\nvar _pysimServicesBusy = false;\n';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_EST_SERVICES',
	'SERVICES_CROSS_REF', 'SERVICES_FILES', 'SERVICES_FS_MAP', 'SERVICES_FS_TREES',
	'SERVICES_FS_TREE_ORDER', 'SERVICES_FS_GSM_MAPPED', 'SERVICES_TABLES']) {
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

// ---- filesystem check: the feature -> symbolic files table (docs/services-editor.md §B) ----

const UICC_FILES = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'uicc_files.json'), 'utf8'));
const FS_INDEX = servicesPathIndexBuild(UICC_FILES.files);
const fsResolve = (role, root) => FS_INDEX.get(servicesFsPathKey(role, root));

test('SERVICES_FS_MAP covers every feature exactly once and carries symbolic file names', () => {
	const names = SERVICES_CROSS_REF.map(f => f.f);
	assert.deepStrictEqual(Object.keys(SERVICES_FS_MAP), names,
		'one entry per cross-reference row, in table order');
	let backed = 0, capability = 0;
	for (const [name, roles] of Object.entries(SERVICES_FS_MAP)) {
		assert.ok(Array.isArray(roles), 'not an array: ' + name);
		assert.strictEqual(new Set(roles).size, roles.length, 'duplicate role: ' + name);
		// the standard file list's own names, never FIDs: a FID is ambiguous
		// across applications (4F30 = EF.PBR and EF.SAI)
		for (const role of roles) assert.match(role, /^(EF|DF)\.[A-Za-z0-9_+.-]+$/, name + ': ' + role);
		if (roles.length) backed++; else capability++;
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

test('the well-known features map to their documented files', () => {
	assert.deepStrictEqual(SERVICES_FS_MAP['Fixed Dialling Numbers (FDN)'], ['EF.FDN', 'EF.EXT2']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Service Provider Name (SPN)'], ['EF.SPN']);
	assert.deepStrictEqual(SERVICES_FS_MAP['GSM Access'], ['DF.GSM-ACCESS']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Local Phone Book (ADN)'], ['EF.ADN', 'DF.PHONEBOOK', 'EF.PBR']);
	assert.deepStrictEqual(SERVICES_FS_MAP['SoLSA'], ['DF.SoLSA', 'EF.SAI', 'EF.SLL']);
	assert.deepStrictEqual(SERVICES_FS_MAP['MExE'], ['DF.MExE', 'EF.MExE-ST']);
	assert.deepStrictEqual(SERVICES_FS_MAP['A2X'], ['DF.A2X', 'EF.AST', 'EF.A2X_CONFIG']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Advice of Charge (AoC)'], ['EF.ACM', 'EF.ACMmax', 'EF.PUCT']);
	assert.deepStrictEqual(SERVICES_FS_MAP['Extension 4'], ['EF.EXT4'],
		'one name: the SIM and the USIM copy have different FIDs but the same name');
});

test('a role outside its feature\'s declaring tree lives in the shared DF.TELECOM subtree', () => {
	// A UST/IST-gated DF may be a child of DF.TELECOM (TS 31.102 4.6.0), i.e.
	// an MF path - the resolver's fallback.  Anything else would be a mapping
	// error, and the GSM application's mapped copies (EF.ADN/EF.EXT1/EF.ECCP)
	// are deliberately not expected there.
	const features = new Set();
	for (const f of SERVICES_CROSS_REF) {
		const trees = [];
		if (f.sst !== null) trees.push('MF');
		if (f.ust !== null || f.est !== null) trees.push('ADF.USIM');
		if (f.ist !== null) trees.push('ADF.ISIM');
		for (const role of (SERVICES_FS_MAP[f.f] || [])) {
			if (trees.some(root => fsResolve(role, root))) continue;
			const path = fsResolve(role, 'MF');
			if (!path) continue;                     // a registry gap, pinned above
			assert.match(path, /^MF\/7F10\//, f.f + ': ' + role + ' -> ' + path);
			assert.ok(SERVICES_FS_GSM_MAPPED.indexOf(role) < 0, f.f + ': ' + role);
			features.add(f.f);
		}
	}
	assert.deepStrictEqual(Array.from(features),
		['Multimedia Messages Storage', 'MCPTT', 'V2X', 'A2X']);
});

test('every mapped role is a standard file, except the documented registry gaps', () => {	// The names that resolve in none of the checked trees: files the standard
	// list does not carry yet (pySim omits DF.GRAPHICS and the DF.TELECOM
	// copies of DF.SoLSA/DF.MExE; EF.3GPPPSDATAOFFservicelist is a pySim TODO).
	// The report lists these as `unchecked` instead of guessing a path.
	const gaps = {
		'Image (IMG)': ['DF.GRAPHICS', 'EF.IMG'],
		'3GPP PS Data Off Service List': ['EF.3GPPPSDATAOFFservicelist'],
	};
	const unresolved = {};
	for (const [name, roles] of Object.entries(SERVICES_FS_MAP)) {
		for (const role of roles) {
			if (SERVICES_FS_TREE_ORDER.some(root => fsResolve(role, root))) continue;
			(unresolved[name] = unresolved[name] || []).push(role);
		}
	}
	assert.deepStrictEqual(unresolved, gaps);
	// a mapped role that does resolve resolves through exactly one list entry
	for (const roles of Object.values(SERVICES_FS_MAP)) {
		for (const role of roles) {
			for (const root of SERVICES_FS_TREE_ORDER) {
				const path = fsResolve(role, root);
				if (!path) continue;
				const entries = UICC_FILES.files.filter(f => f.name === role
					&& f.root.toUpperCase() === root && f.path.toUpperCase() === path);
				assert.strictEqual(entries.length, 1, role + ' ' + root + ' -> ' + path);
			}
		}
	}
});

// ---- filesystem check: the (name, root) -> path index ----

test('servicesPathIndexBuild keys the standard list by (name, root)', () => {
	assert.ok(FS_INDEX.size >= 200, 'indexed files: ' + FS_INDEX.size);
	// every value is one of the list's paths, and the list's roots are the keys
	for (const [key, path] of FS_INDEX) {
		const [name, root] = key.split('|');
		assert.ok(UICC_FILES.files.some(f => f.name.toUpperCase() === name && f.root.toUpperCase() === root
			&& f.path.toUpperCase() === path), key + ' -> ' + path);
	}
	assert.deepStrictEqual(servicesPathIndexBuild(null), new Map());
	assert.deepStrictEqual(servicesPathIndexBuild([null, {}, { name: 'EF.X' }]), new Map());
	// case-insensitive on both sides
	assert.strictEqual(FS_INDEX.get(servicesFsPathKey('ef.spn', 'mf')), 'MF/7F20/6F46');
});

test('the same FID under different names resolves to its own path', () => {
	// EF.PBR and EF.SAI are both 4F30
	assert.strictEqual(fsResolve('EF.PBR', 'ADF.USIM'), 'ADF.USIM/5F3A/4F30');
	assert.strictEqual(fsResolve('EF.SAI', 'ADF.USIM'), 'ADF.USIM/5F70/4F30');
	// EF.PBR exists in both trees, under different parents
	assert.strictEqual(fsResolve('EF.PBR', 'MF'), 'MF/7F10/5F3A/4F30');
	// a DF.TELECOM child resolves in the MF tree
	assert.strictEqual(fsResolve('DF.A2X', 'MF'), 'MF/7F10/5F3F');
	assert.strictEqual(fsResolve('EF.A2X_CONFIG', 'MF'), 'MF/7F10/5F3F/4F02');
	// EF.SST and EF.UST are both 6F38
	assert.strictEqual(fsResolve('EF.UST', 'ADF.USIM'), 'ADF.USIM/6F38');
	assert.strictEqual(fsResolve('EF.IST', 'ADF.ISIM'), 'ADF.ISIM/6F07');
	// the one duplicate (name, root): pySim models EF.SUCI_Calc_Info twice
	// under ADF.USIM (DF.5GS/4F07 - the UST 125 file - and the eSIM profile
	// package's DF.SAIP/4F01); the list's first entry, DF.5GS, wins
	const suci = UICC_FILES.files.filter(f => f.name === 'EF.SUCI_Calc_Info' && f.root === 'ADF.USIM');
	assert.strictEqual(suci.length, 2);
	assert.strictEqual(fsResolve('EF.SUCI_Calc_Info', 'ADF.USIM'), 'ADF.USIM/5FC0/4F07');
});

test('servicesFsRolePath falls back to the shared DF.TELECOM subtree', () => {
	// DF.MCS is a child of DF.TELECOM (TS 31.102 4.6.0): a UST declaration
	// resolves it in the MF tree
	assert.strictEqual(servicesFsRolePath(FS_INDEX, 'DF.MCS', 'ADF.USIM'), 'MF/7F10/5F3D');
	assert.strictEqual(servicesFsRolePath(FS_INDEX, 'EF.MST', 'ADF.USIM'), 'MF/7F10/5F3D/4F01');
	// an own-tree path wins over the fallback
	assert.strictEqual(servicesFsRolePath(FS_INDEX, 'EF.FDN', 'ADF.USIM'), 'ADF.USIM/6F3B');
	assert.strictEqual(servicesFsRolePath(FS_INDEX, 'EF.FDN', 'MF'), 'MF/7F10/6F3B');
	// the GSM application's mapped copies are not expected by a USIM
	// declaration (TS 31.102 4.5) ...
	assert.strictEqual(servicesFsRolePath(FS_INDEX, 'EF.ADN', 'ADF.USIM'), undefined);
	assert.ok(SERVICES_FS_GSM_MAPPED.indexOf('EF.ADN') >= 0);
	// ... and a SIM (MF) declaration never falls back to an application
	assert.strictEqual(servicesFsRolePath(FS_INDEX, 'DF.SoLSA', 'MF'), undefined);
	assert.strictEqual(servicesFsRolePath(null, 'EF.SPN', 'MF'), undefined);
});

// ---- filesystem check: the report ----

const svcFiles = (sst, ust, est, ist) => ({
	sst: sst ? { present: true, dec: sst } : { present: false },
	ust: ust ? { present: true, dec: ust } : { present: false },
	est: est ? { present: true, dec: est } : { present: false },
	ist: ist ? { present: true, dec: ist } : { present: false },
});

// A file-manager tree with parent links (pysimFsNodePath walks up them).
function fsTree(children) {
	const mk = (spec, parent) => {
		const node = {
			name: spec.name, fid: spec.fid, isDir: !!spec.isDir, exists: spec.exists,
			children: null, parent: parent,
		};
		if (spec.children) node.children = spec.children.map(c => mk(c, node));
		return node;
	};
	const root = { name: 'MF', path: 'MF', fid: '3F00', isDir: true, exists: true, parent: null, children: null };
	root.children = (children || []).map(c => mk(c, root));
	return root;
}

test('servicesFsPresentPaths collects the probed nodes\' canonical paths', () => {
	const root = fsTree([
		{ name: 'DF.TELECOM', fid: '7F10', isDir: true, exists: true, children: [
			{ name: 'EF.SMS', fid: '6f3c', exists: true },
			{ name: 'EF.LND', fid: '6F44', exists: false },
		] },
		{ name: 'EF.ICCID', fid: '2FE2', exists: null },          // never probed
		{ name: 'ADF.USIM', isDir: true, exists: true, children: [
			{ name: 'EF.UST', fid: '6F38', exists: true },
		] },
		{ name: 'DF.ABSENT', fid: '5F80', isDir: true, exists: false, children: [
			{ name: 'EF.BCAST', fid: '4F01', exists: true },       // under an absent DF
		] },
	]);
	assert.deepStrictEqual(servicesFsPresentPaths(root),
		['MF/7F10', 'MF/7F10/6F3C', 'ADF.USIM', 'ADF.USIM/6F38']);
	assert.deepStrictEqual(servicesFsPresentPaths(null), []);
});

test('servicesFsDeclaredTrees groups the declared features by declaring application', () => {
	const files = svcFiles(
		sstDec({ 3: [1, 1], 4: [1, 0] }),   // FDN on, SMS allocated-off
		bitDec([2, 10]),                    // UST FDN + SMS
		null,                               // no EF.EST on the card
		bitDec([6]));                       // IST SMS
	const trees = servicesFsDeclaredTrees(files);
	assert.deepStrictEqual(Object.keys(trees), ['MF', 'ADF.USIM', 'ADF.ISIM']);
	assert.ok(trees.MF.includes('Fixed Dialling Numbers (FDN)'), trees.MF.join(' | '));
	assert.ok(!trees.MF.includes('Short Message Storage (SMS)'), 'an allocated-off SST service declares nothing');
	assert.ok(trees['ADF.USIM'].includes('Fixed Dialling Numbers (FDN)'));
	assert.ok(trees['ADF.USIM'].includes('Short Message Storage (SMS)'));
	assert.ok(trees['ADF.ISIM'].includes('Short Message Storage (SMS)'));
	// the tree's own order: cross-reference order, deduplicated
	assert.deepStrictEqual(trees['ADF.USIM'], ['Fixed Dialling Numbers (FDN)', 'Short Message Storage (SMS)']);
	// a tree whose service table is absent is not checked at all
	assert.deepStrictEqual(servicesFsDeclaredTrees(svcFiles(null, bitDec([2]), null, null)),
		{ 'ADF.USIM': ['Fixed Dialling Numbers (FDN)'] });
	assert.deepStrictEqual(servicesFsDeclaredTrees(svcFiles(null, null, null, null)), {});
});

const fsReport = (declaredByTree, present) => servicesFsReport(declaredByTree, FS_INDEX, present);

test('servicesFsReport: a declared feature with an absent path', () => {
	const rep = fsReport({ MF: ['Fixed Dialling Numbers (FDN)'] }, ['MF/7F10/6F3B']);
	assert.deepStrictEqual(rep.missing,
		[{ feature: 'Fixed Dialling Numbers (FDN)', tree: 'MF', paths: ['MF/7F10/6F4B'] }]);
	assert.deepStrictEqual(rep.extra, []);
	assert.deepStrictEqual(rep.unchecked, []);
	// both files gone, in the map's (doc) role order
	assert.deepStrictEqual(fsReport({ MF: ['Fixed Dialling Numbers (FDN)'] }, []).missing,
		[{ feature: 'Fixed Dialling Numbers (FDN)', tree: 'MF', paths: ['MF/7F10/6F3B', 'MF/7F10/6F4B'] }]);
});

test('servicesFsReport: the same FID in another DF no longer explains a service', () => {
	// EF.PBR and EF.SAI are both 4F30: a card holding the phonebook copy but
	// no DF.SoLSA leaves SoLSA's paths missing
	const rep = fsReport({ 'ADF.USIM': ['SoLSA'] }, ['ADF.USIM/5F3A', 'ADF.USIM/5F3A/4F30']);
	assert.deepStrictEqual(rep.missing, [{ feature: 'SoLSA', tree: 'ADF.USIM',
		paths: ['ADF.USIM/5F70', 'ADF.USIM/5F70/4F30', 'ADF.USIM/5F70/4F31'] }]);
	// the phonebook paths are present but ADN is not declared
	assert.deepStrictEqual(rep.extra.map(e => e.path), ['ADF.USIM/5F3A', 'ADF.USIM/5F3A/4F30']);
	assert.deepStrictEqual(rep.extra[1],
		{ path: 'ADF.USIM/5F3A/4F30', role: 'EF.PBR', tree: 'ADF.USIM', features: ['Local Phone Book (ADN)'] });
	// with the SoLSA paths present the report is clean
	const ok = fsReport({ 'ADF.USIM': ['SoLSA', 'Local Phone Book (ADN)'] },
		['ADF.USIM/5F70', 'ADF.USIM/5F70/4F30', 'ADF.USIM/5F70/4F31', 'ADF.USIM/5F3A', 'ADF.USIM/5F3A/4F30']);
	assert.deepStrictEqual(ok, { missing: [], extra: [], unchecked: [] });
});

test('servicesFsReport: ADN\'s USIM files are the phonebook pair, not the GSM copy', () => {
	// TS 31.102 4.5: EF.ADN under DF.TELECOM is the GSM application's mapping
	const usim = fsReport({ 'ADF.USIM': ['Local Phone Book (ADN)'] }, ['ADF.USIM/5F3A', 'ADF.USIM/5F3A/4F30']);
	assert.deepStrictEqual(usim, { missing: [], extra: [], unchecked: [] });
	// ... so a USIM card without the mapped 6F3A is not missing anything
	assert.deepStrictEqual(fsReport({ 'ADF.USIM': ['Local Phone Book (ADN)'] }, ['ADF.USIM/5F3A/4F30']).missing,
		[{ feature: 'Local Phone Book (ADN)', tree: 'ADF.USIM', paths: ['ADF.USIM/5F3A'] }]);
	// the SIM declaration does expect it
	const sim = fsReport({ MF: ['Local Phone Book (ADN)'] }, ['MF/7F10/5F3A', 'MF/7F10/5F3A/4F30']);
	assert.deepStrictEqual(sim.missing,
		[{ feature: 'Local Phone Book (ADN)', tree: 'MF', paths: ['MF/7F10/6F3A'] }]);
	assert.deepStrictEqual(sim.extra, []);
});

test('servicesFsReport: a UST-declared DF.TELECOM file resolves in the MF tree', () => {
	// MCPTT's DF.MCS is a child of DF.TELECOM (TS 31.102 4.6.0)
	assert.deepStrictEqual(fsReport({ 'ADF.USIM': ['MCPTT'] }, ['MF/7F10/5F3D', 'MF/7F10/5F3D/4F01']),
		{ missing: [], extra: [], unchecked: [] });
	assert.deepStrictEqual(fsReport({ 'ADF.USIM': ['MCPTT'] }, []).missing,
		[{ feature: 'MCPTT', tree: 'ADF.USIM', paths: ['MF/7F10/5F3D', 'MF/7F10/5F3D/4F01'] }]);
});

test('servicesFsReport: a declared feature the standard list cannot resolve is unchecked', () => {
	const rep = fsReport({ 'ADF.USIM': ['Image (IMG)'] }, []);
	assert.deepStrictEqual(rep, { missing: [], extra: [], unchecked: [
		{ feature: 'Image (IMG)', tree: 'ADF.USIM', roles: ['DF.GRAPHICS', 'EF.IMG'] }] });
	// a capability row has no files and is never unchecked
	assert.deepStrictEqual(fsReport({ MF: ['CHV1 disable function'] }, []),
		{ missing: [], extra: [], unchecked: [] });
	// a tree the card does not have declares nothing there
	assert.deepStrictEqual(fsReport({}, []), { missing: [], extra: [], unchecked: [] });
});

test('servicesFsReport: a shared file is explained by any declared feature', () => {
	// DF.GSM-ACCESS is gated by both "GSM Access" and "GSM security context";
	// declaring either one explains the present path
	const present = ['ADF.USIM/5F3B', 'ADF.USIM/5F3B/4F20', 'ADF.USIM/5F3B/4F52'];
	assert.deepStrictEqual(fsReport({ 'ADF.USIM': ['GSM security context'] }, present),
		{ missing: [], extra: [], unchecked: [] });
	// with neither declared the path is an extra, naming every mapping feature
	assert.deepStrictEqual(fsReport({ 'ADF.USIM': [] }, ['ADF.USIM/5F3B']).extra,
		[{ path: 'ADF.USIM/5F3B', role: 'DF.GSM-ACCESS', tree: 'ADF.USIM',
			features: ['GSM Access', 'GSM security context'] }]);
	// a lower-case present path matches the list's upper-case one
	assert.deepStrictEqual(fsReport({ MF: ['Service Provider Name (SPN)'] }, ['mf/7f20/6f46']),
		{ missing: [], extra: [], unchecked: [] });
});

test('servicesFsReport: a present path whose service is undeclared', () => {
	const rep = fsReport({ MF: [] }, ['MF/7F10/6F3C']);
	assert.deepStrictEqual(rep.missing, []);
	assert.deepStrictEqual(rep.extra,
		[{ path: 'MF/7F10/6F3C', role: 'EF.SMS', tree: 'MF', features: ['Short Message Storage (SMS)'] }]);
	// extra is sorted by path, and a path without a mapped feature is ignored
	const extra = fsReport({ MF: ['Service Provider Name (SPN)'] },
		['MF/7F20/6F45', 'MF/7F10/6F44', 'MF/7F10/6F3C', 'MF/7F20/6F46', 'MF/2F06']).extra;
	assert.deepStrictEqual(extra.map(e => e.path), ['MF/7F10/6F3C', 'MF/7F10/6F44', 'MF/7F20/6F45']);
	assert.deepStrictEqual(extra[2].features, ['Cell Broadcast Message Identifier (CBMI)']);
});

test('servicesFsReport is deterministic and order-stable', () => {
	const present = ['ADF.USIM/6F3B'];
	const a = fsReport({ 'ADF.USIM': ['Service Provider Name (SPN)', 'Fixed Dialling Numbers (FDN)'] }, present);
	const b = fsReport({ 'ADF.USIM': ['Fixed Dialling Numbers (FDN)', 'Service Provider Name (SPN)'] }, present);
	assert.deepStrictEqual(a, b, 'cross-reference order, not input order');
	assert.deepStrictEqual(a.missing, [
		{ feature: 'Fixed Dialling Numbers (FDN)', tree: 'ADF.USIM', paths: ['ADF.USIM/6F4B'] },
		{ feature: 'Service Provider Name (SPN)', tree: 'ADF.USIM', paths: ['ADF.USIM/6F46'] },
	]);
});

test('servicesFsReportHtml renders the paths and the notes', () => {
	const note = 'path-exact';
	const empty = servicesFsReportHtml(fsReport({ MF: [] }, []));
	assert.ok(empty.includes('The filesystem matches the declared services'), empty);
	assert.ok(empty.includes(note), empty);
	assert.ok(servicesFsReportHtml(null).includes('The filesystem matches the declared services'));
	const rep = fsReport({ MF: ['Fixed Dialling Numbers (FDN)'] }, ['MF/7F10/6F3C']);
	const html = servicesFsReportHtml(rep);
	assert.ok(html.includes('Declared services whose files are missing'), html);
	assert.ok(html.includes('Fixed Dialling Numbers (FDN)'), html);
	assert.ok(html.includes('MF/7F10/6F4B'), 'the missing path is shown');
	assert.ok(html.includes('Present files whose service is not declared'), html);
	assert.ok(html.includes('MF/7F10/6F3C'), 'the extra path is shown');
	assert.ok(html.includes('EF.SMS'), 'the extra path names its role');
	assert.ok(html.includes('Short Message Storage (SMS)'), html);
	assert.ok(html.includes(note), html);
	assert.ok(!html.includes('The filesystem matches the declared services'), html);
	// a single-sided report renders only its own heading
	const one = servicesFsReportHtml(fsReport({ MF: ['Fixed Dialling Numbers (FDN)'] }, ['MF/7F10/6F3B']));
	assert.ok(one.includes('Declared services whose files are missing'), one);
	assert.ok(!one.includes('Present files whose service is not declared'), one);
	assert.ok(!one.includes('Declared services that cannot be checked'), one);
	// the unchecked bucket names the roles and explains itself
	const unc = servicesFsReportHtml(fsReport({ 'ADF.USIM': ['Image (IMG)'] }, []));
	assert.ok(unc.includes('Declared services that cannot be checked'), unc);
	assert.ok(unc.includes('Image (IMG)') && unc.includes('DF.GRAPHICS') && unc.includes('EF.IMG'), unc);
	assert.ok(unc.includes('cannot be verified'), unc);
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
	// the pure report from the per-tree declarations, the standard list's path
	// index and the tree's present paths
	const fn = extractFunc(html, 'pysimServicesFsCheck');
	assert.ok(fn.indexOf('await pysimFsRefresh()') > 0, fn);
	assert.ok(fn.indexOf('await pysimFsProbeAll()') > 0, fn);
	assert.ok(fn.indexOf('servicesFsDeclaredTrees(cache.files)') > 0, fn);
	assert.ok(fn.indexOf('await servicesPathIndex()') > 0, fn);
	assert.ok(fn.indexOf('index.size') > 0, 'an unloadable list is an error, not a wall of unchecked rows');
	assert.ok(fn.indexOf('servicesFsPresentPaths(pysimFsTreeRoot)') > 0, fn);
	assert.ok(fn.indexOf('servicesFsReport(') > 0, fn);
	// the path index is loaded once from the standard file list
	const loader = extractFunc(html, 'servicesPathIndex');
	assert.ok(loader.indexOf('uiccStdFilesLoad()') > 0, loader);
	assert.ok(loader.indexOf('servicesPathIndexBuild(') > 0, loader);
	// the grid's render redraws the kept report (language switch)
	const render = extractFunc(html, 'pysimServicesRender');
	assert.ok(render.indexOf('pysimServicesFsRender()') > 0, render);
	// and a card-session change drops it with the cache
	assert.match(html, /function pysimServicesReset\(\) \{\s*_pysimServicesCache = null;\s*_pysimServicesFsReport = null;/);
});
