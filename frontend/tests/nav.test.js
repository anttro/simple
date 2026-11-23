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

// The pure state helpers (they only read the NAV_FIELDS constant).
eval(html.match(/const NAV_FIELDS = \[[\s\S]*?\];/)[0].replace('const ', 'var '));
eval(extractFunc(html, 'navStateKey'));

test('navStateKey canonicalises the view state', () => {
	assert.strictEqual(navStateKey(null), navStateKey({}));
	assert.strictEqual(navStateKey({ tab: 'cards' }),
		navStateKey({ tab: 'cards', preset: '', view: '' }));
	assert.notStrictEqual(navStateKey({ tab: 'cards', view: 'list' }),
		navStateKey({ tab: 'cards', view: 'edit' }));
	// the field order does not matter
	assert.strictEqual(navStateKey({ tab: 'a', suite: 'x' }),
		navStateKey({ suite: 'x', tab: 'a' }));
	// every field participates in the identity
	const base = { tab: 'phone', sub: 'test', view: 'script', suite: 's', script: 'c' };
	const baseKey = navStateKey(base);
	for (const f of ['tab', 'sub', 'parser', 'view', 'listtab', 'preset', 'suite', 'script', 'item']) {
		const st = Object.assign({}, base);
		st[f] = 'other';
		assert.notStrictEqual(navStateKey(st), baseKey, f + ' must change the key');
	}
});

test('navCurrent builds the state from the view variables', () => {
	// the module view variables + the helpers it calls, stubbed
	eval(`var _activeTab = 'pysim', _scp80Sub = 'sp', _capduSub = 'sim', _parserSub = 'parse',
		_phoneSub = 'phone', _scp81Sub = 'listener', _pysimSub = 'chv', _filesSub = 'files',
		_cardsView = 'list', _cardsEditId = null, _testView = 'suites', _testScriptId = null,
		_navSuite = null, profilerView = 'list',
		profilerEditId = null, snapshotViewId = null;
		function testCurrentSuite() { return _navSuite; }`);
	eval(extractFunc(html, 'filesProfilerPill'));
	eval(extractFunc(html, 'navCurrent'));
	// the landing view
	assert.deepStrictEqual(navCurrent(), { tab: 'pysim', sub: 'chv' });
	_activeTab = 'scp80'; _scp80Sub = 'ram';
	assert.deepStrictEqual(navCurrent(), { tab: 'scp80', sub: 'ram' });
	_activeTab = 'c-apdu'; _capduSub = 'parser'; _parserSub = 'response';
	assert.deepStrictEqual(navCurrent(), { tab: 'c-apdu', sub: 'parser', parser: 'response' });
	_activeTab = 'phone'; _phoneSub = 'test'; _testView = 'script';
	_navSuite = { id: 'S' }; _testScriptId = 'C';
	assert.deepStrictEqual(navCurrent(),
		{ tab: 'phone', sub: 'test', view: 'script', suite: 'S', script: 'C' });
	_activeTab = 'pysim'; _pysimSub = 'apdu';
	assert.deepStrictEqual(navCurrent(), { tab: 'pysim', sub: 'apdu' });
	// the plain Files pills carry the sub only
	_activeTab = 'files'; _filesSub = 'custom'; profilerView = 'editor';
	assert.deepStrictEqual(navCurrent(), { tab: 'files', sub: 'custom' });
	// a profiler pill carries the overlay and, for the two item views, its id
	_filesSub = 'profiles'; profilerView = 'list';
	assert.deepStrictEqual(navCurrent(), { tab: 'files', sub: 'profiles', view: 'list' });
	profilerView = 'editor'; profilerEditId = 'P';
	assert.deepStrictEqual(navCurrent(),
		{ tab: 'files', sub: 'profiles', view: 'editor', item: 'P' });
	profilerEditId = null;
	assert.deepStrictEqual(navCurrent(), { tab: 'files', sub: 'profiles', view: 'editor' });
	_filesSub = 'snapshots'; profilerView = 'snapshot'; snapshotViewId = 'N';
	assert.deepStrictEqual(navCurrent(),
		{ tab: 'files', sub: 'snapshots', view: 'snapshot', item: 'N' });
	// the results view has no item
	profilerView = 'results';
	assert.deepStrictEqual(navCurrent(),
		{ tab: 'files', sub: 'snapshots', view: 'results' });
	_activeTab = 'cards'; _cardsView = 'edit'; _cardsEditId = 'P';
	assert.deepStrictEqual(navCurrent(), { tab: 'cards', view: 'edit', preset: 'P' });
	// a new-preset editor carries no id
	_cardsEditId = null;
	assert.deepStrictEqual(navCurrent(), { tab: 'cards', view: 'edit' });
});

