const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const asset = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'uicc_files.json'), 'utf8'));

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

// The RFM SELECT file picker: merge of the card tree / custom files / the
// shipped standard list, and the per-method fill logic (relative FID chains
// per TS 102 221 11.1.1.2, path from MF without the MF identifier per
// ISO 7816-4, path from the current DF per TS 102 221 11.1.1.2 P1=09).
const FNS = ['chainKind', 'rfmRowKey', 'rfmParentPath', 'rfmEntryByPath', 'rfmRoots',
	'rfmFileEntries', 'rfmStartDf', 'rfmSetStartDf', 'rfmNoticeClear', 'rfmApplySelect',
	'rfmCurrentDf', 'rfmFidsFrom', 'rfmRelativeChain', 'rfmSelectFill', 'rfmFileOptionsHtml',
	'escHtml', 'esc', 'pysimFsNodePath', 'pysimCustomFid'];
let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['const _chains = \\{\\};', 'let _uiccStdFiles = null;', 'let _uiccStdLoad = null;',
	'let _rfmShowAll = false;', 'const _rfmFilter = \\{\\};', 'const _rfmNotice = \\{\\};',
	'const _rfmStartDf = \\{\\};']) {
	const m = html.match(new RegExp(c));
	if (!m) throw new Error('declaration not found: ' + c);
	code += m[0].replace(/^(const|let) /, 'var ') + '\n';
}
code += 'var pysimCustomFiles = [];\n';
code += 'var pysimFsTreeRoot = null;\n';
code += 'function chainRender() {}\n';
eval(code);

// The shipped asset stands in for the fetched standard list.
_uiccStdFiles = asset.files;

function entry(p) {
	const e = _uiccStdFiles.find(f => f.path === p);
	assert.ok(e, 'asset entry missing: ' + p);
	return e;
}

function row(cmd, fields) { return { cmd: cmd, fields: fields || {} }; }

function setChain(chainId, rows) {
	_chains[chainId] = { rows: rows || [] };
	return chainId;
}

test('the shipped asset is well formed and covers the standard trees', () => {
	assert.ok(asset.files.length > 300, 'entries: ' + asset.files.length);
	const paths = asset.files.map(f => f.path);
	assert.strictEqual(paths.length, new Set(paths).size, 'duplicate paths');
	assert.strictEqual(entry('MF/2FE2').name, 'EF.ICCID');
	assert.strictEqual(entry('MF/7F10').name, 'DF.TELECOM');
	assert.strictEqual(entry('MF/7F10/6F40').name, 'EF.MSISDN');
	assert.strictEqual(entry('MF/7F20/6F46').name, 'EF.SPN');
	assert.strictEqual(entry('ADF.USIM/6F07').name, 'EF.IMSI');
	assert.strictEqual(entry('ADF.USIM').kind, 'adf');
	assert.strictEqual(entry('ADF.USIM').aid, 'A0000000871002');
	assert.strictEqual(entry('ADF.ISIM').aid, 'A0000000871004');
});

test('rfmFidsFrom walks a path down to the FIDs', () => {
	const list = rfmFileEntries();
	assert.strictEqual(rfmFidsFrom(list, 'MF', 'MF/7F10/6F40'), '7F106F40');
	assert.strictEqual(rfmFidsFrom(list, 'MF/7F10', 'MF/7F10/6F40'), '6F40');
	assert.strictEqual(rfmFidsFrom(list, 'MF/7F10', 'MF/7F20'), '');
	assert.strictEqual(rfmFidsFrom(list, 'MF', 'ADF.USIM/6F07'), '');
});

test('rfmRelativeChain uses the FID search order (children/parent/siblings)', () => {
	const list = rfmFileEntries();
	assert.strictEqual(rfmRelativeChain(list, 'MF', 'MF/7F10/6F40'), '7F10,6F40');
	// Sibling branch: from DF.TELECOM to EF.SPN under DF.GSM.
	assert.strictEqual(rfmRelativeChain(list, 'MF/7F10', 'MF/7F20/6F46'), '7F20,6F46');
	// One level up then sideways.
	assert.strictEqual(rfmRelativeChain(list, 'MF/7F10/5F3A', 'MF/7F20'), '7F10,7F20');
	assert.strictEqual(rfmRelativeChain(list, 'ADF.USIM', 'ADF.USIM/6F07'), '6F07');
	assert.strictEqual(rfmRelativeChain(list, 'ADF.USIM/5F3A', 'ADF.USIM/6F07'), '6F07');
	// Different roots: a FID select cannot cross into another tree.
	assert.strictEqual(rfmRelativeChain(list, 'MF', 'ADF.USIM/6F07'), '');
	assert.strictEqual(rfmRelativeChain(list, 'ADF.USIM', 'MF/2FE2'), '');
});

