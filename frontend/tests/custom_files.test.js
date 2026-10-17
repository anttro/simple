const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name, asyncFn) {
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
	return (asyncFn ? 'async ' : '') + src.slice(m.index, i + 1);
}

let code = 'var pysimCustomFiles = [];\nvar pysimCustomEditIndex = null;\nvar _pysimCustomDropped = 0;\nvar pysimFsTreeRoot = null;\n';
for (const fn of ['pysimCustomNormPath', 'pysimCustomKindForName', 'pysimCustomFid',
	'pysimCustomParent', 'pysimCustomRoot', 'pysimCustomKnownDfPaths', 'pysimCustomParentStatus',
	'pysimCustomValidate', 'pysimCustomRewriteDescendants', 'pysimCustomNormalizeEntries',
	'pysimCustomSave', 'pysimCustomRenderRoots', 'pysimCustomRenderParents',
	'pysimCustomRootChanged', 'pysimCustomParentInput', 'pysimCustomSubmit',
	'pysimCustomEdit', 'pysimCustomEditCancel', 'pysimCustomRemove', 'pysimCustomRender',
	'pysimFsNodePath', 'pysimFsFindNodeByPath', 'pysimCustomInject', 'pysimCustomRefreshTree']) {
	// pysimCustomRemove is async (it awaits the shared confirm dialog)
	code += extractFunc(html, fn, fn === 'pysimCustomRemove') + '\n';
}
code += html.match(/const CUSTOM_ROOTS = \[[^\]]*\];/)[0].replace('const ', 'var ') + '\n';
code += 'globalThis.esc = s => s;\nglobalThis.t = s => s;\nglobalThis.pysimFsRenderTree = () => {};\n';
eval(code);
// The dialog sweep replaced the native alert/confirm with the helpers: route
// them to the native stubs setup() installs on globalThis.
globalThis.pysimAlert = m => globalThis.alert(m);
globalThis.pysimConfirm = async m => globalThis.confirm(m);
globalThis.pysimPrompt = async (m, v) => globalThis.prompt(m, v);