test('navApply replays the Files state and folds a legacy Profiler tab', () => {
	// navApply runs against stubs: the switch functions record, the router's
	// own bookkeeping variables are provided.
	function runApply(state) {
		const calls = [];
		const stub = 'var _navApplying = false; var _navState = null; var _filesSub = "files";\n'
			+ 'function switchTab(n) { __c("switchTab:" + n); }\n'
			+ 'function scp80SwitchSubtab(n) { __c("scp80:" + n); }\n'
			+ 'function cApduSwitchSubtab(n) { __c("capdu:" + n); }\n'
			+ 'function parserSwitchSubtab(n) { __c("parser:" + n); }\n'
			+ 'function phoneSwitchSubtab(n) { __c("phone:" + n); }\n'
			+ 'function scp81SwitchSubtab(n) { __c("scp81:" + n); }\n'
			+ 'function pysimSwitchSubtab(n) { __c("pysim:" + n); }\n'
			+ 'function filesSwitchSubtab(n) { _filesSub = n; __c("files:" + n); }\n'
			+ 'function navApplyTest(st) { __c("test"); }\n'
			+ 'function navApplyCards(st) { __c("cards"); }\n'
			+ 'function navApplyProfiler(st) { __c("profiler:" + (st.view || "list") + ":" + (st.item || "")); }\n'
			+ 'function navCurrent() { return { tab: "seed" }; }\n'
			+ extractFunc(html, 'filesProfilerPill')
			+ '\n' + extractFunc(html, 'navApply')
			+ '\nnavApply(__state);\n';
		const fn = new Function('__state', '__c', stub);
		fn(state, name => calls.push(name));
		return calls;
	}
	// a Files state replays the pill, then the profiler overlay
	assert.deepStrictEqual(runApply({ tab: 'files', sub: 'profiles', view: 'editor', item: 'P' }),
		['switchTab:files', 'files:profiles', 'profiler:editor:P']);
	assert.deepStrictEqual(runApply({ tab: 'files', sub: 'files' }),
		['switchTab:files', 'files:files']);
	assert.deepStrictEqual(runApply({ tab: 'files', sub: 'snapshots', view: 'results' }),
		['switchTab:files', 'files:snapshots', 'profiler:results:']);
	// a state recorded before 4.4.0 names the Profiler tab: it is a Files state
	// now, the legacy listtab naming the pill
	assert.deepStrictEqual(runApply({ tab: 'profiler', view: 'editor', item: 'P',
		listtab: 'snapshots' }),
		['switchTab:files', 'files:snapshots', 'profiler:editor:P']);
	assert.deepStrictEqual(runApply({ tab: 'profiler', view: 'list', listtab: 'profiles' }),
		['switchTab:files', 'files:profiles', 'profiler:list:']);
	assert.deepStrictEqual(runApply({ tab: 'profiler', view: 'list', listtab: 'custom' }),
		['switchTab:files', 'files:profiles', 'profiler:list:']);
	// the landing state and a missing tab
	assert.deepStrictEqual(runApply({ tab: 'pysim', sub: 'chv' }),
		['switchTab:pysim', 'pysim:chv']);
	assert.deepStrictEqual(runApply({}), ['switchTab:pysim']);
	// the fallback after a throwing replay is the landing tab
	const apply = extractFunc(html, 'navApply');
	assert.match(apply, /switchTab\('pysim'\)/);
});