test('rfmSelectFill keeps a method that can express the pick', () => {
	setChain('chain-sim', [row('select', { method: 'fid' })]);
	let fill = rfmSelectFill('chain-sim', 0, entry('MF/7F10'), rfmFileEntries());
	assert.deepStrictEqual(fill, { method: 'fid', fields: { fid: '7F10' }, switched: false });
	setChain('chain-sim', [row('select', { method: 'chain' })]);
	fill = rfmSelectFill('chain-sim', 0, entry('MF/7F10/6F40'), rfmFileEntries());
	assert.deepStrictEqual(fill, { method: 'chain', fields: { chain: '7F10,6F40' }, switched: false });
});

test('rfmSelectFill prefers path when the method has to switch', () => {
	// SIM RFM: path is P1=08 (from MF) only.
	setChain('chain-sim', [row('select', { method: 'fid' })]);
	let fill = rfmSelectFill('chain-sim', 0, entry('MF/7F10/6F40'), rfmFileEntries());
	assert.deepStrictEqual(fill, { method: 'path', fields: { path: '7F106F40', base: 'mf' }, switched: true });
	// ADF entry cannot be reached from MF by FID/path/chain.
	assert.strictEqual(rfmSelectFill('chain-sim', 0, entry('ADF.USIM/6F07'), rfmFileEntries()), null);
	// USIM RFM starts in ADF.USIM: its files are direct children, deep ones use
	// the relative path from the current DF.
	setChain('chain-usim', [row('select', { method: 'fid' })]);
	fill = rfmSelectFill('chain-usim', 0, entry('ADF.USIM/6F07'), rfmFileEntries());
	assert.deepStrictEqual(fill, { method: 'fid', fields: { fid: '6F07' }, switched: false });
	setChain('chain-usim', [row('select', { method: 'fid' })]);
	fill = rfmSelectFill('chain-usim', 0, entry('ADF.USIM/5F3A/4F22'), rfmFileEntries());
	assert.deepStrictEqual(fill, { method: 'path', fields: { path: '5F3A4F22', base: 'df' }, switched: true });
	// Cross-root: an MF file is reachable from the ADF session by path from MF.
	setChain('chain-usim', [row('select', { method: 'fid' })]);
	fill = rfmSelectFill('chain-usim', 0, entry('MF/2FE2'), rfmFileEntries());
	assert.deepStrictEqual(fill, { method: 'path', fields: { path: '2FE2', base: 'mf' }, switched: true });
});

test('rfmSelectFill falls back to chain when path cannot express it', () => {
	// Session in ADF.USIM, currently inside DF.PHONEBOOK: a target in the
	// DF.GSM-ACCESS branch is not under the current DF and not in the MF tree,
	// so only the relative chain works.
	const list = rfmFileEntries();
	setChain('chain-usim', [row('select', { method: 'path', path: '5F3A', base: 'df' })]);
	const fill = rfmSelectFill('chain-usim', 1, entry('ADF.USIM/5F3B/4F20'), list);
	assert.strictEqual(fill.method, 'chain');
	assert.strictEqual(fill.fields.chain, '5F3B,4F20');
	assert.strictEqual(fill.switched, true);
	// ADF roots are selected by AID, not by the picker.
	assert.strictEqual(rfmSelectFill('chain-usim', 1, entry('ADF.USIM'), list), null);
});

test('rfmCurrentDf tracks the session context across rows', () => {
	const list = rfmFileEntries();
	setChain('chain-sim', [
		row('select', { method: 'fid', fid: '7F10' }),
		row('select', { method: 'fid', fid: '6F40' }),
		row('select', { method: 'path', path: '7F106F40' }),
	]);
	assert.strictEqual(rfmCurrentDf('chain-sim', 0, list), 'MF');
	assert.strictEqual(rfmCurrentDf('chain-sim', 1, list), 'MF/7F10');
	// Selecting an EF keeps the current DF.
	assert.strictEqual(rfmCurrentDf('chain-sim', 2, list), 'MF/7F10');
	// A path select sets the DF to the file's parent.
	assert.strictEqual(rfmCurrentDf('chain-sim', 3, list), 'MF/7F10');
	setChain('chain-usim', [
		row('select', { method: 'path', path: '5F3A', base: 'df' }),
		row('select', { method: 'chain', chain: '4F22' }),
	]);
	assert.strictEqual(rfmCurrentDf('chain-usim', 0, list), 'ADF.USIM');
	assert.strictEqual(rfmCurrentDf('chain-usim', 1, list), 'ADF.USIM/5F3A');
	assert.strictEqual(rfmCurrentDf('chain-usim', 2, list), 'ADF.USIM/5F3A');
	// The picker can start the session elsewhere (TAR decides the implicit DF).
	rfmSetStartDf('chain-sim', 'ADF.USIM');
	assert.strictEqual(rfmCurrentDf('chain-sim', 0, list), 'ADF.USIM');
	rfmSetStartDf('chain-sim', 'MF');
});

