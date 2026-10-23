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
	'efBytes', 'efHex', 'efAllFf', 'efRstripFf', 'efUcs2Be', 'efGsm7Octets',
	'efAnnexA', 'efBcdAddress', 'efPlmn', 'efPlmnAct', 'efDecSpn', 'efDecPlmnWact',
	'efDecAdn', 'efFindDecoder', 'efDecodeBytes', 'efIsRawOnly', 'efTlv',
	'efPnnText', 'efDecItemsFromHex',
	'efGsm7PlainText', 'efEncTextBytes', 'efPlmnEnc', 'efActEnc', 'efEncSpn',
	'efEncPlmnList', 'efListRows', 'efPlmnRowOrig', 'efEncAdn', 'efEncAdnExt', 'efPnnNameBytes', 'efPnnHexBytes', 'efPnnOrigTlv', 'efEncPnn',
	'efFindEncoder', 'efHasEncoder', 'efEncError',
	'efDecModelValues', 'efDecModelFor', 'efDecEditModel', 'efActTechList', 'efDecFieldHtml',
	'efDecRowHtml', 'efDecGridHtml', 'efDecItemRowHtml', 'efDecItemsHtml',
	'efDecRecordsHtml', 'efDecEditHtml',
	'efDecCollectValues', 'efDecCollectRows', 'efDecCollectItems',
	'efBitsSelected', 'pysimFsDecBitsFilter', 'pysimFsDecRowCheck', 'pysimFsDecItemTag', 'pysimFsDecAddItem',
	'pysimFsDecRemoveItem', 'pysimFsFindNode',
	'pysimFsHasDecoder', 'pysimFsHasEncoder', 'pysimFsDecSave', 'pysimFsDecEdit',
	'pysimFsPillsEnabled', 'pysimFsSyncContentVisibility', 'gsm7Decode',
	'gsm7TextToSeptets', 'gsm7Encode',
	'efDecServiceTable', 'efDecSst', 'efDecAcc', 'efDecPhase', 'efDecHpplmn', 'efDecAd', 'efDecCbmi', 'efDecCbmir', 'efDecIccid', 'efDecImsi', 'efDecPlmnList', 'efDecFplmn', 'efEncServiceTable', 'efEncAcc', 'efPhaseValue', 'efPhaseModel', 'efEncPhase', 'efEncHpplmn', 'efAdOpValue', 'efAdModel', 'efEncAd', 'efEncPlmnOnly', 'efEncIccid', 'efEncImsi', 'efCbmiModel', 'efEncCbmi', 'efCbmirModel', 'efEncCbmir', 
];

let code = 'var pysimFsSelected = null;\nvar pysimFsTreeRoot = null;\nvar pysimFsDecModel = null;\n'
	+ 'var pysimFsDecEditMode = false;\nvar pysimFsEditMode = false;\n';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['GSM7_ALPHABET', 'GSM7_EXT_MAP', 'EF_ACT_TECHS', 'EF_SPN_FIELDS',
	'EF_ADN_FIELDS', 'EF_PLMN_LIST', 'EF_PNN_ITEMS', 'EF_UST_SERVICES', 'EF_SST_SERVICES', 'EF_IST_SERVICES', 'EF_EST_SERVICES', 'EF_PHASE_NAMES', 'EF_ACC_CLASSES', 'EF_UST_FIELDS', 'EF_IST_FIELDS', 'EF_EST_FIELDS', 'EF_ACC_FIELDS', 'EF_PHASE_FIELDS', 'EF_HPPLMN_FIELDS', 'EF_AD_OP_MODES', 'EF_AD_FIELDS', 'EF_PLMN_ONLY_LIST', 'EF_ICCID_FIELDS', 'EF_IMSI_FIELDS', 'EF_CBMI_LIST', 'EF_CBMIR_LIST', 'EF_READONLY_NAMES', 'EF_ENCODERS']) {
	code += extractConst(html, c);
}
eval(code);

// The decoder entries these files resolve to (the full EF_DECODERS registry is
// pinned by ef_decode.test.js; only the model's lookup path is exercised here).
var EF_DECODERS = [
	{ name: 'EF.ICCID', fid: '2fe2', fn: efDecIccid },
	{ name: 'EF.IMSI', fid: '6f07', fn: efDecImsi },
	{ name: 'EF.IST', fid: '6f07', fn: b => efDecServiceTable(b, EF_IST_SERVICES) },
	{ name: 'EF.UST', fid: '6f38', fn: b => efDecServiceTable(b, EF_UST_SERVICES) },
	{ name: 'EF.SST', fid: '6f38', fn: efDecSst },
	{ name: 'EF.EST', fid: '6f56', fn: b => efDecServiceTable(b, EF_EST_SERVICES) },
	{ name: 'EF.HPPLMN', fid: '6f31', fn: efDecHpplmn },
	{ name: 'EF.ACC', fid: '6f78', fn: efDecAcc },
	{ name: 'EF.PHASE', fid: '6fae', fn: efDecPhase },
	{ name: 'EF.AD', fid: '6fad', fn: efDecAd },
	{ name: 'EF.PLMNsel', fid: '6f30', fn: efDecPlmnList },
	{ name: 'EF.FPLMN', fid: '6f7b', fn: efDecFplmn },
	{ name: 'EF.EHPLMN', fid: '6fd9', fn: efDecPlmnList },
	{ name: 'EF.CBMI', fid: '6f45', fn: efDecCbmi },
	{ name: 'EF.CBMID', fid: '6f48', fn: efDecCbmi },
	{ name: 'EF.CBMIR', fid: '6f50', fn: efDecCbmir },
	{ name: 'EF.SPN', fid: '6f46', fn: efDecSpn },
	{ name: 'EF.PLMNwAcT', fid: '6f60', fn: efDecPlmnWact },
	{ name: 'EF.OPLMNwAcT', fid: '6f61', fn: efDecPlmnWact },
	{ name: 'EF.HPLMNwAcT', fid: '6f62', fn: efDecPlmnWact },
	{ name: 'EF.ADN', fid: '6f3a', fn: efDecAdn },
	{ name: 'EF.FDN', fid: '6f3b', fn: efDecAdn },
	{ name: 'EF.SDN', fid: '6f49', fn: efDecAdn },
	{ name: 'EF.MSISDN', fid: '6f40', fn: efDecAdn },
	{ name: 'EF.LND', fid: '6f44', fn: efDecAdn },
	{ name: 'EF.BDN', fid: '6f4d', fn: b => efDecAdn(b, 15) },
];

