// Test suite view/report tests (v3.22.0): the report renderers, the Markdown
// export and the markup wiring of the four views under the Test script pill.

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name, asyncFn) {
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

let code = '';
for (const fn of ['testStatusBadge', 'testRunStepHtml', 'testReportMemberHtml',
	'testReportMarkdown', 'testSuiteProgress', 'testRoleOptions', 'testFailOptions',
	'testSuiteSave']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'var _testSuites = []; var _testScripts = []; var _testSuiteIdx = -1;'
	+ ' var _testView = "suite"; var _testSuiteSaveTimer = null;'
	+ ' var _testSuiteTarget = null; var _testScriptId = null; var _testScriptDraft = null;\n';
eval(code);
globalThis.t = s => s;
globalThis.esc = s => String(s);

function snapshot() {
	return {
		name: 'regression', status: 'error', preset: 'SE test1',
		started: 1, finished: 2,
		suite: {
			id: 'a'.repeat(32), name: 'regression', require_adm: true,
			session_start: 1, session_end: 1, session_changed: false,
			counters_before: {'2': '0000000010'}, counters_after: {'2': '0000000012'},
			members: [
				{script_id: 'b'.repeat(32), name: 'setup', role: 'setup',
				 on_fail: 'stop', status: 'ok',
				 steps: [{index: 0, type: 'action', label: 'STATUS', status: 'ok',
					checks: [{label: 'SW', ok: true, expected: '9000', actual: '9000'}]}],
				 log: ['TEST-RUN [1/3 setup setup] step 1: STATUS 1/1 -> 9000'], log_truncated: false},
				{script_id: 'c'.repeat(32), name: 'alfa-dsa', role: 'member',
				 on_fail: 'stop', status: 'error',
				 note: 'step 2 failed',
				 steps: [{index: 0, type: 'action', label: 'SCP80', status: 'ok', checks: []},
					{index: 1, type: 'expect', label: 'REFRESH', status: 'error',
					 checks: [{label: 'Command', ok: false, expected: 'REFRESH',
						actual: '(none)', level: 'error'}]}],
				 log: ['TEST-RUN [2/3 member alfa-dsa] step 1: SCP80 -> SW=9103'], log_truncated: true},
				{script_id: 'd'.repeat(32), name: 'teardown', role: 'teardown',
				 on_fail: 'stop', status: 'skipped', steps: [], log: [], log_truncated: false},
			],
			summary: {ok: 1, warning: 0, error: 1, skipped: 1, stopped: false,
				session_changed: false, wall_ms: 12345,
				counters_before: {'2': '0000000010'}, counters_after: {'2': '0000000012'},
				setup: 'ok', teardown: 'skipped'},
		},
		adm: {required: true, verified: true},
	};
}

test('testReportMarkdown renders the members, steps, notes and logs', () => {
	const md = testReportMarkdown(snapshot());
	assert.match(md, /# Test suite report: regression/);
	assert.match(md, /- status: error/);
	assert.match(md, /- Preset: SE test1/);
	assert.match(md, /passed: 1, warnings: 0, failed: 1, skipped: 1/);
	assert.match(md, /wall time: 12\.3 s/);
	assert.match(md, /SCP80: 2 0000000010 -> 0000000012/);
	assert.match(md, /## 2\. alfa-dsa \(member, on_fail: stop\) - error/);
	assert.match(md, /> step 2 failed/);
	assert.match(md, /### Step 2: REFRESH - error/);
	assert.match(md, /- ✗ Command: expected REFRESH, actual \(none\)/);
	assert.match(md, /TEST-RUN \[2\/3 member alfa-dsa\] step 1: SCP80 -> SW=9103/);
	assert.match(md, /## 3\. teardown \(teardown, on_fail: stop\) - skipped/);
});

test('testReportMarkdown tolerates an empty snapshot', () => {
	assert.match(testReportMarkdown(null), /# Test suite report: /);
});

test('testReportMemberHtml carries the status, note and log details', () => {
	const member = snapshot().suite.members[1];
	const htmlOut = testReportMemberHtml(member, 1);
	assert.match(htmlOut, /alfa-dsa/);
	assert.match(htmlOut, /step 2 failed/);
	assert.match(htmlOut, /Run log/);
	assert.match(htmlOut, /truncated/);
	assert.match(htmlOut, /Command: /);
	const teardown = testReportMemberHtml(snapshot().suite.members[2], 2);
	assert.match(teardown, /teardown/);
	assert.match(teardown, /skipped/);
});

test('testSuiteProgress counts the finished members only', () => {
	assert.strictEqual(testSuiteProgress(snapshot()), '3/3');
	const snap = snapshot();
	snap.suite.members[1].status = 'running';
	snap.suite.members[2].status = 'pending';
	assert.strictEqual(testSuiteProgress(snap), '1/3');
});

test('the role and on_fail selects mark the stored value', () => {
	assert.match(testRoleOptions('setup'), /value="setup" selected/);
	assert.match(testRoleOptions('member'), /value="member" selected/);
	assert.doesNotMatch(testRoleOptions('member'), /value="setup" selected/);
	assert.match(testFailOptions('continue'), /value="continue" selected/);
	assert.match(testFailOptions('stop'), /value="stop" selected/);
});

test('the Test script pill hosts the four suite views and their wiring', () => {
	for (const id of ['test-view-suites', 'test-view-suite', 'test-view-script', 'test-view-report']) {
		assert.ok(html.includes('id="' + id + '"'), 'missing ' + id);
	}
	for (const fn of ['testSuiteNew', 'testSuiteOpen', 'testSuiteDelete', 'testSuiteExport',
		'testSuiteRun', 'testSuiteAddScript', 'testSuiteDeleteScript', 'testSuiteMoveScript',
		'testSuiteTargetGo', 'testOpenScript', 'testScriptBack', 'testReportExportJson',
		'testReportExportMarkdown', 'testImportFile', 'testViewShow']) {
		assert.ok(html.includes('function ' + fn) || html.includes('async function ' + fn),
			'missing ' + fn);
	}
	assert.ok(/id="test-suite-name"[^>]*oninput="testSuiteNameChanged\(\)"/.test(html));
	assert.ok(/id="test-suite-adm"[^>]*onchange="testSuiteAdmChanged\(\)"/.test(html));
	assert.ok(/id="test-script-adm"[^>]*onchange="testScriptAdmChanged\(\)"/.test(html));
	assert.ok(/id="test-suite-run-btn"[^>]*data-needs="card"/.test(html));
	assert.ok(/id="test-run-btn"[^>]*data-needs="card"/.test(html));
});

test('the suite run opens the report view and names the preset', () => {
	const src = extractFunc(html, 'testSuiteRun', true);
	assert.match(src, /'\/api\/test\/run'/);
	assert.match(src, /suite_id/);
	assert.match(src, /_testView = 'report'/);
	assert.match(src, /testRunPreset\(usesScp80\)/);
	assert.match(src, /needsAdm/);
});

test('a single script run sends its require_adm flag and keeps its id', () => {
	const src = extractFunc(html, 'testRunStart', true);
	assert.match(src, /require_adm: !!cur\.require_adm/);
	assert.match(src, /body\.script_id = cur\.id/);
	assert.match(src, /testRenderRun\(resp\)/);
});

test('the suite save adopts the stored suite (the server normalises the order)', async () => {
	const calls = [];
	const suite = {id: 'a'.repeat(32), name: 'edited', require_adm: false, scripts: []};
	_testSuites = [suite];
	_testSuiteIdx = 0;
	globalThis.testCurrentSuite = () => suite;
	globalThis.testSuites = () => _testSuites;
	globalThis.testRenderSuiteScripts = () => {};
	globalThis.ioStatus = () => {};
	globalThis.pysimFetch = async (path, body) => {
		calls.push([path, body]);
		return {ok: true, suite: Object.assign({}, suite, {name: 'stored'})};
	};
	testSuiteSave(true);
	await new Promise(r => setTimeout(r, 10));
	assert.strictEqual(calls[0][0], '/api/test/suites/update');
	assert.strictEqual(calls[0][1].suite.scripts.length, 0);
	assert.strictEqual(_testSuites[0].name, 'stored');
});

test('role rows cannot be moved and the i18n refresh redraws the test views', () => {
	const rows = extractFunc(html, 'testRenderSuiteScripts');
	assert.match(rows, /const movable = e\.role === 'member'/);
	assert.match(rows, /!movable \|\| i === 0/);
	assert.match(rows, /!movable \|\| i === entries\.length - 1/);
	const del = extractFunc(html, 'testSuiteDelete', true);
	assert.match(del, /_testSuiteSaveTimer/);
	const i18n = extractFunc(html, 'refreshDynamicI18n');
	assert.match(i18n, /isViewVisible\('phone-sub-test'\)\) testRender\(\)/);
});
