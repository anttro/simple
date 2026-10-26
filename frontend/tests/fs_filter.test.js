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
const _store = {};
let _treeRenders = 0;

globalThis.localStorage = {
	getItem: k => (Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null),
	setItem: (k, v) => { _store[k] = String(v); },
	removeItem: k => { delete _store[k]; },
};
globalThis.esc = s => String(s);
globalThis.t = k => k;
globalThis.pysimFsRenderTree = () => { _treeRenders++; };
let _select = null;
globalThis.document = { getElementById: id => (id === 'pysim-fs-filter' ? _select : null) };

let code = '';
for (const f of ['fsFilterServiceOptions', 'fsFilterIndexBuild', 'fsFilterIndex',
	'pysimFsFilterMatch', 'pysimFsFilterShow', 'pysimFsFilterRender', 'pysimFsSetFilter',
	'pysimFsFilterRestore']) {
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

function fakeSelect(selected) {
	const options = (selected || []).map(v => ({ value: v, selected: true }));
	return {
		_html: '',
		get innerHTML() { return this._html; },
		set innerHTML(v) {
			this._html = v;
			options.length = 0;
			for (const m of v.matchAll(/<option value="([^"]*)"[^>]*>/g)) options.push({ value: m[1], selected: false });
		},
		querySelectorAll() { return options; },
		get selectedOptions() { return options.filter(o => o.selected); },
		options,
	};
}

const fileBackedFeatures = () => Object.keys(SERVICES_FS_MAP).filter(n => SERVICES_FS_MAP[n].length);

test('the file tree has a multiselect filter next to the sort pills', () => {
	assert.match(html, /id="pysim-fs-filter"[^>]*multiple[^>]*onchange="pysimFsSetFilter\(\)"/);
	assert.match(html, /data-l10n="Filter:"/);
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

test('a directory stays while it has a matching descendant', () => {
	const df = children => ({ name: 'DF.GSM', fid: '7F20', isDir: true, children });
	withFilter(['provider-names'], () => {
		// a matching file shows, a non-matching one hides
		assert.strictEqual(pysimFsFilterShow({ name: 'EF.SPN', isDir: false }), true);
		assert.strictEqual(pysimFsFilterShow({ name: 'EF.ECC', isDir: false }), false);
		// a DF with a matching loaded child shows, one without hides
		assert.strictEqual(pysimFsFilterShow(df([{ name: 'EF.ECC', isDir: false }, { name: 'EF.SPN', isDir: false }])), true);
		assert.strictEqual(pysimFsFilterShow(df([{ name: 'EF.ECC', isDir: false }])), false);
		assert.strictEqual(pysimFsFilterShow(df([])), false);
		// a collapsed DF whose children were never loaded is kept
		assert.strictEqual(pysimFsFilterShow({ name: 'DF.GSM', isDir: true, children: null, expanded: false }), true);
		// ... and a matching descendant nested one level deeper counts too
		assert.strictEqual(pysimFsFilterShow(df([
			{ name: 'DF.X', isDir: true, children: [{ name: 'EF.ECC', isDir: false }, { name: 'EF.PNN', isDir: false }] },
		])), true);
		assert.strictEqual(pysimFsFilterShow(df([{ name: 'DF.X', isDir: true, children: null }])), true);
	});
	// a DF that is itself a per-service file (the GPRS feature names DF.GSM)
	withFilter(['svc:GPRS'], () => {
		assert.strictEqual(pysimFsFilterMatch({ name: 'DF.GSM', isDir: true }), true);
	});
	// everything shows with the default
	withFilter(['all'], () => {
		assert.strictEqual(pysimFsFilterShow(df([{ name: 'EF.ECC', isDir: false }])), true);
	});
});

test('selecting "all" clears the categories', () => {
	pysimFsFilter = new Set(['identifiers']);
	_select = fakeSelect(['all', 'identifiers']);
	_treeRenders = 0;
	pysimFsSetFilter();
	assert.deepStrictEqual([...pysimFsFilter], ['all']);
	assert.strictEqual(_store.simple_fs_filter, '["all"]');
	assert.strictEqual(_treeRenders, 1);
});

test('selecting a category drops "all"', () => {
	pysimFsFilter = new Set(['all']);
	_select = fakeSelect(['all', 'sms']);
	pysimFsSetFilter();
	assert.deepStrictEqual([...pysimFsFilter], ['sms']);
	assert.strictEqual(_store.simple_fs_filter, '["sms"]');
});

test('several categories stay selected and an empty selection means all', () => {
	pysimFsFilter = new Set(['all']);
	_select = fakeSelect(['identifiers', 'sms']);
	pysimFsSetFilter();
	assert.deepStrictEqual([...pysimFsFilter], ['identifiers', 'sms']);
	pysimFsFilter = new Set(['identifiers', 'sms']);
	_select = fakeSelect([]);
	pysimFsSetFilter();
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

test('the dropdown renders all, the groups and the services', () => {
	pysimFsFilter = new Set(['all']);
	const el = fakeSelect(['all']);
	_select = el;
	pysimFsFilterRender();
	assert.match(el.innerHTML, /^<option value="all">All<\/option><option value="custom">Custom files<\/option>/);
	assert.match(el.innerHTML, /<optgroup label="Identifiers"><option value="identifiers">Identifiers<\/option><option value="provider-names">Provider names<\/option><\/optgroup>/);
	assert.match(el.innerHTML, /<optgroup label="PLMN"><option value="plmn-selection">PLMN selection<\/option><option value="wlan">WLAN \/ I-WLAN<\/option><\/optgroup>/);
	assert.match(el.innerHTML, /<optgroup label="Services"><option value="sms">SMS-related<\/option><option value="cb">CB-related<\/option><option value="svc:Local Phone Book \(ADN\)">/);
	const values = [...el.innerHTML.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);
	assert.strictEqual(values.length, 2 + FS_FILTER_GROUPS.length + fsFilterServiceOptions().length);
	assert.ok(values.includes('svc:Service Provider Name (SPN)'));
	assert.ok(values.includes('custom'));
	assert.ok(!values.includes('svc:Proactive SIM'));
	assert.strictEqual(el.options.find(o => o.value === 'all').selected, true);
});

test('the dropdown marks the saved selection', () => {
	pysimFsFilter = new Set(['svc:Short Message Storage (SMS)']);
	const el = fakeSelect();
	_select = el;
	pysimFsFilterRender();
	const selected = el.options.filter(o => o.selected).map(o => o.value);
	assert.deepStrictEqual(selected, ['svc:Short Message Storage (SMS)']);
});

test('the render consults the filter when walking children', () => {
	assert.match(html, /if \(!pysimFsFilterShow\(child\)\) continue;/);
	assert.ok(!/pysimFsFilterMatch\(child\)/.test(html));
});

test('the filter state is persisted under simple_fs_filter', () => {
	const fn = extractFunc(html, 'pysimFsSetFilter');
	assert.ok(fn.indexOf("localStorage.setItem('simple_fs_filter'") > 0, fn);
	assert.ok(fn.indexOf('pysimFsRenderTree()') > 0, fn);
});

test('the dropdown is re-rendered on language switch', () => {
	const fn = extractFunc(html, 'refreshDynamicI18n');
	assert.ok(fn.indexOf('pysimFsFilterRender()') > 0, fn);
});

test('the filter labels have Russian entries', () => {
	const dict = html.match(/const LANG_RU = \{([\s\S]*?)\n\};/)[1];
	for (const key of ['All', 'Custom files', 'Identifiers', 'PLMN', 'Services', 'PLMN selection',
		'Provider names', 'WLAN / I-WLAN', 'SMS-related', 'CB-related']) {
		assert.ok(dict.includes("'" + key + "':"), 'missing Russian entry: ' + key);
	}
});