globalThis.t = k => k;
globalThis.esc = s => String(s);

const SPN_ORIG = '0147534d2d52204348ffffffffffffffff';
const ADN_1 = '42204841203120536963FFFFFFFFFFFF06810628560810FFFFFFFFFFFFFF';
const ADN_FF = 'FF'.repeat(64);
const PLMN_ORIG = '62F210008422F8608000';

test('efFindEncoder / efHasEncoder resolve by name and FID, like the decoders', () => {
	assert.strictEqual(efFindEncoder('EF.SPN', null).fid, '6f46');
	assert.strictEqual(efFindEncoder('ef.plmnwact', null).name, 'EF.PLMNwAcT');
	assert.strictEqual(efFindEncoder('MF/7F10/6F3A/EF.ADN', null).name, 'EF.ADN');
	assert.strictEqual(efFindEncoder('EF.ALIAS', '6f40').name, 'EF.MSISDN');
	assert.strictEqual(efFindEncoder('EF.LOCI', '6f7e'), null);
	assert.strictEqual(efFindEncoder(null, null), null);
	assert.strictEqual(efHasEncoder('EF.SDN', null), true);
	assert.strictEqual(efHasEncoder('EF.LOCI', null), false);
});

test('the model is a TR value set for SPN / PLMNwAcT and record rows for ADN', () => {
	const spn = efDecEditModel('EF.SPN', null, { kind: 'transparent', data: SPN_ORIG });
	assert.strictEqual(spn.mode, 'tr');
	assert.strictEqual(spn.entry.name, 'EF.SPN');
	assert.deepStrictEqual(spn.values, { show_in_hplmn: true, hide_in_oplmn: false, name: 'GSM-R CH' });
	// an all-FF SPN exposes its FF display bits (both set, no name), so an
	// untouched save keeps the file byte-identical
	const emptySpn = efDecEditModel('EF.SPN', '6f46', { kind: 'transparent', data: 'FF'.repeat(17) });
	assert.deepStrictEqual(emptySpn.values, { show_in_hplmn: true, hide_in_oplmn: true });
	const plmn = efDecEditModel('EF.PLMNwAcT', null, { kind: 'transparent', data: PLMN_ORIG });
	assert.strictEqual(plmn.values.plmns.length, 2);
	assert.strictEqual(plmn.values.plmns[0].mcc, '262');
	const adn = efDecEditModel('EF.ADN', null, { kind: 'record', records: [
		{ num: 1, data: ADN_1 },
		{ num: 2, data: ADN_FF },
	] });
	assert.strictEqual(adn.mode, 'records');
	assert.strictEqual(adn.records.length, 2);
	assert.strictEqual(adn.records[0].num, 1);
	assert.strictEqual(adn.records[0].orig, ADN_1);
	assert.strictEqual(adn.records[0].values.name, 'B HA 1 Sic');
	assert.strictEqual(adn.records[0].values.number, '6082658001');
	assert.deepStrictEqual(adn.records[1].values, {});
	assert.strictEqual(adn.records[1].orig, ADN_FF);
	// no encoder -> no model, even when a decoder exists
	assert.strictEqual(efDecEditModel('EF.LOCI', null, { kind: 'transparent', data: '082982608200002080' }), null);
	assert.strictEqual(efDecEditModel('EF.NOPE', 'abcd', { kind: 'transparent', data: '00' }), null);
});

