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

function extractStrConst(src, name) {
	const m = new RegExp("const " + name + " = '([^']*)';").exec(src);
	if (!m) throw new Error('const ' + name + ' not found');
	return 'var ' + name + " = '" + m[1] + "';\n";
}

// module state the extracted functions close over
let pysimFsFilter = new Set(['all']);
let _fsFilterIndex = null;
// The shipped standard list stands in for the fetched one (uicc_files.json is
// what uiccStdFilesLoad caches; the filter reads its entries synchronously).
const stdAsset = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'uicc_files.json'), 'utf8'));
let _uiccStdFiles = stdAsset.files;
let _fsFilterPathIndex = null;
let _fsFilterPathIndexSrc = null;
let _fsFilterPaths = null;
let _fsFilterPathsKey = null;
let pysimCustomFiles = [];
const _store = {};
let _treeRenders = 0;
let pysimFsTreeRoot = null;   // expand-all walks this; null = nothing to expand
let pysimFsLoadChildren = async node => { if (!node.children) node.children = []; };

globalThis.localStorage = {
	getItem: k => (Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null),
	setItem: (k, v) => { _store[k] = String(v); },
	removeItem: k => { delete _store[k]; },
};
globalThis.esc = s => String(s);
globalThis.t = k => k;
globalThis.pysimFsRenderTree = () => { _treeRenders++; };

// The filter's two DOM elements: the collapsed row's button and the hidden
// menu it expands into.  The menu only needs a settable innerHTML (the
// render never reads the DOM back) and a classList for the open/closed state.
function fakeBtn() {
	const classes = new Set();
	return {
		textContent: '',
		attrs: {},
		classes,
		classList: {
			contains: c => classes.has(c),
			toggle(c, force) {
				const on = force === undefined ? !classes.has(c) : !!force;
				if (on) classes.add(c); else classes.delete(c);
				return on;
			},
		},
		setAttribute(k, v) { this.attrs[k] = String(v); },
		getAttribute(k) { return this.attrs[k]; },
	};
}

function fakeMenu() {
	const classes = new Set(['hidden']);
	return {
		_html: '',
		classes,
		classList: {
			contains: c => classes.has(c),
			toggle(c, force) {
				const on = force === undefined ? !classes.has(c) : !!force;
				if (on) classes.add(c); else classes.delete(c);
				return on;
			},
		},
		get innerHTML() { return this._html; },
		set innerHTML(v) { this._html = v; },
	};
}

const dom = { menu: null, btn: null };
globalThis.document = {
	getElementById: id => (id === 'pysim-fs-filter-menu' ? dom.menu
		: (id === 'pysim-fs-filter-btn' ? dom.btn : null)),
};

let code = '';
for (const f of ['fsFilterServiceOptions', 'fsFilterIndexBuild', 'fsFilterIndex',
	'fsFilterPathIndexBuild', 'fsFilterPathIndex', 'fsFilterCustomPaths',
	'fsFilterPathsKeyOf', 'pysimFsFilterPathsInvalidate', 'pysimFsFilterMatchingPaths',
	'pysimFsFilterMatch', 'pysimFsFilterShow', 'pysimFsFilterLabel',
	'pysimFsFilterSummary', 'pysimFsFilterRender', 'pysimFsFilterToggle',
	'pysimFsFilterSetEntry', 'pysimFsFilterRestore', 'pysimFsNodePath',
	'pysimFsExpandAll', 'pysimFsCollapseAll', 'pysimFsAutoExpandForFilter']) {
	code += extractFunc(html, f) + '\n';
}
for (const c of ['FS_FILTER_GROUPS', 'FS_FILTER_GROUP_ORDER', 'SERVICES_FS_MAP']) code += extractConst(html, c);
code += extractStrConst(html, 'FS_FILTER_ALL');
code += extractStrConst(html, 'FS_FILTER_CUSTOM');
eval(code);

function withFilter(ids, fn) {
	const saved = pysimFsFilter;
	pysimFsFilter = new Set(ids);
	try { return fn(); } finally { pysimFsFilter = saved; }
}

// The checkbox rows the render wrote: [{id, checked}] in document order.
function renderedBoxes() {
	const out = [];
	for (const m of (dom.menu ? dom.menu.innerHTML : '').matchAll(
		/<input type="checkbox" data-filter="([^"]*)"( checked)?/g)) {
		out.push({ id: m[1], checked: !!m[2] });
	}
	return out;
}

