// The service-table rows' cross-link to the file-tree filter: a table row
// that is backed by files gets a "To EF filter" button that narrows the tree
// to exactly that service's files.
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

// module state the extracted functions close over
let pysimFsFilter = new Set(['all']);
let _servicesFeatureByNumber = null;
// the tree filter's matching-path cache (pysimFsSetFilterToService drops it)
let _fsFilterPaths = null;
let _fsFilterPathsKey = null;
const _store = {};
let _renders = 0;
let _treeRenders = 0;

globalThis.localStorage = {
	getItem: k => (Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null),
	setItem: (k, v) => { _store[k] = String(v); },
	removeItem: k => { delete _store[k]; },
};
globalThis.esc = s => String(s);
globalThis.t = k => k;
globalThis.pysimFsFilterRender = () => { _renders++; };
globalThis.pysimFsRenderTree = () => { _treeRenders++; };

let code = '';
for (const f of ['servicesFeatureByNumber', 'pysimFsFilterBtnHtml', 'pysimFsSetFilterToService',
	'pysimFsFilterPathsInvalidate']) {
	code += extractFunc(html, f) + '\n';
}
for (const c of ['SERVICES_CROSS_REF', 'SERVICES_FS_MAP']) code += extractConst(html, c);
eval(code);

test('servicesFeatureByNumber inverts the cross-reference per file table', () => {
	const map = servicesFeatureByNumber();
	assert.strictEqual(map.ust[2], 'Fixed Dialling Numbers (FDN)');
	assert.strictEqual(map.sst[2], 'Local Phone Book (ADN)');
	assert.strictEqual(map.est[1], 'Fixed Dialling Numbers (FDN)');
	assert.strictEqual(map.ist[1], 'P-CSCF address');
	// the same number names a different feature in another table
	assert.strictEqual(map.ust[1], 'Local Phone Book (ADN)');
	assert.strictEqual(map.sst[1], 'CHV1 disable function');
	// a capability-only number maps to a feature too
	assert.strictEqual(map.ust[99], 'URI support by UICC');
	// the map is built once
	assert.strictEqual(servicesFeatureByNumber(), map);
});

test('pysimFsFilterBtnHtml links a file-backed service row only', () => {
	const out = pysimFsFilterBtnHtml('ust', 2);
	assert.match(out, /^<button type="button" class="ml-1 px-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"/);
	assert.ok(out.indexOf("pysimFsSetFilterToService('ust', 2)") > 0, out);
	assert.ok(out.indexOf('To EF filter') > 0, out);
	// OCI is file-backed
	assert.notStrictEqual(pysimFsFilterBtnHtml('ust', 8), '');
	// a capability-only service, a missing table and an unmodelled number
	assert.strictEqual(pysimFsFilterBtnHtml('ust', 99), '');
	assert.strictEqual(pysimFsFilterBtnHtml(undefined, 1), '');
	assert.strictEqual(pysimFsFilterBtnHtml('sst', 99), '');
});

test('pysimFsSetFilterToService narrows the tree to that service', () => {
	delete _store.simple_fs_filter;
	_renders = 0;
	_treeRenders = 0;
	pysimFsFilter = new Set(['all']);
	pysimFsSetFilterToService('ust', 2);
	assert.deepStrictEqual([...pysimFsFilter], ['svc:Fixed Dialling Numbers (FDN)']);
	assert.strictEqual(_store.simple_fs_filter, JSON.stringify(['svc:Fixed Dialling Numbers (FDN)']));
	assert.strictEqual(_renders, 1);
	assert.strictEqual(_treeRenders, 1);
	// a capability-only service changes nothing
	pysimFsSetFilterToService('ust', 99);
	assert.deepStrictEqual([...pysimFsFilter], ['svc:Fixed Dialling Numbers (FDN)']);
	assert.strictEqual(_renders, 1);
	assert.strictEqual(_treeRenders, 1);
});

test('the four service-table fields carry their table, EF.ACC carries none', () => {
	assert.match(html, /const EF_UST_FIELDS = \[\{[^\n]*table: 'ust'/);
	assert.match(html, /const EF_IST_FIELDS = \[\{[^\n]*table: 'ist'/);
	assert.match(html, /const EF_EST_FIELDS = \[\{[^\n]*table: 'est'/);
	assert.match(html, /const EF_SST_FIELDS = \[\{[\s\S]{0,200}?table: 'sst'/);
	const acc = /const EF_ACC_FIELDS = [^\n]*/.exec(html);
	assert.ok(acc, 'EF_ACC_FIELDS not found');
	assert.ok(acc[0].indexOf('table') < 0, acc[0]);
});

test('both service-grid renderers append the cross-link after their control', () => {
	const fn = extractFunc(html, 'efDecFieldHtml');
	assert.strictEqual((fn.match(/pysimFsFilterBtnHtml\(f\.table, n\)/g) || []).length, 2, fn);
});