test('a stale Card reader sub-tab replay folds onto the PIN / ADM pill', () => {
	// A recorded state from an older layout names a sub-panel that no longer
	// exists (files / services / cmd): it must show the default pill (PIN /
	// ADM since v4.3.1) instead of leaving the tab blank.
	const consts = (name) => {
		const from = html.indexOf('const ' + name);
		assert.ok(from >= 0, 'const ' + name + ' not found');
		const end = html.indexOf(';', from);
		return 'var ' + html.slice(from + 'const '.length, end) + ';\n';
	};
	const run = (name) => {
		const panels = {};
		const panel = id => {
			if (!panels[id]) {
				const toggled = [];
				panels[id] = { toggled,
					classList: { toggle: (c, on) => toggled.push([c, on]) } };
			}
			return panels[id];
		};
		globalThis.document = {
			querySelectorAll: () => [],
			getElementById: id => ((id === 'pysim-sub-apdu' || id === 'pysim-sub-chv')
				? panel(id) : null),
		};
		let help = null, recorded = 0;
		const stub = 'var _pysimSub = null; var pysimHelpAnchor = null;\n'
			+ 'function setHelpAnchor(a) { __setHelp(a); }\n'
			+ 'function navRecord() { __recorded(); }\n'
			+ 'function pysimChvRender() { __chvRender(); }\n'
			+ consts('PYSIM_SUB_PANELS') + consts('PYSIM_SUB_HELP')
			+ extractFunc(html, 'pysimSwitchSubtab')
			+ '\npysimSwitchSubtab(' + JSON.stringify(name) + ');\n'
			+ 'return { sub: _pysimSub, help: pysimHelpAnchor };';
		const out = new Function('__setHelp', '__recorded', '__chvRender', stub)(
			a => { help = a; }, () => { recorded++; }, () => { recorded += 100; });
		return { out, panels, help, recorded };
	};

	for (const stale of ['files', 'services', 'cmd']) {
		const { out, panels } = run(stale);
		assert.strictEqual(out.sub, 'chv', stale + ' folds onto the PIN / ADM pill');
		assert.strictEqual(out.help, 'pin-adm');
		assert.deepStrictEqual(panels['pysim-sub-chv'].toggled, [['hidden', false]],
			stale + ' shows the PIN / ADM panel');
		assert.deepStrictEqual(panels['pysim-sub-apdu'].toggled, [['hidden', true]]);
	}
	// both pills are first-class targets with their own help anchors
	const chv = run('chv');
	assert.strictEqual(chv.out.sub, 'chv');
	assert.strictEqual(chv.out.help, 'pin-adm');
	assert.strictEqual(chv.help, 'pin-adm');
	assert.strictEqual(chv.recorded, 101, 'navRecord plus the pill render');
	assert.deepStrictEqual(chv.panels['pysim-sub-apdu'].toggled, [['hidden', true]]);
	assert.deepStrictEqual(chv.panels['pysim-sub-chv'].toggled, [['hidden', false]]);
	const apdu = run('apdu');
	assert.strictEqual(apdu.out.sub, 'apdu');
	assert.strictEqual(apdu.out.help, 'raw-apdu');
	assert.deepStrictEqual(apdu.panels['pysim-sub-apdu'].toggled, [['hidden', false]]);
	assert.deepStrictEqual(apdu.panels['pysim-sub-chv'].toggled, [['hidden', true]]);
	delete globalThis.document;
});

test('the Card reader opens on the PIN / ADM pill', () => {
	// the default view state
	assert.match(html, /let _pysimSub = 'chv';/);
	// the map's order puts PIN / ADM first, and the unknown-name fallback is
	// the default pill
	assert.match(html, /const PYSIM_SUB_PANELS = \{ chv: 'pysim-sub-chv', apdu: 'pysim-sub-apdu' \};/);
	assert.match(html, /if \(!PYSIM_SUB_PANELS\[name\]\) name = 'chv';/);
	// the help anchor starts at the default pill and follows the switches
	assert.match(html, /let pysimHelpAnchor = PYSIM_SUB_HELP\[_pysimSub\];/);
	assert.match(html, /const PYSIM_SUB_HELP = \{ chv: 'pin-adm', apdu: 'raw-apdu' \};/);
	// the tab's own switch re-applies the last-used pill (which sets its
	// anchor and builds the form for the first paint)
	assert.match(html, /else if \(name === 'pysim'\) pysimSwitchSubtab\(_pysimSub\);/);
});