const box = id => renderedBoxes().find(b => b.id === id);

const fileBackedFeatures = () => Object.keys(SERVICES_FS_MAP).filter(n => SERVICES_FS_MAP[n].length);

test('the file tree has a collapsed filter dropdown next to the sort pills', () => {
	assert.match(html, /id="pysim-fs-filter-btn"[^>]*aria-haspopup="true"[^>]*aria-expanded="false"[^>]*onclick="pysimFsFilterToggle\(\)"/);
	assert.match(html, /id="pysim-fs-filter-menu"[^>]*role="group"[^>]*class="hidden absolute/);
	assert.match(html, /data-l10n="Filter:"/);
	// the multiselect (and its onchange handler) is gone
	assert.ok(!/id="pysim-fs-filter"/.test(html));
	assert.ok(!/pysimFsSetFilter\(/.test(html));
});

test('an outside click or Escape closes the menu', () => {
	const click = html.match(/document\.addEventListener\('click', e => \{([\s\S]{0,600}?)\n\}\);/);
	assert.ok(click, 'no document click listener');
	assert.match(click[1], /#pysim-fs-filter-btn/);
	assert.match(click[1], /#pysim-fs-filter-menu/);
	assert.match(click[1], /pysimFsFilterToggle\(false\)/);
	const key = html.match(/document\.addEventListener\('keydown', e => \{([\s\S]{0,300}?)\n\}\);/);
	assert.ok(key, 'no document keydown listener');
	assert.match(key[1], /Escape/);
	assert.match(key[1], /pysimFsFilterToggle\(false\)/);
});

test('the six category groups name their files', () => {
	assert.deepStrictEqual(FS_FILTER_GROUPS.map(g => g.id),
		['identifiers', 'plmn-selection', 'provider-names', 'wlan', 'sms', 'cb']);
	const files = id => new Set(FS_FILTER_GROUPS.find(g => g.id === id).files);
	assert.deepStrictEqual([...files('identifiers')],
		['EF.ICCID', 'EF.IMSI', 'EF.MSISDN', 'EF.IMPI', 'EF.IMPU', 'EF.SUPI_NAI', 'EF.GID1', 'EF.GID2']);
	assert.deepStrictEqual([...files('plmn-selection')],
		['EF.PLMNsel', 'EF.PLMNwAcT', 'EF.OPLMNwAcT', 'EF.HPLMNwAcT', 'EF.EHPLMN', 'EF.FPLMN',
			'EF.HPPLMN', 'EF.LRPLMNSI', 'EF.EHPLMNPI', 'EF.MCHPPLMN']);
	assert.deepStrictEqual([...files('provider-names')],
		['EF.SPN', 'EF.PNN', 'EF.OPL', 'EF.OPL5G', 'EF.SPDI', 'EF.SPNI', 'EF.PNNI', 'EF.CNL']);
	assert.deepStrictEqual([...files('wlan')],
		['EF.UPLMNWLAN', 'EF.OPLMNWLAN', 'EF.WRI', 'EF.HWSIDL', 'EF.WEHPLMNPI', 'EF.WHPI',
			'EF.WLRPLMN', 'EF.HPLMNDAI']);
	assert.deepStrictEqual([...files('sms')], ['EF.SMS', 'EF.SMSS', 'EF.SMSP', 'EF.SMSR', 'EF.PSISMSC']);
	assert.deepStrictEqual([...files('cb')], ['EF.CBMI', 'EF.CBMID', 'EF.CBMIR']);
	assert.strictEqual(FS_FILTER_ALL, 'all');
	assert.strictEqual(FS_FILTER_CUSTOM, 'custom');
});

test('the per-service options come from the file-backed features only', () => {
	const services = fsFilterServiceOptions();
	const features = fileBackedFeatures();
	assert.ok(features.length > 50, 'expected the cross-reference to be available');
	assert.strictEqual(services.length, features.length);
	assert.ok(services.every(o => o.id === 'svc:' + o.label));
	const spn = services.find(o => o.id === 'svc:Service Provider Name (SPN)');
	assert.deepStrictEqual(spn.files, ['EF.SPN']);
	// capability/RFU rows carry no files, so they are not offered
	assert.ok(!services.some(o => o.id === 'svc:Proactive SIM' || o.id === 'svc:CHV1 disable function'));
});

test('the inverted index keys the uppercased names and merges memberships', () => {
	const idx = fsFilterIndexBuild(FS_FILTER_GROUPS, fsFilterServiceOptions());
	const ids = name => Array.from(idx.get(name.toUpperCase()) || []);
	assert.ok(ids('EF.ICCID').includes('identifiers'));
	assert.ok(ids('EF.PLMNwAcT').includes('plmn-selection'));
	assert.ok(ids('EF.SPN').includes('provider-names'));
	assert.ok(ids('EF.SPN').includes('svc:Service Provider Name (SPN)'));
	assert.ok(ids('EF.SMS').includes('sms'));
	assert.ok(ids('EF.SMS').includes('svc:Short Message Storage (SMS)'));
	assert.ok(ids('EF.CBMI').includes('cb'));
	// EF.PSISMSC is only listed by the SMS category - pySim models no such
	// file, so it never matches a tree node (no service lists it either)
	assert.deepStrictEqual([...idx.get('EF.PSISMSC')], ['sms']);
	assert.ok(!fileBackedFeatures().some(n => (SERVICES_FS_MAP[n] || []).includes('EF.PSISMSC')));
});

test('a file matches the selected categories, case-insensitively', () => {
	withFilter(['identifiers'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ICCID', fid: '2FE2' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'ef.iccid', fid: '2FE2' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.SPN', fid: '6F46' }), false);
		assert.strictEqual(pysimFsFilterMatch({ name: 'DF.GSM', fid: '7F20', isDir: true }), false);
	});
	withFilter(['svc:Service Provider Name (SPN)'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.SPN' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.PNN' }), false);
	});
	withFilter(['identifiers', 'sms'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.IMSI' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.SMSR' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.SPN' }), false);
	});
	withFilter(['sms'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ECC' }), false);
	});
});