test('rfmFileEntries merges card tree, custom files and the standard list', () => {
	const mf = { name: 'MF', fid: '3F00', isDir: true, parent: null, children: [] };
	const gsm = { name: 'DF.GSM', fid: '7F20', isDir: true, parent: mf, exists: true, children: [] };
	const adn = { name: 'EF.ADN', fid: '6F3A', isDir: false, parent: gsm, exists: false, children: null };
	const iccid = { name: 'EF.ICCID', fid: '2FE2', isDir: false, parent: mf, exists: true, children: null };
	const customNode = { name: 'EF.MYFILE', fid: '6F99', isDir: false, parent: mf, custom: true, customPath: 'MF/6F99', exists: null, children: null };
	gsm.children.push(adn);
	mf.children.push(gsm, iccid, customNode);
	pysimFsTreeRoot = mf;
	pysimCustomFiles = [{ path: 'MF/6F99', name: 'EF.MYFILE', kind: 'ef' }];
	const list = rfmFileEntries();
	const byPath = {};
	list.forEach(e => { byPath[e.path] = e; });
	// The card tree wins over the standard entry with the same path and hides
	// probed-absent files.
	assert.strictEqual(byPath['MF/7F20'].source, 'card');
	assert.strictEqual(byPath['MF/2FE2'].exists, true);
	assert.ok(!byPath['MF/7F20/6F3A'], 'probed-absent file must be dropped');
	assert.strictEqual(byPath['MF/6F99'].source, 'custom');
	// The standard list still covers branches the card tree has not loaded.
	assert.ok(byPath['MF/7F10/6F40'], 'standard entry missing');
	assert.strictEqual(byPath['MF/7F10/6F40'].source, 'standard');
	// ADF roots are group headers, not entries.
	assert.ok(!list.some(e => e.kind === 'adf'));
	pysimFsTreeRoot = null;
	pysimCustomFiles = [];
});

test('rfmFileOptionsHtml labels files with their path within the root', () => {
	const list = rfmFileEntries();
	setChain('chain-usim', [row('select', { method: 'fid' })]);
	const opts = rfmFileOptionsHtml('chain-usim', 0, list);
	// ADF.USIM session: EF.IMSI is a direct child of the root.
	assert.ok(opts.includes('>EF.IMSI — 6F07<'), opts.match(/>EF\.IMSI[^<]*</)[0]);
	// The root is the optgroup, the path is relative to it (FIDs in order).
	assert.ok(opts.includes('<optgroup label="ADF.USIM">'));
	assert.ok(!opts.includes('6F07 — '), 'no reversed "fid — parent" label');
	// Deep file under a DF: the label reads like the path field.
	rfmSetStartDf('chain-usim', 'MF');
	setChain('chain-sim', [row('select', { method: 'path', path: '7F10' })]);
	const deep = rfmFileOptionsHtml('chain-sim', 1, list);
	assert.ok(deep.includes('>EF.MSISDN — 7F10/6F40<'), deep.match(/>EF\.MSISDN[^<]*</)[0]);
	rfmSetStartDf('chain-usim', 'ADF.USIM');
});

test('rfmFileOptionsHtml groups by root and marks method switches', () => {
	const list = rfmFileEntries();
	setChain('chain-sim', [row('select', { method: 'fid' })]);
	// Default: only what by-FID from MF can express (its direct children).
	const opts = rfmFileOptionsHtml('chain-sim', 0, list);
	assert.ok(opts.includes('<optgroup label="MF">'));
	assert.ok(opts.includes('MF/7F10'), 'DF.TELECOM is a direct child');
	assert.ok(!opts.includes('MF/7F10/6F40'), 'deep files need a switch');
	assert.ok(!opts.includes('<optgroup label="ADF.USIM">'), 'ADF files are not reachable from MF');
	// "all files" reveals the switchable entries, marked with the method they
	// would switch to; files in another root stay hidden (the session start
	// selector covers those).
	_rfmShowAll = true;
	const all = rfmFileOptionsHtml('chain-sim', 0, list);
	assert.ok(all.includes('MF/7F10/6F40'));
	assert.ok(all.includes('→ path'), 'switching entries are marked');
	assert.ok(!all.includes('<optgroup label="ADF.USIM">'));
	// Starting the session in the ADF makes its files pickable instead.
	_rfmShowAll = false;
	rfmSetStartDf('chain-sim', 'ADF.USIM');
	const adf = rfmFileOptionsHtml('chain-sim', 0, list);
	assert.ok(adf.includes('<optgroup label="ADF.USIM">'));
	assert.ok(adf.includes('ADF.USIM/6F07'), 'EF.IMSI is a direct child of ADF.USIM');
	rfmSetStartDf('chain-sim', 'MF');
	_rfmFilter['chain-sim#0'] = 'imsi';
	const filtered = rfmFileOptionsHtml('chain-sim', 0, list);
	assert.ok(!filtered.includes('EF.ADN'));
	delete _rfmFilter['chain-sim#0'];
});
