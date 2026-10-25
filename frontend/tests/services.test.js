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
];

let code = 'var _pysimServicesCache = null;\nvar _pysimServicesBusy = false;\n';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_EST_SERVICES',
	'SERVICES_CROSS_REF', 'SERVICES_FILES', 'SERVICES_TABLES']) {
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