test('an empty filter and "all" show everything', () => {
	withFilter([], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ADN' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'DF.GSM', isDir: true }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: undefined }), true);
	});
	withFilter(['all'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ADN' }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ZZZ' }), true);
	});
	// a stale multi-selection that still names all also shows everything
	withFilter(['all', 'identifiers'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ECC' }), true);
	});
});

test('a capability-only service selection matches nothing', () => {
	withFilter(['svc:Proactive SIM'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ADN' }), false);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.UST' }), false);
	});
});

test('"custom files" matches only the injected custom nodes', () => {
	withFilter(['custom'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ADN', custom: false }), false);
		assert.strictEqual(pysimFsFilterMatch({ name: 'MyCustomEF', custom: true }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ICCID', custom: true }), true);
	});
	withFilter(['custom', 'identifiers'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ICCID', custom: false }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'MyCustomEF', custom: true }), true);
		assert.strictEqual(pysimFsFilterMatch({ name: 'EF.SPN', custom: false }), false);
	});
});

// A small tree with real parent links (pysimFsNodePath walks them): MF with
// DF.TELECOM (EF.MSISDN), DF.GSM (EF.SPN, EF.ECC) and ADF.USIM (DF.WLAN).
function fsTree() {
	const root = { name: 'MF', fid: '3F00', isDir: true, children: [], parent: null, expanded: true };
	const add = (parent, name, fid, isDir) => {
		const n = { name, fid, isDir: !!isDir, children: isDir ? [] : null, parent, expanded: true };
		parent.children.push(n);
		return n;
	};
	const telecom = add(root, 'DF.TELECOM', '7F10', true);
	const msisdn = add(telecom, 'EF.MSISDN', '6F40', false);
	const gsm = add(root, 'DF.GSM', '7F20', true);
	const spn = add(gsm, 'EF.SPN', '6F46', false);
	const ecc = add(gsm, 'EF.ECC', '6FB7', false);
	const usim = add(root, 'ADF.USIM', '7FFF', true);
	const wlan = add(usim, 'DF.WLAN', '5F40', true);
	return { root, telecom, msisdn, gsm, spn, ecc, usim, wlan };
}

test('a directory shows only on the path of a file the filter selects', () => {
	const t = fsTree();
	withFilter(['provider-names'], () => {
		// the root always stays; DF.GSM holds EF.SPN (MF/7F20/6F46)
		assert.strictEqual(pysimFsFilterShow(t.root), true);
		assert.strictEqual(pysimFsFilterShow(t.gsm), true);
		// ... and ADF.USIM holds its own EF.SPN copy
		assert.strictEqual(pysimFsFilterShow(t.usim), true);
		// neither DF.TELECOM nor DF.WLAN holds a provider-name file
		assert.strictEqual(pysimFsFilterShow(t.telecom), false);
		assert.strictEqual(pysimFsFilterShow(t.wlan), false);
		// files keep their name rule
		assert.strictEqual(pysimFsFilterShow(t.spn), true);
		assert.strictEqual(pysimFsFilterShow(t.ecc), false);
	});
	withFilter(['identifiers'], () => {
		// EF.MSISDN is in DF.TELECOM, EF.IMSI/EF.GID1/2 in DF.GSM
		assert.strictEqual(pysimFsFilterShow(t.telecom), true);
		assert.strictEqual(pysimFsFilterShow(t.msisdn), true);
		assert.strictEqual(pysimFsFilterShow(t.gsm), true);
		assert.strictEqual(pysimFsFilterShow(t.usim), true);   // EF.IMSI/EF.MSISDN copies
		assert.strictEqual(pysimFsFilterShow(t.wlan), false);
	});
	withFilter(['cb'], () => {
		assert.strictEqual(pysimFsFilterShow(t.gsm), true);      // EF.CBMI/CBMID/CBMIR
		assert.strictEqual(pysimFsFilterShow(t.telecom), false);
		assert.strictEqual(pysimFsFilterShow(t.wlan), false);
	});
});

test('a directory named by the selection itself shows', () => {
	const t = fsTree();
	// the GPRS feature names DF.GSM (its SST bit covers the DF, not a file)
	withFilter(['svc:GPRS'], () => {
		assert.strictEqual(pysimFsFilterMatch(t.gsm), true);
		assert.strictEqual(pysimFsFilterShow(t.gsm), true);
		assert.strictEqual(pysimFsFilterShow(t.telecom), false);
	});
});

test('"all" and an empty selection show every node', () => {
	const t = fsTree();
	for (const ids of [['all'], []]) {
		withFilter(ids, () => {
			assert.strictEqual(pysimFsFilterShow(t.root), true);
			assert.strictEqual(pysimFsFilterShow(t.telecom), true);
			assert.strictEqual(pysimFsFilterShow(t.wlan), true);
			assert.strictEqual(pysimFsFilterShow(t.ecc), true);
		});
	}
});

test('directories are kept until the standard list is available', () => {
	const t = fsTree();
	const saved = _uiccStdFiles;
	_uiccStdFiles = null;
	try {
		withFilter(['provider-names'], () => {
			assert.strictEqual(pysimFsFilterMatchingPaths(), null);
			// the ancestors cannot be judged, so nothing vanishes
			assert.strictEqual(pysimFsFilterShow(t.telecom), true);
			assert.strictEqual(pysimFsFilterShow(t.wlan), true);
			// files still obey the name rule
			assert.strictEqual(pysimFsFilterShow(t.ecc), false);
		});
	} finally {
		_uiccStdFiles = saved;
	}
});

test('the standard list gives a name every tree it lives in', () => {
	const idx = fsFilterPathIndexBuild(_uiccStdFiles);
	assert.deepStrictEqual(idx.get('EF.SPN').slice().sort(),
		['ADF.USIM-NON-IMSI/6F46', 'ADF.USIM/6F46', 'MF/7F20/6F46']);
	assert.deepStrictEqual(idx.get('EF.ICCID'), ['MF/2FE2']);
	// the index is cached for the loaded list
	assert.strictEqual(fsFilterPathIndex(), fsFilterPathIndex());
});

test('the selection resolves to the paths of its files', () => {
	withFilter(['cb'], () => {
		const paths = pysimFsFilterMatchingPaths();
		assert.ok(paths instanceof Set, 'expected a Set');
		assert.deepStrictEqual(Array.from(paths).sort(), [
			'ADF.USIM-NON-IMSI/6F45', 'ADF.USIM-NON-IMSI/6F48', 'ADF.USIM-NON-IMSI/6F50',
			'ADF.USIM/6F45', 'ADF.USIM/6F48', 'ADF.USIM/6F50',
			'MF/7F20/6F45', 'MF/7F20/6F48', 'MF/7F20/6F50',
		]);
		// cached for the same selection
		assert.strictEqual(pysimFsFilterMatchingPaths(), paths);
	});
	withFilter(['custom'], () => {
		assert.deepStrictEqual(Array.from(pysimFsFilterMatchingPaths()), []);
	});
});

test('"custom files" keeps the directories that hold them', () => {
	const t = fsTree();
	const custom = { name: 'EF.MYFILE', fid: '6F99', isDir: false, children: null, parent: t.gsm, custom: true };
	t.gsm.children.push(custom);
	pysimCustomFiles = [{ path: 'MF/7F20/6F99', name: 'EF.MYFILE', kind: 'ef' }];
	try {
		withFilter(['custom'], () => {
			// custom files are outside the standard list: their own paths are
			// the ancestry, so the holding DF stays
			assert.strictEqual(pysimFsFilterShow(custom), true);
			assert.strictEqual(pysimFsFilterShow(t.gsm), true);
			assert.strictEqual(pysimFsFilterShow(t.root), true);
			assert.strictEqual(pysimFsFilterShow(t.telecom), false);
		});
	} finally {
		pysimCustomFiles = [];
	}
});

test('the menu lists all, custom, the three headings and the services', () => {
	pysimFsFilter = new Set(['all']);
	dom.menu = fakeMenu();
	dom.btn = fakeBtn();
	pysimFsFilterRender();
	const out = dom.menu.innerHTML;
	assert.match(out, /^<label[^>]*><input type="checkbox" data-filter="all" checked/);
	assert.match(out, /data-filter="custom"/);
	assert.match(out, /<div class="[^"]*">Identifiers<\/div>/);
	assert.match(out, /<div class="[^"]*">PLMN<\/div>/);
	assert.match(out, /<div class="[^"]*">Services<\/div>/);
	// the headings come in the declared order
	assert.ok(out.indexOf('>Identifiers</div>') < out.indexOf('>PLMN</div>'));
	assert.ok(out.indexOf('>PLMN</div>') < out.indexOf('>Services</div>'));
	// the categories sit under their headings
	assert.ok(out.indexOf('data-filter="provider-names"') < out.indexOf('>PLMN</div>'));
	assert.ok(out.indexOf('data-filter="plmn-selection"') > out.indexOf('>PLMN</div>'));
	assert.ok(out.indexOf('data-filter="sms"') > out.indexOf('>Services</div>'));
	const boxes = renderedBoxes();
	assert.strictEqual(boxes.length, 2 + FS_FILTER_GROUPS.length + fsFilterServiceOptions().length);
	assert.strictEqual(boxes[0].id, 'all');
	assert.strictEqual(boxes[0].checked, true);
	assert.strictEqual(box('custom').checked, false);
	assert.ok(boxes.some(b => b.id === 'svc:Service Provider Name (SPN)'));
	assert.ok(!boxes.some(b => b.id === 'svc:Proactive SIM'));
	// the row shows the summary, and the checkbox calls the setter directly
	assert.strictEqual(dom.btn.textContent, 'All');
	assert.match(out, /onchange="pysimFsFilterSetEntry\(this\.dataset\.filter, this\.checked\)"/);
});

test('the button turns blue when a filter is active and back for all', () => {
	dom.menu = fakeMenu();
	dom.btn = fakeBtn();
	pysimFsFilter = new Set(['identifiers']);
	pysimFsFilterRender();
	assert.strictEqual(dom.btn.classes.has('bg-blue-600'), true);
	assert.strictEqual(dom.btn.classes.has('text-white'), true);
	assert.strictEqual(dom.btn.classes.has('text-gray-700'), false);
	pysimFsFilter = new Set(['all']);
	pysimFsFilterRender();
	assert.strictEqual(dom.btn.classes.has('bg-blue-600'), false);
	assert.strictEqual(dom.btn.classes.has('text-white'), false);
	assert.strictEqual(dom.btn.classes.has('text-gray-700'), true);
});

test('expand all and collapse all walk the directory tree, skipping absent DFs', async () => {
	const telecom = { name: 'DF.TELECOM', fid: '7F10', isDir: true, exists: true, expanded: false,
		parent: null, children: [
			{ name: 'DF.PHONEBOOK', fid: '5F3A', isDir: true, exists: true, expanded: false, parent: null, children: null },
		] };
	const gsm = { name: 'DF.GSM', fid: '7F20', isDir: true, exists: false, expanded: false, parent: null, children: null };
	pysimFsTreeRoot = { name: 'MF', fid: '3F00', isDir: true, exists: true, expanded: true, parent: null,
		children: [gsm, telecom] };
	let loads = 0;
	pysimFsLoadChildren = async node => { if (!node.children) { node.children = []; } loads++; };
	try {
		_treeRenders = 0;
		await pysimFsExpandAll();
		assert.strictEqual(pysimFsTreeRoot.expanded, true);
		assert.strictEqual(gsm.expanded, false, 'an absent DF is not expanded');
		assert.strictEqual(telecom.expanded, true);
		assert.strictEqual(telecom.children[0].expanded, true);
		assert.ok(loads > 0, 'lazy children are loaded on demand');
		assert.strictEqual(_treeRenders, 1);

		pysimFsCollapseAll();
		assert.strictEqual(pysimFsTreeRoot.expanded, false);
		assert.strictEqual(telecom.expanded, false);
		assert.strictEqual(telecom.children[0].expanded, false);
	} finally {
		pysimFsTreeRoot = null;
	}
});

test('auto-expand fires only for a non-"all" filter', () => {
	// expand-all (with a null tree) still re-renders once; pin the branch
	pysimFsFilter = new Set(['all']);
	_treeRenders = 0;
	pysimFsAutoExpandForFilter();
	assert.strictEqual(_treeRenders, 0, '"all" does not expand');
	pysimFsFilter = new Set(['identifiers']);
	pysimFsAutoExpandForFilter();
	assert.strictEqual(_treeRenders, 1, 'a category expands the tree');
});

test('the menu marks the saved selection', () => {
	pysimFsFilter = new Set(['svc:Short Message Storage (SMS)', 'identifiers']);
	dom.menu = fakeMenu();
	dom.btn = fakeBtn();
	pysimFsFilterRender();
	assert.deepStrictEqual(renderedBoxes().filter(b => b.checked).map(b => b.id),
		['identifiers', 'svc:Short Message Storage (SMS)']);
	assert.strictEqual(box('all').checked, false);
	assert.strictEqual(dom.btn.textContent, 'Selected: 2');
});

test('the button summary names the single entry or counts the selection', () => {
	const summary = ids => withFilter(ids, () => pysimFsFilterSummary());
	assert.strictEqual(summary(['all']), 'All');
	// an empty selection also means everything
	assert.strictEqual(summary([]), 'All');
	assert.strictEqual(summary(['all', 'sms']), 'All');
	assert.strictEqual(summary(['custom']), 'Custom files');
	assert.strictEqual(summary(['plmn-selection']), 'PLMN selection');
	assert.strictEqual(summary(['svc:Service Provider Name (SPN)']), 'Service Provider Name (SPN)');
	assert.strictEqual(summary(['sms', 'cb']), 'Selected: 2');
	// an unknown id falls back to itself
	assert.strictEqual(summary(['providers']), 'providers');
});

test('the menu toggles open and closed', () => {
	dom.menu = fakeMenu();
	dom.btn = fakeBtn();
	assert.strictEqual(dom.menu.classList.contains('hidden'), true);
	pysimFsFilterToggle();
	assert.strictEqual(dom.menu.classList.contains('hidden'), false);
	assert.strictEqual(dom.btn.getAttribute('aria-expanded'), 'true');
	pysimFsFilterToggle();
	assert.strictEqual(dom.menu.classList.contains('hidden'), true);
	assert.strictEqual(dom.btn.getAttribute('aria-expanded'), 'false');
	// the forced forms (the outside click / Escape handlers pass false)
	pysimFsFilterToggle(true);
	assert.strictEqual(dom.menu.classList.contains('hidden'), false);
	pysimFsFilterToggle(false);
	assert.strictEqual(dom.menu.classList.contains('hidden'), true);
});

test('the checkbox rows keep "all" exclusive and persist the selection', () => {
	dom.menu = fakeMenu();
	dom.btn = fakeBtn();
	delete _store.simple_fs_filter;
	_treeRenders = 0;
	pysimFsFilter = new Set(['all']);
	pysimFsFilterSetEntry('all', true);
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
	assert.strictEqual(_store.simple_fs_filter, '["all"]');
	assert.strictEqual(_treeRenders, 1);
	// choosing a category drops "all"
	pysimFsFilterSetEntry('sms', true);
	assert.deepStrictEqual([...pysimFsFilter], ['sms']);
	assert.strictEqual(_store.simple_fs_filter, '["sms"]');
	assert.strictEqual(_treeRenders, 3);   // render + the auto-expand's render
	assert.strictEqual(box('sms').checked, true);
	assert.strictEqual(box('all').checked, false);
	assert.strictEqual(dom.btn.textContent, 'SMS-related');
	// several categories stay selected
	pysimFsFilterSetEntry('identifiers', true);
	assert.deepStrictEqual([...pysimFsFilter], ['sms', 'identifiers']);
	// unchecking one keeps the other
	pysimFsFilterSetEntry('sms', false);
	assert.deepStrictEqual([...pysimFsFilter], ['identifiers']);
	// unchecking the last one restores the default
	pysimFsFilterSetEntry('identifiers', false);
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
	assert.strictEqual(_store.simple_fs_filter, '["all"]');
	assert.strictEqual(box('all').checked, true);
	// "all" is exclusive even when another entry is selected
	pysimFsFilter = new Set(['sms']);
	pysimFsFilterSetEntry('all', true);
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
});

test('the saved selection is restored, defaulting to "all"', () => {
	delete _store.simple_fs_filter;
	pysimFsFilterRestore();
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
	_store.simple_fs_filter = JSON.stringify(['providers', 'svc:Service Provider Name (SPN)']);
	pysimFsFilterRestore();
	assert.deepStrictEqual([...pysimFsFilter], ['providers', 'svc:Service Provider Name (SPN)']);
	// `all` is exclusive; an empty or broken save falls back to it
	_store.simple_fs_filter = JSON.stringify(['all', 'sms']);
	pysimFsFilterRestore();
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
	_store.simple_fs_filter = '[]';
	pysimFsFilterRestore();
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
	_store.simple_fs_filter = '{not json';
	pysimFsFilterRestore();
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
});

test('the render consults the filter when walking children', () => {
	assert.match(html, /if \(!pysimFsFilterShow\(child\)\) continue;/);
	assert.ok(!/pysimFsFilterMatch\(child\)/.test(html));
});

test('the directory rule uses the registry paths, not the loaded children', () => {
	const fn = extractFunc(html, 'pysimFsFilterShow');
	assert.ok(!fn.includes('node.children'), fn);
	assert.ok(fn.indexOf('pysimFsFilterMatchingPaths()') > 0, fn);
	assert.ok(fn.indexOf("path === 'MF'") > 0, fn);
	// the standard list is read synchronously from the cached array
	assert.match(html, /let _uiccStdFiles = null;/);
	assert.ok(extractFunc(html, 'pysimFsFilterMatchingPaths').indexOf('_uiccStdFiles') > 0);
});

test('every filter change drops the path cache before the tree redraws', () => {
	for (const name of ['pysimFsFilterSetEntry', 'pysimFsSetFilterToService', 'pysimFsFilterRestore']) {
		const fn = extractFunc(html, name);
		const at = fn.indexOf('pysimFsFilterPathsInvalidate()');
		assert.ok(at > 0, name + ' does not invalidate the path cache');
		const render = fn.indexOf('pysimFsRenderTree()');
		if (render > 0) assert.ok(at < render, name + ': the cache is dropped before the redraw');
	}
	// the list arriving redraws a tree that was filtered while it loaded
	const load = extractFunc(html, 'uiccStdFilesLoad');
	assert.ok(load.indexOf('pysimFsRenderTree()') > 0, load);
});

test('the filter state is persisted under simple_fs_filter', () => {
	const fn = extractFunc(html, 'pysimFsFilterSetEntry');
	assert.ok(fn.indexOf("localStorage.setItem('simple_fs_filter'") > 0, fn);
	assert.ok(fn.indexOf('pysimFsRenderTree()') > 0, fn);
	assert.ok(fn.indexOf('pysimFsFilterRender()') > 0, fn);
});

test('the dropdown is re-rendered on language switch', () => {
	const fn = extractFunc(html, 'refreshDynamicI18n');
	assert.ok(fn.indexOf('pysimFsFilterRender()') > 0, fn);
});

test('the filter labels have Russian entries', () => {
	const dict = html.match(/const LANG_RU = \{([\s\S]*?)\n\};/)[1];
	for (const key of ['All', 'Custom files', 'Identifiers', 'PLMN', 'Services', 'PLMN selection',
		'Provider names', 'WLAN / I-WLAN', 'SMS-related', 'CB-related', 'Selected: {n}']) {
		assert.ok(dict.includes("'" + key + "':"), 'missing Russian entry: ' + key);
	}
});