test('the TR form is a field grid carrying data-key inputs and the original hex', () => {
	const model = efDecEditModel('EF.SPN', null, { kind: 'transparent', data: SPN_ORIG });
	const out = efDecEditHtml(model, SPN_ORIG);
	assert.match(out, /class="pysim-dec-edit"/);
	assert.match(out, /data-mode="tr"/);
	assert.ok(out.includes('data-orig="' + SPN_ORIG + '"'));
	assert.match(out, /data-key="show_in_hplmn" checked/);
	assert.match(out, /data-key="hide_in_oplmn"/);
	assert.match(out, /data-key="name" value="GSM-R CH"/);
	assert.strictEqual((out.match(/class="pysim-dec-item /g) || []).length, 3);
	assert.doesNotMatch(out, /pysim-fs-record-cb/);
	// the form's booleans/labels go through t() and the labels are the spec
	// bit semantics (TS 31.102 4.2.12)
	assert.match(out, /Display the network name in HPLMN \/ an SPDI PLMN/);
	assert.match(out, /Hide the service provider name outside HPLMN \/ the SPDI PLMNs/);
});

test('the TR list form is a row editor with per-row remove and an Add button', () => {
	const model = efDecEditModel('EF.PLMNwAcT', null, { kind: 'transparent', data: PLMN_ORIG });
	const out = efDecEditHtml(model, PLMN_ORIG);
	assert.match(out, /class="pysim-dec-edit"/);
	assert.strictEqual((out.match(/class="pysim-dec-row /g) || []).length, 2);
	assert.strictEqual((out.match(/class="pysim-dec-remove/g) || []).length, 2);
	assert.ok(out.includes('✕'));
	assert.match(out, /onclick="pysimFsDecAddRow\(\)"/);
	assert.match(out, />Add network</);
	assert.match(out, /data-key="mcc" value="262"/);
	assert.match(out, /data-key="mnc" value="01"/);
	assert.strictEqual((out.match(/data-tech="GSM" checked/g) || []).length, 1);
	assert.strictEqual((out.match(/data-tech="UTRAN" checked/g) || []).length, 1);
	assert.match(out, /data-tech="NG-RAN"/);
});

test('the record form is one block per record with a hidden selection checkbox', () => {
	const model = efDecEditModel('EF.ADN', null, { kind: 'record', records: [
		{ num: 1, data: ADN_1 },
		{ num: 2, data: ADN_FF },
	] });
	const out = efDecEditHtml(model);
	assert.match(out, /data-mode="records"/);
	assert.strictEqual((out.match(/class="pysim-dec-rec /g) || []).length, 2);
	assert.match(out, /data-rec="1" data-orig="4220/);
	assert.match(out, /data-rec="2" data-orig="FFFF/);
	assert.strictEqual((out.match(/class="pysim-fs-record-cb shrink-0 hidden"/g) || []).length, 2);
	assert.match(out, /Record 1/);
	assert.match(out, /Record 2 — empty/);
	assert.match(out, /data-key="name" value="B HA 1 Sic"/);
	assert.match(out, /data-key="number" value="6082658001"/);
	// every input auto-checks its record block
	assert.match(out, /oninput="pysimFsDecRowCheck\(this\)"/);
});

function fakeRoot(inputs, techs) {
	return {
		querySelector: sel => {
			const m = /^\[data-key="(.+)"\]$/.exec(sel);
			return (m && inputs[m[1]]) || null;
		},
		querySelectorAll: sel => sel === '[data-tech]' ? (techs || []) : [],
	};
}

test('efDecCollectValues reads the DOM inputs back into the decoder shape', () => {
	const spn = efDecCollectValues(fakeRoot({
		show_in_hplmn: { checked: true },
		hide_in_oplmn: { checked: false },
		name: { value: 'GSM-R CH' },
	}), EF_SPN_FIELDS);
	assert.deepStrictEqual(spn, { show_in_hplmn: true, hide_in_oplmn: false, name: 'GSM-R CH' });
	// a missing boolean reads false, a missing text field ''
	assert.deepStrictEqual(efDecCollectValues(fakeRoot({}), EF_ADN_FIELDS), { name: '', number: '' });
	const row = efDecCollectValues(fakeRoot({
		mcc: { value: '262' },
		mnc: { value: '01' },
	}, [
		{ checked: true, getAttribute: () => 'GSM' },
		{ checked: true, getAttribute: () => 'UTRAN' },
		{ checked: false, getAttribute: () => 'NG-RAN' },
	]), EF_PLMN_LIST.itemFields);
	assert.deepStrictEqual(row, { mcc: '262', mnc: '01', access_tech: 'GSM, UTRAN' });
	assert.deepStrictEqual(efDecCollectValues(null, EF_ADN_FIELDS), {});
});

test('efDecCollectRows reads the PLMN row editor back into the decoder shape', () => {
	const rows = [
		fakeRoot({ mcc: { value: '262' }, mnc: { value: '01' } }, [{ checked: true, getAttribute: () => 'GSM' }]),
		fakeRoot({ mcc: { value: '228' }, mnc: { value: '06' } }, [{ checked: true, getAttribute: () => 'UTRAN' }]),
	];
	const out = efDecCollectRows({ querySelectorAll: sel => sel === '.pysim-dec-row' ? rows : [] }, EF_PLMN_LIST.itemFields);
	assert.deepStrictEqual(out, [
		{ mcc: '262', mnc: '01', access_tech: 'GSM' },
		{ mcc: '228', mnc: '06', access_tech: 'UTRAN' },
	]);
	assert.deepStrictEqual(efDecCollectRows(null, EF_PLMN_LIST.itemFields), []);
});

test('changing a record input checks its block', () => {
	const cb = { checked: false };
	const rec = { querySelector: sel => sel === '.pysim-fs-record-cb' ? cb : null };
	const el = { closest: sel => sel === '.pysim-dec-rec' ? rec : null };
	pysimFsDecRowCheck(el);
	assert.strictEqual(cb.checked, true);
	pysimFsDecRowCheck(null);
	assert.strictEqual(cb.checked, true);
});

test('pysimFsHasEncoder resolves the selected file by name or FID', () => {
	pysimFsTreeRoot = { name: 'MF', children: [{ name: 'EF.SPN', fid: '6f46' }, { name: 'EF.ALIAS', fid: '6f3a' }] };
	pysimFsSelected = 'EF.SPN';
	assert.strictEqual(pysimFsHasEncoder(), true);
	pysimFsSelected = 'EF.ALIAS';
	assert.strictEqual(pysimFsHasEncoder(), true);
	pysimFsSelected = '6F46';
	assert.strictEqual(pysimFsHasEncoder(), true);
	pysimFsSelected = 'EF.LOCI';
	assert.strictEqual(pysimFsHasEncoder(), false);
	// the SIM service table shares 6F38 with UST but has no editor
	pysimFsTreeRoot = { name: 'MF', children: [{ name: 'EF.SST', fid: '6f38' }] };
	pysimFsSelected = 'EF.SST';
	assert.strictEqual(pysimFsHasEncoder(), false);
	pysimFsSelected = null;
	assert.strictEqual(pysimFsHasEncoder(), false);
});

test('the Edit decoded button is encoder-gated next to Edit raw', () => {
	const btn = /<button[^>]*id="pysim-fs-edit-dec-btn"[^>]*>/.exec(html);
	assert.ok(btn, 'Edit decoded button');
	assert.ok(btn[0].includes('data-needs="card"'));
	assert.ok(btn[0].includes('data-needs-encoder'));
	assert.ok(btn[0].includes('onclick="pysimFsDecEdit()"'));
	assert.match(html, /data-l10n="Edit decoded">Edit decoded</);
	assert.ok(html.indexOf('id="pysim-fs-edit-btn"') < html.indexOf('id="pysim-fs-edit-dec-btn"'));
	const avail = extractFunc(html, 'pysimApplyAvailability');
	assert.ok(avail.includes("hasAttribute('data-needs-encoder')"));
	assert.ok(avail.includes("t('No decoded editor for this file')"));
});

test('the edit flow reads raw, renders the model and saves only checked records', () => {
	const edit = extractFunc(html, 'pysimFsDecEdit');
	assert.ok(edit.includes("body.mode = 'raw'"));
	assert.ok(edit.includes('efDecEditModel'));
	assert.ok(edit.includes('efDecEditHtml'));
	assert.ok(edit.includes('pysimFsDecEditMode = true'));
	assert.ok(edit.includes("querySelectorAll('.pysim-fs-record-cb')"));

	const save = extractFunc(html, 'pysimFsSave');
	assert.ok(save.includes('pysimFsDecEditMode'));
	assert.ok(save.includes('pysimFsDecSave'));

	const decSave = extractFunc(html, 'pysimFsDecSave');
	assert.ok(decSave.includes("pysimFetch('/api/write'"));
	assert.ok(decSave.includes("querySelectorAll('.pysim-dec-rec')"));
	assert.ok(decSave.includes("querySelector('.pysim-fs-record-cb')"));
	assert.ok(decSave.includes('if (cb && !cb.checked) continue;'), 'only checked records are written');
	assert.ok(decSave.includes('body.record_nr = parseInt(num)'));
	assert.ok(decSave.includes('entry.enc('));
	assert.ok(decSave.includes('efDecCollectRows(root, entry.list.itemFields)'));
	assert.ok(decSave.includes("getAttribute('data-orig')"));
	assert.ok(decSave.includes('_pysimArrCache = null'));

	const add = extractFunc(html, 'pysimFsDecAddRow');
	assert.ok(add.includes('efDecRowHtml'));
	const remove = extractFunc(html, 'pysimFsDecRemoveRow');
	assert.ok(remove.includes("closest('.pysim-dec-row')"));
});

function fakeRecordBlock(num, orig, inputs, checked) {
	return {
		querySelector: sel => {
			if (sel === '.pysim-fs-record-cb') return { checked: checked };
			const m = /^\[data-key="(.+)"\]$/.exec(sel);
			return (m && inputs[m[1]]) || null;
		},
		querySelectorAll: () => [],
		getAttribute: k => k === 'data-rec' ? String(num) : (k === 'data-orig' ? orig : null),
	};
}

function saveStubs(result) {
	const bodies = [];
	globalThis.pysimFetch = async (path, body) => {
		bodies.push({ path: path, body: body });
		return typeof result === 'function' ? result(path, body) : result;
	};
	globalThis.pysimFsSelectBody = () => ({ name: 'EF.ADN' });
	globalThis.pysimFsShowError = (el, sw, err) => { el.textContent = 'SW: ' + sw + ' — ' + err; };
	let cancelled = 0;
	globalThis.pysimFsCancel = () => { cancelled++; };
	return { bodies: bodies, cancelled: () => cancelled };
}

test('the decoded save writes a TR list as one re-encoded /api/write', async () => {
	const entry = efFindEncoder('EF.PLMNwAcT', null);
	pysimFsDecModel = { entry: entry, mode: 'tr' };
	const rows = [
		fakeRoot({ mcc: { value: '262' }, mnc: { value: '01' } }, [{ checked: true, getAttribute: () => 'GSM' }]),
		fakeRoot({ mcc: { value: '228' }, mnc: { value: '06' } }, [{ checked: true, getAttribute: () => 'UTRAN' }]),
	];
	const root = {
		getAttribute: k => k === 'data-orig' ? PLMN_ORIG : null,
		querySelectorAll: sel => sel === '.pysim-dec-row' ? rows : [],
	};
	const out = { querySelector: sel => sel === '.pysim-dec-edit' ? root : null, querySelectorAll: () => [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.PLMNwAcT', status, out, false);
	assert.strictEqual(stubs.bodies.length, 1);
	assert.strictEqual(stubs.bodies[0].path, '/api/write');
	assert.strictEqual(stubs.bodies[0].body.data, PLMN_ORIG);
	assert.strictEqual(stubs.bodies[0].body.record_nr, undefined);
	assert.strictEqual(status.textContent, 'SW: 9000 OK');
	assert.strictEqual(stubs.cancelled(), 1);
});

test('the decoded save writes only the checked ADN records, with record_nr', async () => {
	const entry = efFindEncoder('EF.ADN', null);
	pysimFsDecModel = { entry: entry, mode: 'records' };
	const blocks = [
		fakeRecordBlock(1, ADN_1, { name: { value: 'Ann' }, number: { value: '123' } }, true),
		fakeRecordBlock(2, ADN_FF, { name: { value: '' }, number: { value: '' } }, false),
	];
	const out = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? blocks : [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status, out, false);
	assert.strictEqual(stubs.bodies.length, 1);
	assert.strictEqual(stubs.bodies[0].body.record_nr, 1);
	assert.strictEqual(stubs.bodies[0].body.data, efEncAdn({ name: 'Ann', number: '123' }, ADN_1));
	assert.strictEqual(status.textContent, 'SW: 9000 OK');
	assert.strictEqual(stubs.cancelled(), 1);
});

test('no checked record means no write; a failed write stops at that record', async () => {
	const entry = efFindEncoder('EF.ADN', null);
	pysimFsDecModel = { entry: entry, mode: 'records' };
	const off = [fakeRecordBlock(1, ADN_1, { name: { value: 'Ann' }, number: { value: '1' } }, false)];
	const out = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? off : [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status, out, false);
	assert.strictEqual(stubs.bodies.length, 0);
	assert.match(status.textContent, /No records selected/);
	assert.strictEqual(stubs.cancelled(), 0);

	const on = [fakeRecordBlock(1, ADN_1, { name: { value: 'Ann' }, number: { value: '1' } }, true)];
	const out2 = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? on : [] };
	const stubs2 = saveStubs(() => ({ success: false, sw: '6982', error: 'Security status not satisfied' }));
	const status2 = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status2, out2, false);
	assert.strictEqual(stubs2.bodies.length, 1);
	assert.match(status2.textContent, /6982/);
	assert.match(status2.textContent, /record 1/);
	assert.strictEqual(stubs2.cancelled(), 0);
});

// EF.PNN: the fourth shape - a record is a list of TLVs.
const PNN_1 = '430a82d432bbbc7eb75de432450a82d432bbbc7eb75de432ffffffff';

test('the model builds per-record item lists for an items encoder', () => {
	const model = efDecEditModel('EF.PNN', '6fc5', { kind: 'record', records: [
		{ num: 1, data: PNN_1 },
		{ num: 2, data: ADN_FF },
	] });
	assert.strictEqual(model.mode, 'records');
	assert.deepStrictEqual(model.records[0].items, [
		{ tag: '43', text: 'Telekom.de', orig: '430A82D432BBBC7EB75DE432' },
		{ tag: '45', text: 'Telekom.de', orig: '450A82D432BBBC7EB75DE432' },
	]);
	assert.strictEqual(model.records[0].values, undefined);
	assert.deepStrictEqual(model.records[1].items, []);
});

test('the items form renders a tag select, the value input, remove and Add TLV per record', () => {
	const model = efDecEditModel('EF.PNN', '6fc5', { kind: 'record', records: [
		{ num: 1, data: PNN_1 },
		{ num: 2, data: ADN_FF },
	] });
	const out = efDecEditHtml(model);
	assert.strictEqual((out.match(/class="pysim-dec-item-row /g) || []).length, 2);
	assert.strictEqual((out.match(/class="pysim-dec-items /g) || []).length, 2);
	assert.strictEqual((out.match(/data-itag/g) || []).length, 2);
	assert.strictEqual((out.match(/class="pysim-dec-remove /g) || []).length, 2);
	assert.strictEqual((out.match(/class="pysim-dec-add /g) || []).length, 2);
	assert.match(out, /data-tag="43"/);
	assert.match(out, /data-tag="45"/);
	assert.match(out, /data-ikey="text" value="Telekom.de"/);
	assert.match(out, /<option value="43" data-kind="name" selected>43 Full name<\/option>/);
	assert.match(out, />Add TLV</);
	// the empty record still gets its own Add TLV button
	assert.match(out, /Record 2 — empty/);
});

test('an unknown tag in an items record stays read-only without a remove button', () => {
	const hex = '430a82d432bbbc7eb75de4325a02aabb' + 'ff'.repeat(14);
	const model = efDecEditModel('EF.PNN', '6fc5', { kind: 'record', records: [{ num: 1, data: hex }] });
	const out = efDecEditHtml(model);
	assert.match(out, /data-tag="5A"/);
	assert.strictEqual((out.match(/data-itag/g) || []).length, 1, 'the raw row has no tag select');
	assert.match(out, /data-ikey="hex" value="AABB" readonly/);
	assert.strictEqual((out.match(/class="pysim-dec-remove /g) || []).length, 1);
});

function fakeItemRow(tag, kind, value) {
	return {
		getAttribute: k => k === 'data-tag' ? tag : null,
		querySelector: sel => sel === '[data-ikey]' ? { getAttribute: () => kind, value: value } : null,
	};
}

test('efDecCollectItems reads the TLV rows in DOM order', () => {
	const rows = [
		fakeItemRow('43', 'text', 'Name'),
		fakeItemRow('80', 'hex', '00410042'),
		fakeItemRow('5A', 'hex', 'AABB'),
	];
	const root = { querySelectorAll: sel => sel === '.pysim-dec-item-row' ? rows : [] };
	assert.deepStrictEqual(efDecCollectItems(root), [
		{ tag: '43', text: 'Name' },
		{ tag: '80', hex: '00410042' },
		{ tag: '5A', hex: 'AABB' },
	]);
	assert.deepStrictEqual(efDecCollectItems(null), []);
});

test('the decoded save encodes item records through the entry encoder', async () => {
	const entry = efFindEncoder('EF.PNN', null);
	pysimFsDecModel = { entry: entry, mode: 'records' };
	const rows = [
		fakeItemRow('43', 'text', 'Telekom.de'),
		fakeItemRow('80', 'hex', '00410042'),
	];
	const block = {
		querySelector: sel => sel === '.pysim-fs-record-cb' ? { checked: true } : null,
		querySelectorAll: sel => sel === '.pysim-dec-item-row' ? rows : [],
		getAttribute: k => k === 'data-rec' ? '1' : (k === 'data-orig' ? PNN_1 : null),
	};
	const out = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? [block] : [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.PNN', status, out, false);
	assert.strictEqual(stubs.bodies.length, 1);
	assert.strictEqual(stubs.bodies[0].body.record_nr, 1);
	assert.strictEqual(stubs.bodies[0].body.data,
		efEncPnn([{ tag: '43', text: 'Telekom.de' }, { tag: '80', hex: '00410042' }], PNN_1));
	assert.strictEqual(status.textContent, 'SW: 9000 OK');
	assert.strictEqual(stubs.cancelled(), 1);
});

test('the item add/remove/tag handlers are wired to the record', () => {
	const add = extractFunc(html, 'pysimFsDecAddItem');
	assert.ok(add.includes("querySelector('.pysim-dec-items')"));
	assert.ok(add.includes('efDecItemRowHtml'));
	assert.ok(add.includes('pysimFsDecRowCheck'));
	const rem = extractFunc(html, 'pysimFsDecRemoveItem');
	assert.ok(rem.includes("closest('.pysim-dec-item-row')"));
	assert.ok(rem.includes('pysimFsDecRowCheck'));
	const tag = extractFunc(html, 'pysimFsDecItemTag');
	assert.ok(tag.includes("setAttribute('data-tag'"));
	assert.ok(tag.includes("setAttribute('data-ikey'"));
});

test('the BDN model decodes the X+15 layout and keeps the CMI pointer out of the form', () => {
	const BDN = '54455354' + '06' + '91' + '2143658709' + 'FF'.repeat(7) + '03';
	const model = efDecEditModel('EF.BDN', '6f4d', { kind: 'record', records: [{ num: 1, data: BDN }] });
	assert.deepStrictEqual(model.records[0].values, { name: 'TEST', number: '+1234567890', cmi: 3 });
	const out = efDecEditHtml(model);
	assert.match(out, /data-key="name" value="TEST"/);
	assert.match(out, /data-key="number" value="\+1234567890"/);
	assert.doesNotMatch(out, /cmi/);
	// the save path re-encodes with the 15-byte layout (the CMI byte survives)
	const entry = efFindEncoder('EF.BDN', null);
	assert.strictEqual(entry.enc({ name: 'TEST', number: '+1234567890' }, BDN), BDN);
});

// A control-string/extended-BCD ADN record (the decoder only sees '100').
const SSC_ADN = '54455354' + 'FF'.repeat(12) + '04' + 'FF' + 'BA01B0' + 'FF'.repeat(7) + 'FF' + 'FF';

function adnBlock(num, orig, name, number, checked) {
	const inputs = { name: { value: name }, number: { value: number } };
	return {
		querySelector: sel => {
			if (sel === '.pysim-fs-record-cb') return { checked: checked };
			const m = /^\[data-key="(.+)"\]$/.exec(sel);
			return m ? inputs[m[1]] : null;
		},
		querySelectorAll: () => [],
		getAttribute: k => k === 'data-rec' ? String(num) : (k === 'data-orig' ? orig : null),
	};
}

test('F1: the save path writes an untouched SSC record byte-identically', async () => {
	const entry = efFindEncoder('EF.ADN', null);
	pysimFsDecModel = { entry: entry, mode: 'records' };
	const out = { querySelector: () => null,
		querySelectorAll: sel => sel === '.pysim-dec-rec' ? [adnBlock(1, SSC_ADN, 'TEST', '100', true)] : [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status, out, false);
	assert.strictEqual(stubs.bodies.length, 1);
	assert.strictEqual(stubs.bodies[0].body.record_nr, 1);
	assert.strictEqual(stubs.bodies[0].body.data, SSC_ADN);
});

test('F3: a later record that cannot be encoded writes nothing', async () => {
	const entry = efFindEncoder('EF.ADN', null);
	pysimFsDecModel = { entry: entry, mode: 'records' };
	// record 2 has a non-FF EXT1 and an edited number -> efEncAdn refuses
	const ext1 = ADN_1.slice(0, -2) + '03';
	const blocks = [
		adnBlock(1, ADN_1, 'B HA 1 Sic', '6082658001', true),
		adnBlock(2, ext1, 'B HA 1 Sic', '6082658002', true),
	];
	const out = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? blocks : [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status, out, false);
	assert.strictEqual(stubs.bodies.length, 0, 'nothing may be written');
	assert.match(status.textContent, /record 2/);
	assert.match(status.textContent, /EF\.EXT1/);
	assert.strictEqual(stubs.cancelled(), 0, 'the form stays open');
});

test('F3: a failed write names the record and how many were written', async () => {
	const entry = efFindEncoder('EF.ADN', null);
	pysimFsDecModel = { entry: entry, mode: 'records' };
	const blocks = [
		adnBlock(1, ADN_1, 'B HA 1 Sic', '6082658001', true),
		adnBlock(2, ADN_1, 'B HA 1 Sic', '6082658001', true),
	];
	const out = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? blocks : [] };
	let calls = 0;
	const stubs = saveStubs(() => (++calls === 1
		? { success: true, sw: '9000' }
		: { success: false, sw: '6982', error: 'Security status not satisfied' }));
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status, out, false);
	assert.strictEqual(stubs.bodies.length, 2);
	assert.match(status.textContent, /record 2/);
	assert.match(status.textContent, /1 of 2 records written/);
	assert.strictEqual(stubs.cancelled(), 0, 'the form stays open');
});

test('F6: a missing decoded model refuses to write', async () => {
	pysimFsDecModel = null;
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ADN', status, { querySelector: () => null, querySelectorAll: () => [] }, false);
	assert.strictEqual(stubs.bodies.length, 0);
	assert.match(status.textContent, /No decoded editor/);
});

test('F2: untouched PNN items keep their TLV bytes through the DOM and the save', async () => {
	const entry = efFindEncoder('EF.PNN', null);
	const model = efDecEditModel('EF.PNN', '6fc5', { kind: 'record', records: [{ num: 1, data: PNN_1 }] });
	const form = efDecEditHtml(model);
	assert.ok(form.includes('data-iorig="430A82D432BBBC7EB75DE432"'));
	assert.ok(form.includes('data-iorig="450A82D432BBBC7EB75DE432"'));
	// the row carries its original TLV into the collection
	const row = {
		getAttribute: k => k === 'data-tag' ? '43' : (k === 'data-iorig' ? '430A82D432BBBC7EB75DE432' : null),
		querySelector: sel => sel === '[data-ikey]' ? { getAttribute: () => 'text', value: 'Telekom.de' } : null,
	};
	assert.deepStrictEqual(
		efDecCollectItems({ querySelectorAll: sel => sel === '.pysim-dec-item-row' ? [row] : [] }),
		[{ tag: '43', text: 'Telekom.de', orig: '430A82D432BBBC7EB75DE432' }]);
	// an untouched save is byte-identical
	pysimFsDecModel = { entry: entry, mode: 'records' };
	const rows = efDecItemsFromHex(entry, PNN_1).map(it => ({
		getAttribute: k => k === 'data-tag' ? it.tag : (k === 'data-iorig' ? it.orig : null),
		querySelector: sel => sel === '[data-ikey]' ? {
			getAttribute: () => (it.text !== undefined ? 'text' : 'hex'),
			value: it.text !== undefined ? it.text : it.hex,
		} : null,
	}));
	const block = {
		querySelector: sel => sel === '.pysim-fs-record-cb' ? { checked: true } : null,
		querySelectorAll: sel => sel === '.pysim-dec-item-row' ? rows : [],
		getAttribute: k => k === 'data-rec' ? '1' : (k === 'data-orig' ? PNN_1 : null),
	};
	const out = { querySelector: () => null, querySelectorAll: sel => sel === '.pysim-dec-rec' ? [block] : [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.PNN', status, out, false);
	assert.strictEqual(stubs.bodies[0].body.data, PNN_1.toUpperCase());
});

test('F12: a selection change during the read abandons the decoded edit', async () => {
	pysimFsSelected = 'EF.SPN';
	pysimFsTreeRoot = { name: 'MF', children: [{ name: 'EF.SPN', fid: '6f46' }] };
	const fakeEl = () => {
		const classes = new Set();
		return {
			innerHTML: '', textContent: '',
			classList: {
				add: (...c) => c.forEach(x => classes.add(x)),
				remove: (...c) => c.forEach(x => classes.delete(x)),
				toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
				contains: c => classes.has(c),
			},
		};
	};
	const content = fakeEl();
	const status = fakeEl();
	const buttons = {
		'pysim-fs-content': content, 'pysim-fs-status': status,
		'pysim-fs-edit-btn': fakeEl(), 'pysim-fs-edit-dec-btn': fakeEl(),
		'pysim-fs-save-btn': fakeEl(), 'pysim-fs-cancel-btn': fakeEl(),
	};
	globalThis.document = {
		getElementById: id => buttons[id] || null,
		querySelectorAll: () => [],
	};
	globalThis.t = k => k;
	globalThis.esc = s => String(s);
	let release = null;
	globalThis.pysimFetch = () => new Promise(res => { release = () => res({ success: true, sw: '9000', data: SPN_ORIG }); });
	globalThis.pysimFsSelectBody = () => ({});
	globalThis.pysimFsShowError = () => {};
	let resets = 0;
	globalThis.pysimFsResetEdit = () => { resets++; };
	const pending = pysimFsDecEdit();
	pysimFsSelected = 'EF.ADN';
	release();
	await pending;
	assert.strictEqual(resets, 1);
	assert.strictEqual(content.innerHTML, '', 'the old file must not render over the new selection');
});

// Wave 2 fixtures.
const UST_ONE = '00000010';                  // service 29
const ACC_ONE = '8001';                      // classes 15 and 0
const CBMI_HEX = '0010FFFF0020';
const CBMIR_HEX = '0000FFFE';
const ICCID_HEX = '988812010000400310f0';

test('the bits field renders a filtered checkbox grid and collects {n,name}', () => {
	const model = efDecEditModel('EF.UST', '6f38', { kind: 'transparent', data: UST_ONE });
	assert.deepStrictEqual(model.values.services, [{ n: 29, name: 'Data download via SMS-CB' }]);
	const out = efDecEditHtml(model, UST_ONE);
	assert.match(out, /class="pysim-dec-bits /);
	assert.match(out, /pysim-dec-bits-filter/);
	assert.match(out, /data-bit="29" data-bit-name="Data download via SMS-CB" checked/);
	// the whole UST table is rendered (146 services)
	assert.strictEqual((out.match(/data-bit="/g) || []).length, 146);
	// ACC: 15 editable classes (10 is always written 0), 15 and 0 checked
	const acc = efDecEditHtml(efDecEditModel('EF.ACC', '6f78', { kind: 'transparent', data: ACC_ONE }), ACC_ONE);
	assert.strictEqual((acc.match(/data-bit="/g) || []).length, 15);
	assert.match(acc, /data-bit="15" data-bit-name="Class 15" checked/);
	assert.match(acc, /data-bit="0" data-bit-name="Class 0" checked/);
	assert.doesNotMatch(acc, /data-bit="10"/);
	assert.match(acc, /data-bit="11" data-bit-name="Class 11"/);
	// the collection yields the checked {n,name} list
	const root = {
		querySelectorAll: sel => sel === '[data-bit]' ? [
			{ checked: true, getAttribute: k => k === 'data-bit' ? '1' : 'Fixed Dialling Numbers (FDN)' },
			{ checked: false, getAttribute: () => '2' },
			{ checked: true, getAttribute: k => k === 'data-bit' ? '3' : 'APN Control List (ACL)' },
		] : [],
	};
	assert.deepStrictEqual(efDecCollectValues(root, EF_EST_FIELDS), { services: [
		{ n: 1, name: 'Fixed Dialling Numbers (FDN)' },
		{ n: 3, name: 'APN Control List (ACL)' },
	] });
});

test('the bits filter hides the non-matching rows', () => {
	const rows = [
		{ textContent: '1 Local Phone Book', style: {} },
		{ textContent: '2 Fixed Dialling Numbers', style: {} },
	];
	const box = { querySelectorAll: sel => sel === '.pysim-dec-bit-row' ? rows : [] };
	const input = { value: 'fixed', closest: sel => sel === '.pysim-dec-bits' ? box : null };
	pysimFsDecBitsFilter(input);
	assert.strictEqual(rows[0].style.display, 'none');
	assert.strictEqual(rows[1].style.display, '');
});

test('the select keeps an unknown current value as an option', () => {
	const model = efDecEditModel('EF.PHASE', '6fae', { kind: 'transparent', data: '0F' });
	assert.deepStrictEqual(model.values, { phase_hex: '0F' });
	const out = efDecEditHtml(model, '0F');
	assert.match(out, /<option value="0F" selected>Current 0F<\/option>/);
	const known = efDecEditHtml(efDecEditModel('EF.PHASE', '6fae', { kind: 'transparent', data: '03' }), '03');
	assert.match(known, /<option value="03" selected>phase 2 and PROFILE DOWNLOAD required<\/option>/);
	// AD unknown op mode
	const ad = efDecEditHtml(efDecEditModel('EF.AD', '6fad',
		{ kind: 'transparent', data: '55' + '0000' + '03' + 'AABB' }), '55' + '0000' + '03' + 'AABB');
	assert.match(ad, /<option value="55" selected>Current 55<\/option>/);
	assert.match(ad, /data-key="additional_info" data-hex="1" value="0000"/);
	assert.match(ad, /data-key="mnc_length" value="3"/);
});

test('the hint fields render their help text', () => {
	const out = efDecEditHtml(efDecEditModel('EF.HPPLMN', '6f31', { kind: 'transparent', data: '2A' }), '2A');
	assert.match(out, /data-key="interval" data-hex="1" value="0x2A" maxlength="4"/);
	assert.match(out, /00 = no higher-priority PLMN search/);
	assert.match(out, /NB-IoT\/EC-GSM-IoT\/eMTC-M1 read the same byte in hours/);
});

test('the wave-2 model hooks shape PHASE/AD/CBMI/CBMIR for the form', () => {
	assert.deepStrictEqual(efDecEditModel('EF.PHASE', '6fae', { kind: 'transparent', data: '02' }).values,
		{ phase_hex: '02' });
	assert.deepStrictEqual(efDecEditModel('EF.AD', '6fad', { kind: 'transparent', data: '80' + '1234' + '02' }).values,
		{ op_mode: '80', additional_info: '1234', mnc_length: '2' });
	const cbmi = efDecEditModel('EF.CBMI', '6f45', { kind: 'transparent', data: CBMI_HEX });
	assert.deepStrictEqual(cbmi.values, { message_ids: [{ id: '0010' }, { id: '0020' }] });
	const cbmir = efDecEditModel('EF.CBMIR', '6f50', { kind: 'transparent', data: CBMIR_HEX });
	assert.deepStrictEqual(cbmir.values, { ranges: [{ low: '0000', high: 'FFFE' }] });
});

test('the CB list forms use the row editor with hex ids/bounds', () => {
	const cbmi = efDecEditModel('EF.CBMI', '6f45', { kind: 'transparent', data: CBMI_HEX });
	const out = efDecEditHtml(cbmi, CBMI_HEX);
	assert.strictEqual((out.match(/class="pysim-dec-row /g) || []).length, 2);
	assert.match(out, /data-key="id" data-hex="1" value="0010"/);
	assert.match(out, /data-hex="1"/);
	assert.match(out, />Add message ID</);
	const cbmir = efDecEditModel('EF.CBMIR', '6f50', { kind: 'transparent', data: CBMIR_HEX });
	const out2 = efDecEditHtml(cbmir, CBMIR_HEX);
	assert.strictEqual((out2.match(/class="pysim-dec-row /g) || []).length, 1);
	assert.match(out2, /data-key="low" data-hex="1" value="0000"/);
	assert.match(out2, /data-key="high" data-hex="1" value="FFFE"/);
	assert.match(out2, />Add range</);
});

test('the confirm entries ask once and only for a changed identity', async () => {
	const decSave = extractFunc(html, 'pysimFsDecSave');
	assert.ok(decSave.includes('entry.confirm'));
	assert.ok(decSave.includes('pysimConfirm(t(entry.confirm))'));
	const entry = efFindEncoder('EF.ICCID', null);
	pysimFsDecModel = { entry: entry, mode: 'tr' };
	const makeOut = value => {
		const root = {
			getAttribute: k => k === 'data-orig' ? ICCID_HEX : null,
			querySelector: sel => sel === '[data-key="iccid"]' ? { value: value } : null,
		};
		return { querySelector: sel => sel === '.pysim-dec-edit' ? root : null, querySelectorAll: () => [] };
	};
	// unchanged -> no dialog
	let asked = 0;
	globalThis.pysimConfirm = async () => { asked++; return true; };
	let stubs = saveStubs({ success: true, sw: '9000' });
	let status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ICCID', status, makeOut('8988211000000430010'), false);
	assert.strictEqual(asked, 0);
	assert.strictEqual(stubs.bodies.length, 1);
	// changed + cancel -> nothing written
	globalThis.pysimConfirm = async () => { asked++; return false; };
	stubs = saveStubs({ success: true, sw: '9000' });
	status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ICCID', status, makeOut('8988211000000430011'), false);
	assert.strictEqual(asked, 1);
	assert.strictEqual(stubs.bodies.length, 0);
	assert.match(status.textContent, /Write cancelled/);
	// changed + accept -> written
	globalThis.pysimConfirm = async () => { asked++; return true; };
	stubs = saveStubs({ success: true, sw: '9000' });
	status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.ICCID', status, makeOut('8988211000000430011'), false);
	assert.strictEqual(asked, 2);
	assert.strictEqual(stubs.bodies.length, 1);
	assert.strictEqual(stubs.bodies[0].body.data, '988812010000400310F1');
});

// Finding 1: the list save passes the collected row ARRAY; the encoders must
// not wipe the file when nothing was edited.
const PLMN_ONLY = '62F21022F860';
const FPLMN_HOLES = '22F860FFFFFF62F210';

function listSaveRoot(orig, rows) {
	const rowEls = rows.map(vals => ({
		querySelector: sel => {
			const m = /^\[data-key="(.+)"\]$/.exec(sel);
			return m && vals[m[1]] !== undefined ? { value: vals[m[1]] } : null;
		},
		querySelectorAll: () => [],
	}));
	return {
		getAttribute: k => k === 'data-orig' ? orig : null,
		querySelectorAll: sel => sel === '.pysim-dec-row' ? rowEls : [],
	};
}

test('F1: a list save with no edits keeps the original bytes', async () => {
	const cases = [
		{ name: 'EF.PLMNsel', fid: '6f30', orig: PLMN_ONLY,
			rows: [{ mcc: '262', mnc: '01' }, { mcc: '228', mnc: '06' }] },
		{ name: 'EF.FPLMN', fid: '6f7b', orig: FPLMN_HOLES,
			rows: [{ mcc: '228', mnc: '06' }, { mcc: '262', mnc: '01' }] },
		{ name: 'EF.EHPLMN', fid: '6fd9', orig: PLMN_ONLY,
			rows: [{ mcc: '262', mnc: '01' }, { mcc: '228', mnc: '06' }] },
		{ name: 'EF.CBMI', fid: '6f45', orig: CBMI_HEX, rows: [{ id: '0010' }, { id: '0020' }] },
		{ name: 'EF.CBMID', fid: '6f48', orig: CBMI_HEX, rows: [{ id: '0010' }, { id: '0020' }] },
		{ name: 'EF.CBMIR', fid: '6f50', orig: CBMIR_HEX, rows: [{ low: '0000', high: 'FFFE' }] },
	];
	for (const c of cases) {
		pysimFsDecModel = { entry: efFindEncoder(c.name, c.fid), mode: 'tr' };
		const out = {
			querySelector: sel => sel === '.pysim-dec-edit' ? listSaveRoot(c.orig, c.rows) : null,
			querySelectorAll: () => [],
		};
		const stubs = saveStubs({ success: true, sw: '9000' });
		const status = { textContent: '' };
		await pysimFsDecSave(null, c.name, status, out, false);
		assert.strictEqual(stubs.bodies.length, 1, c.name);
		assert.strictEqual(stubs.bodies[0].body.data, c.orig,
			c.name + ': a no-edit list save must not wipe the file');
	}
});

test('F1: an edited list row is written through the same save path', async () => {
	const cases = [
		{ name: 'EF.PLMNsel', fid: '6f30', orig: PLMN_ONLY,
			rows: [{ mcc: '262', mnc: '01' }, { mcc: '228', mnc: '07' }] },
		{ name: 'EF.CBMI', fid: '6f45', orig: CBMI_HEX,
			rows: [{ id: '0010' }, { id: '0030' }] },
		{ name: 'EF.CBMIR', fid: '6f50', orig: CBMIR_HEX,
			rows: [{ low: '0000', high: 'FF00' }] },
	];
	for (const c of cases) {
		const entry = efFindEncoder(c.name, c.fid);
		pysimFsDecModel = { entry: entry, mode: 'tr' };
		const out = {
			querySelector: sel => sel === '.pysim-dec-edit' ? listSaveRoot(c.orig, c.rows) : null,
			querySelectorAll: () => [],
		};
		const stubs = saveStubs({ success: true, sw: '9000' });
		const status = { textContent: '' };
		await pysimFsDecSave(null, c.name, status, out, false);
		const expected = entry.enc(c.rows, c.orig);
		assert.notStrictEqual(expected, c.orig, c.name + ': the edit must change the bytes');
		assert.strictEqual(stubs.bodies.length, 1, c.name);
		assert.strictEqual(stubs.bodies[0].body.data, expected, c.name);
	}
});

test('F5: an untouched all-FF AD saves FF through the collected form value', async () => {
	const entry = efFindEncoder('EF.AD', null);
	const model = efDecEditModel('EF.AD', null, { kind: 'transparent', data: 'FF'.repeat(6) });
	assert.deepStrictEqual(model.values, { op_mode: 'FF', additional_info: '', mnc_length: '' });
	const form = efDecEditHtml(model, 'FF'.repeat(6));
	assert.match(form, /<option value="FF" selected>Current FF<\/option>/);
	pysimFsDecModel = model;
	const vals = { op_mode: 'FF', additional_info: '', mnc_length: '' };
	const root = {
		getAttribute: k => k === 'data-orig' ? 'FF'.repeat(6) : null,
		querySelector: sel => {
			const m = /^\[data-key="(.+)"\]$/.exec(sel);
			return m ? { value: vals[m[1]] } : null;
		},
	};
	const out = { querySelector: sel => sel === '.pysim-dec-edit' ? root : null, querySelectorAll: () => [] };
	const stubs = saveStubs({ success: true, sw: '9000' });
	const status = { textContent: '' };
	await pysimFsDecSave(null, 'EF.AD', status, out, false);
	assert.strictEqual(stubs.bodies.length, 1);
	assert.strictEqual(stubs.bodies[0].body.data, 'FF'.repeat(6));
});
