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
	// the module view variables + the one helper it calls, stubbed
	eval(`var _activeTab = 'c-apdu', _scp80Sub = 'sp', _capduSub = 'sim', _parserSub = 'parse',
		_phoneSub = 'phone', _scp81Sub = 'listener', _pysimSub = 'files', _cardsView = 'list',
		_cardsEditId = null, _testView = 'suites', _testScriptId = null, _navSuite = null,
		profilerView = 'list', profilerListTab = 'profiles', profilerEditId = null,
		snapshotViewId = null;
		function testCurrentSuite() { return _navSuite; }`);
	eval(extractFunc(html, 'navCurrent'));
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
	_activeTab = 'cards'; _cardsView = 'edit'; _cardsEditId = 'P';
	assert.deepStrictEqual(navCurrent(), { tab: 'cards', view: 'edit', preset: 'P' });
	// a new-preset editor carries no id
	_cardsEditId = null;
	assert.deepStrictEqual(navCurrent(), { tab: 'cards', view: 'edit' });
	_activeTab = 'profiler'; profilerView = 'snapshot'; snapshotViewId = 'N';
	assert.deepStrictEqual(navCurrent(), { tab: 'profiler', view: 'snapshot', item: 'N' });
	profilerView = 'list'; profilerListTab = 'custom';
	assert.deepStrictEqual(navCurrent(), { tab: 'profiler', view: 'list', listtab: 'custom' });
});

test('the view switches record history, the back buttons replace it', () => {
	// every user-facing switch records its view
	for (const fn of ['switchTab', 'scp80SwitchSubtab', 'cApduSwitchSubtab',
		'parserSwitchSubtab', 'phoneSwitchSubtab', 'scp81SwitchSubtab',
		'pysimSwitchSubtab', 'cardsShowList', 'cardsShowEditor', 'testViewShow',
		'profilerSetView', 'profilerListSwitch']) {
		assert.match(extractFunc(html, fn), /navRecord\(\)/, fn + ' must record');
	}
	// the view variables the state is built from
	assert.match(extractFunc(html, 'switchTab'), /_activeTab = name/);
	assert.match(extractFunc(html, 'scp80SwitchSubtab'), /_scp80Sub = name/);
	assert.match(extractFunc(html, 'cApduSwitchSubtab'), /_capduSub = name/);
	assert.match(extractFunc(html, 'phoneSwitchSubtab'), /_phoneSub = name/);
	assert.match(extractFunc(html, 'scp81SwitchSubtab'), /_scp81Sub = name/);
	assert.match(extractFunc(html, 'pysimSwitchSubtab'), /_pysimSub = name/);
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
	assert.match(apply, /navApplyTest\(/);
	assert.match(apply, /navApplyCards\(/);
	assert.match(apply, /navApplyProfiler\(/);
	// a throwing replay falls back instead of escaping the popstate handler
	assert.match(apply, /catch \(e\)/);
	for (const fn of ['navApplyTest', 'navApplyCards', 'navApplyProfiler', 'testBackToSuites']) {
		assert.match(html, new RegExp('function ' + fn + '\\('), fn + ' must exist');
	}
});