test('the Card reader and Files help anchors point at their own section', () => {
	// the Card reader opens on the PIN / ADM pill (§6.3), not the status block
	assert.match(html, /let pysimHelpAnchor = PYSIM_SUB_HELP\[_pysimSub\]/);
	// the app lands there too: the first help link follows the same pill
	assert.match(html, /let currentHelpAnchor = PYSIM_SUB_HELP\[_pysimSub\]/);
	// the Files pills map onto their sections (the profiler list included)
	const files = extractFunc(html, 'filesPanelsApply');
	assert.match(files, /services: 'services'/);
	assert.match(files, /custom: 'custom-files'/);
	assert.match(files, /profiles: 'profiler'/);
	assert.match(files, /snapshots: 'card-snapshots'/);
	// ... and the switch itself sets the pill before applying it
	const sw = extractFunc(html, 'filesSwitchSubtab');
	assert.match(sw, /_filesSub = name;\s*filesPanelsApply\(\)/);
});

test('the view switches record history, the back buttons replace it', () => {
	// every user-facing switch records its view
	for (const fn of ['switchTab', 'scp80SwitchSubtab', 'cApduSwitchSubtab',
		'parserSwitchSubtab', 'phoneSwitchSubtab', 'scp81SwitchSubtab',
		'pysimSwitchSubtab', 'filesSwitchSubtab', 'cardsShowList', 'cardsShowEditor',
		'testViewShow', 'profilerSetView']) {
		assert.match(extractFunc(html, fn), /navRecord\(\)/, fn + ' must record');
	}
	// the view variables the state is built from
	assert.match(extractFunc(html, 'switchTab'), /_activeTab = name/);
	assert.match(extractFunc(html, 'scp80SwitchSubtab'), /_scp80Sub = name/);
	assert.match(extractFunc(html, 'cApduSwitchSubtab'), /_capduSub = name/);
	assert.match(extractFunc(html, 'phoneSwitchSubtab'), /_phoneSub = name/);
	assert.match(extractFunc(html, 'scp81SwitchSubtab'), /_scp81Sub = name/);
	assert.match(extractFunc(html, 'pysimSwitchSubtab'), /_pysimSub = name/);
	assert.match(extractFunc(html, 'filesSwitchSubtab'), /_filesSub = name/);
	assert.match(extractFunc(html, 'cardsShowList'), /_cardsView = 'list'/);
	assert.match(extractFunc(html, 'cardsShowEditor'), /_cardsView = 'edit'/);
	assert.match(extractFunc(html, 'cardsEdit'), /_cardsEditId = c\.id/);
	assert.match(extractFunc(html, 'cardsNew'), /_cardsEditId = null/);
	// the nested parser switch must not record two entries (the target sub is
	// set before the recursion)
	const capdu = extractFunc(html, 'cApduSwitchSubtab');
	assert.match(capdu, /_parserSub = name;\s*cApduSwitchSubtab\('parser'\)/);
	// the in-app Cancel/Back (and the profiler save) replace their entry
	for (const fn of ['cardsCancelEdit', 'profilerBackToList', 'testScriptBack',
		'profilerSaveProfile']) {
		assert.match(extractFunc(html, fn), /navReplace\(\)/, fn + ' must replace');
	}
	// the router core: seeded, guarded, replayed through the switch functions
	assert.match(html, /window\.addEventListener\('popstate'/);
	assert.match(html, /history\.pushState\(/);
	assert.match(html, /history\.replaceState\(/);
	assert.match(html, /navSeed\(\);/);
	const apply = extractFunc(html, 'navApply');
	assert.match(apply, /_navApplying = true/);
	assert.match(apply, /switchTab\(/);
	assert.match(apply, /pysimSwitchSubtab\(/);
	assert.match(apply, /filesSwitchSubtab\(/);
	assert.match(apply, /navApplyTest\(/);
	assert.match(apply, /navApplyCards\(/);
	assert.match(apply, /navApplyProfiler\(/);
	// a throwing replay falls back instead of escaping the popstate handler
	assert.match(apply, /catch \(e\)/);
	for (const fn of ['navApplyTest', 'navApplyCards', 'navApplyProfiler', 'testBackToSuites']) {
		assert.match(html, new RegExp('function ' + fn + '\\('), fn + ' must exist');
	}
});
