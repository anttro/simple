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

test('the view switches record history, the back buttons replace it', () => {
	// every user-facing switch records its view
	for (const fn of ['switchTab', 'scp80SwitchSubtab', 'cApduSwitchSubtab',
		'parserSwitchSubtab', 'phoneSwitchSubtab', 'scp81SwitchSubtab',
		'cardsShowList', 'cardsShowEditor', 'testViewShow', 'profilerSetView',
		'profilerListSwitch']) {
		assert.match(extractFunc(html, fn), /navRecord\(\)/, fn + ' must record');
	}
	// the view variables the state is built from
	assert.match(extractFunc(html, 'switchTab'), /_activeTab = name/);
	assert.match(extractFunc(html, 'scp80SwitchSubtab'), /_scp80Sub = name/);
	assert.match(extractFunc(html, 'cApduSwitchSubtab'), /_capduSub = name/);
	assert.match(extractFunc(html, 'phoneSwitchSubtab'), /_phoneSub = name/);
	assert.match(extractFunc(html, 'scp81SwitchSubtab'), /_scp81Sub = name/);
	assert.match(extractFunc(html, 'cardsShowList'), /_cardsView = 'list'/);
	assert.match(extractFunc(html, 'cardsShowEditor'), /_cardsView = 'edit'/);
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
	assert.match(extractFunc(html, 'navApply'), /_navApplying = true/);
	assert.match(extractFunc(html, 'navApply'), /switchTab\(/);
	for (const fn of ['navApplyTest', 'navApplyCards', 'navApplyProfiler', 'testBackToSuites']) {
		assert.match(html, new RegExp('function ' + fn + '\\('), fn + ' must exist');
	}
});