function fakeEl(id) {
	const classes = new Set();
	return {
		id, value: '', innerHTML: '', textContent: '', attrs: {}, focused: 0,
		classList: {
			add: (...cs) => cs.forEach(c => classes.add(c)),
			remove: (...cs) => cs.forEach(c => classes.delete(c)),
			contains: c => classes.has(c),
		},
		setAttribute(k, v) { this.attrs[k] = v; },
		focus() { this.focused++; },
		options() { return [...this.innerHTML.matchAll(/value="([^"]+)"/g)].map(m => m[1]); },
	};
}

function setup(entries) {
	const els = {
		'pysim-cf-root': fakeEl('pysim-cf-root'),
		'pysim-cf-parent': fakeEl('pysim-cf-parent'),
		'pysim-cf-parent-list': fakeEl('pysim-cf-parent-list'),
		'pysim-cf-fid': fakeEl('pysim-cf-fid'),
		'pysim-cf-name': fakeEl('pysim-cf-name'),
		'pysim-cf-list': fakeEl('pysim-cf-list'),
		'pysim-cf-add-btn': fakeEl('pysim-cf-add-btn'),
		'pysim-cf-cancel-btn': fakeEl('pysim-cf-cancel-btn'),
	};
	els['pysim-cf-cancel-btn'].classList.add('hidden');
	const store = {};
	globalThis.localStorage = {
		getItem: k => (k in store ? store[k] : null),
		setItem: (k, v) => { store[k] = String(v); },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.alertCalls = [];
	globalThis.alert = m => { globalThis.alertCalls.push(m); };
	globalThis.confirmResult = true;
	globalThis.confirm = () => globalThis.confirmResult;
	pysimCustomFiles = (entries || []).map(e => Object.assign({}, e));
	pysimCustomEditIndex = null;
	_pysimCustomDropped = 0;
	pysimFsTreeRoot = null;
	pysimCustomRenderRoots();
	pysimCustomRenderParents();
	return els;
}

function fill(els, root, parent, fid, name) {
	els['pysim-cf-root'].value = root;
	pysimCustomRenderParents();
	els['pysim-cf-parent'].value = parent;
	els['pysim-cf-fid'].value = fid;
	els['pysim-cf-name'].value = name;
}

test('helpers derive canonical roots, parents, FIDs and kinds', () => {
	assert.strictEqual(pysimCustomNormPath('3f00/7f20/6f46'), 'MF/7F20/6F46');
	assert.strictEqual(pysimCustomNormPath(' mf / a153 '), 'MF/A153');
	assert.strictEqual(pysimCustomNormPath(''), '');
	assert.strictEqual(pysimCustomRoot({ path: 'ADF.USIM/6F07' }), 'ADF.USIM');
	assert.strictEqual(pysimCustomParent({ path: 'MF/A153/4954' }), 'MF/A153');
	assert.strictEqual(pysimCustomParent({ path: 'MF/6F46' }), 'MF');
	assert.strictEqual(pysimCustomFid({ path: 'MF/A153/4954' }), '4954');
	assert.strictEqual(pysimCustomKindForName('EF.SPN'), 'ef');
	assert.strictEqual(pysimCustomKindForName('DF.GSM'), 'df');
	assert.strictEqual(pysimCustomKindForName('XX.SPN'), null);
});

test('migration resolves legacy relative paths and drops the unresolvable', () => {
	const res = pysimCustomNormalizeEntries([
		{ path: '3f00/a153', name: 'DF.A1' },
		{ path: 'a153/4954', name: 'EF.SPNS', fid: '4954', parentFid: 'a153' },
		{ path: 'a153/4955', name: 'EF.SMSCS' },
		{ path: 'ffff/1111', name: 'EF.ORPHAN' },      // no such custom DF -> dropped
		{ path: 'MF/6F46', name: 'BAD.NAME' },          // invalid alias -> dropped
	], []);
	assert.deepStrictEqual(res.files, [
		{ path: 'MF/A153', name: 'DF.A1', kind: 'df' },
		{ path: 'MF/A153/4954', name: 'EF.SPNS', kind: 'ef' },
		{ path: 'MF/A153/4955', name: 'EF.SMSCS', kind: 'ef' },
	]);
	assert.strictEqual(res.dropped, 2);
});

test('validation accepts standard/unknown parents but rejects known EFs', () => {
	const files = [{ path: 'MF/A153', name: 'DF.A1', kind: 'df' }];
	// a parent that is not a custom entry is accepted: it may be a standard DF
	// from the card model, or simply not seen in the tree yet
	const unknown = pysimCustomValidate('MF', 'MF/A999', '6F46', 'EF.SPN', files, null);
	assert.strictEqual(unknown.error, null);
	assert.strictEqual(unknown.path, 'MF/A999/6F46');
	// parent defined -> ok
	assert.strictEqual(pysimCustomValidate('MF', 'MF/A153', '6F46', 'EF.SPN', files, null).path, 'MF/A153/6F46');
	// a bare FID chain typed without the root is completed from the selector
	assert.strictEqual(pysimCustomValidate('MF', 'A153', '6F46', 'EF.SPN', files, null).path, 'MF/A153/6F46');
	// deep chains are fine
	assert.strictEqual(pysimCustomValidate('MF', 'MF/7F20/5F01', '6F46', 'EF.DEEP', [], null).path, 'MF/7F20/5F01/6F46');
	// a parent known here to be an EF is rejected
	assert.match(pysimCustomValidate('MF', 'MF/A153/6F46', '1234', 'EF.X',
		files.concat([{ path: 'MF/A153/6F46', name: 'EF.OTHER', kind: 'ef' }]), null).error, /not a DF/);
	// parent segments must be 4-hex FIDs
	assert.match(pysimCustomValidate('MF', 'MF/FOO', '6F46', 'EF.SPN', files, null).error, /4-hex/);
	// bad FID
	assert.match(pysimCustomValidate('MF', 'MF', '6F4', 'EF.SPN', files, null).error, /4 hex/);
	// bad alias
	assert.match(pysimCustomValidate('MF', 'MF', '6F46', 'SPN', files, null).error, /EF\.|DF\./);
	// duplicate
	assert.match(pysimCustomValidate('MF', 'MF/A153', '6F46', 'EF.SPN',
		files.concat([{ path: 'MF/A153/6F46', name: 'EF.OTHER', kind: 'ef' }]), null).error, /already defined/);
	// editing the same entry is not a duplicate
	assert.strictEqual(pysimCustomValidate('MF', 'MF/A153', '6F46', 'EF.SPN',
		files.concat([{ path: 'MF/A153/6F46', name: 'EF.OTHER', kind: 'ef' }]), 1).error, null);
	// root must be known
	assert.match(pysimCustomValidate('MFX', 'MFX', '6F46', 'EF.SPN', files, null).error, /Root/);
});

test('root and parent suggestions list roots, custom DFs and tree DFs', () => {
	const els = setup([
		{ path: 'MF/A153', name: 'DF.A1', kind: 'df' },
		{ path: 'MF/A153/4954', name: 'EF.SPNS', kind: 'ef' },
		{ path: 'ADF.USIM/6F07', name: 'EF.IMSI', kind: 'ef' },
	]);
	assert.deepStrictEqual(els['pysim-cf-root'].options(), ['MF', 'ADF.USIM', 'ADF.ISIM']);
	let parents = els['pysim-cf-parent-list'].options();
	assert.ok(parents.includes('MF'));
	assert.ok(parents.includes('MF/A153'));
	assert.ok(!parents.includes('MF/A153/4954'), 'EFs are not parent options');
	// DFs known from the loaded file tree are suggested, at any depth
	pysimFsTreeRoot = { name: 'MF', fid: '3F00', isDir: true, parent: null, children: [] };
	const df = { name: 'DF.TELECOM', fid: '7F10', isDir: true, parent: pysimFsTreeRoot, children: [] };
	const sub = { name: 'DF.SUB', fid: '5F01', isDir: true, parent: df, children: [] };
	df.children = [sub];
	pysimFsTreeRoot.children = [df];
	pysimCustomRenderParents();
	parents = els['pysim-cf-parent-list'].options();
	assert.ok(parents.includes('MF/7F10'));
	assert.ok(parents.includes('MF/7F10/5F01'));
	// the ADF root is always a valid parent
	els['pysim-cf-root'].value = 'ADF.USIM';
	pysimCustomRootChanged();
	assert.deepStrictEqual(els['pysim-cf-parent-list'].options(), ['ADF.USIM']);
});

test('parent status classifies root, custom, tree and unknown parents', () => {
	setup([
		{ path: 'MF/A153', name: 'DF.A1', kind: 'df' },
		{ path: 'MF/A153/4954', name: 'EF.SPNS', kind: 'ef' },
	]);
	assert.strictEqual(pysimCustomParentStatus('MF').status, 'root');
	assert.strictEqual(pysimCustomParentStatus('MF/A153').status, 'df');
	assert.strictEqual(pysimCustomParentStatus('MF/A153/4954').status, 'not-df');
	assert.strictEqual(pysimCustomParentStatus('MF/FFFF').status, 'unknown');
	pysimFsTreeRoot = { name: 'MF', fid: '3F00', isDir: true, parent: null, children: [] };
	const df = { name: 'DF.GSM', fid: '7F20', isDir: true, parent: pysimFsTreeRoot, children: [] };
	df.children = [{ name: 'EF.SPN', fid: '6F46', isDir: false, parent: df, children: null }];
	pysimFsTreeRoot.children = [df];
	assert.strictEqual(pysimCustomParentStatus('MF/7F20').status, 'df');
	assert.strictEqual(pysimCustomParentStatus('MF/7F20/6F46').status, 'not-df');
	assert.strictEqual(pysimCustomParentStatus('MF/7F20/9999').status, 'unknown');
});

test('import keeps canonical entries whose parent is outside the custom list', () => {
	const res = pysimCustomNormalizeEntries([
		{ path: '3F00/7f20/5f01/6f46', name: 'EF.DEEP' },
		{ path: 'mf/ffff/6f46', name: 'EF.ORPHAN' },
		{ path: 'MF/6F46', name: 'NOPE' },
	], []);
	assert.deepStrictEqual(res.files, [
		{ path: 'MF/7F20/5F01/6F46', name: 'EF.DEEP', kind: 'ef' },
		{ path: 'MF/FFFF/6F46', name: 'EF.ORPHAN', kind: 'ef' },
	]);
	assert.strictEqual(res.dropped, 1);
});

test('submit adds files under the root and under a defined DF', () => {
	const els = setup([]);
	fill(els, 'MF', 'MF', '6F46', 'EF.SPN');
	pysimCustomSubmit();
	assert.deepStrictEqual(pysimCustomFiles, [{ path: 'MF/6F46', name: 'EF.SPN', kind: 'ef' }]);
	assert.strictEqual(els['pysim-cf-fid'].value, '');
	// add a DF, then a file under it
	fill(els, 'MF', 'MF', 'A153', 'DF.A1');
	pysimCustomSubmit();
	fill(els, 'MF', 'MF/A153', '4954', 'EF.SPNS');
	pysimCustomSubmit();
	assert.deepStrictEqual(pysimCustomFiles.map(c => c.path),
		['MF/6F46', 'MF/A153', 'MF/A153/4954']);
	// a parent that is not a custom entry is allowed (it may be a standard DF
	// the tree has not loaded yet)
	fill(els, 'MF', 'MF/A153', '2222', 'EF.ORPHAN');
	pysimCustomSubmit();
	assert.deepStrictEqual(pysimCustomFiles.map(c => c.path),
		['MF/6F46', 'MF/A153', 'MF/A153/4954', 'MF/A153/2222']);
	assert.strictEqual(globalThis.alertCalls.length, 0);
	// a parent known here to be an EF is rejected
	fill(els, 'MF', 'MF/A153/4954', '3333', 'EF.NOPE');
	pysimCustomSubmit();
	assert.deepStrictEqual(globalThis.alertCalls, ['Parent is not a DF: MF/A153/4954']);
});

test('editing a DF FID rewrites its descendants', () => {
	const els = setup([
		{ path: 'MF/A153', name: 'DF.A1', kind: 'df' },
		{ path: 'MF/A153/4954', name: 'EF.SPNS', kind: 'ef' },
		{ path: 'MF/A153/4955', name: 'EF.SMSCS', kind: 'ef' },
	]);
	pysimCustomEdit(0);
	assert.strictEqual(els['pysim-cf-fid'].value, 'A153');
	els['pysim-cf-fid'].value = 'A154';
	pysimCustomSubmit();
	assert.deepStrictEqual(pysimCustomFiles.map(c => c.path),
		['MF/A154', 'MF/A154/4954', 'MF/A154/4955']);
	assert.strictEqual(pysimCustomEditIndex, null);
});

test('editing an entry to another defined path is rejected', () => {
	const els = setup([
		{ path: 'MF/6F46', name: 'EF.SPN', kind: 'ef' },
		{ path: 'MF/6F44', name: 'EF.SPN2', kind: 'ef' },
	]);
	pysimCustomEdit(0);
	els['pysim-cf-fid'].value = '6F44';
	pysimCustomSubmit();
	assert.deepStrictEqual(globalThis.alertCalls, ['File already defined: MF/6F44']);
	assert.deepStrictEqual(pysimCustomFiles.map(c => c.path), ['MF/6F46', 'MF/6F44']);
	assert.strictEqual(pysimCustomEditIndex, 0);
});

test('deleting a DF cascades to its children only after confirmation', async () => {
	setup([
		{ path: 'MF/A153', name: 'DF.A1', kind: 'df' },
		{ path: 'MF/A153/4954', name: 'EF.SPNS', kind: 'ef' },
		{ path: 'MF/6F46', name: 'EF.SPN', kind: 'ef' },
	]);
	globalThis.confirmResult = false;
	await pysimCustomRemove(0);
	assert.strictEqual(pysimCustomFiles.length, 3, 'cancelled delete must keep everything');
	globalThis.confirmResult = true;
	await pysimCustomRemove(0);
	assert.deepStrictEqual(pysimCustomFiles.map(c => c.path), ['MF/6F46']);
});

test('render shows the canonical path, kind and row actions', () => {
	const els = setup([
		{ path: 'MF/A153', name: 'DF.A1', kind: 'df' },
		{ path: 'MF/A153/4954', name: 'EF.SPNS', kind: 'ef' },
	]);
	pysimCustomRender();
	const out = els['pysim-cf-list'].innerHTML;
	assert.match(out, /MF\/A153/);
	assert.match(out, /MF\/A153\/4954/);
	assert.match(out, /\(DF\)/);
	assert.match(out, /pysimCustomEdit\(0\)/);
	assert.match(out, /pysimCustomRemove\(1\)/);
});

test('render flags dropped legacy entries', () => {
	const els = setup([]);
	_pysimCustomDropped = 3;
	pysimCustomRender();
	assert.match(els['pysim-cf-list'].innerHTML, /3/);
	_pysimCustomDropped = 0;
});
