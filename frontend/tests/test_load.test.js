const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
// the editor's cap, read from the source so the stub cannot drift
const ITERATIONS_MAX = parseInt(html.match(/const TEST_ITERATIONS_MAX = (\d+);/)[1], 10);

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

const FNS = ['esc', 'testStatusBadge', 'testRenderScriptRun', 'testRenderReport',
	'testIterationsInit', 'testIterationsChanged', 'testIterationsValue',
	'testLoadTiming', 'testDurationText', 'testIterAvgText', 'testRunBannerText'];
const code = FNS.map(f => extractFunc(html, f)).join('\n');

function fakeEl(id) {
	return { id, innerHTML: '', textContent: '', className: '', value: '',
		querySelectorAll: () => [] };
}

function build(els, storage, curRef) {
	const fn = new Function('els', 'storage', 'curRef', `
		var _testRunState = null;
		var _testRunScriptRef = curRef.value;
		var TEST_ITERATIONS_MAX = ${ITERATIONS_MAX};
		var _testIterationsReady = false;
		var localStorage = storage;
		var document = { getElementById: id => els[id] || null };
		var t = k => k;
		function testCurrentScript() { return curRef.value; }
		function testRunStepHtml(s) { return '<STEP ' + (s.note || '') + '>'; }
		function testReportMemberHtml(m, i) { return '<MEMBER ' + (m.name || '') + '>'; }
		${code}
		return {
			setState: v => { _testRunState = v; },
			getState: () => _testRunState,
			testRenderScriptRun, testRenderReport,
			testIterationsValue, testIterationsChanged, testIterationsInit,
			testLoadTiming, testDurationText, testIterAvgText, testRunBannerText,
		};
	`);
	return fn(els, storage, curRef);
}

test('the iteration count clamps and persists (v3.37.0)', () => {
	const els = { 'test-iterations': fakeEl('test-iterations'),
		'test-suite-iterations': fakeEl('test-suite-iterations') };
	const storage = { data: {}, getItem(k) { return this.data[k] === undefined ? null : this.data[k]; },
		setItem(k, v) { this.data[k] = String(v); } };
	const app = build(els, storage, { value: null });

	els['test-iterations'].value = '5';
	assert.strictEqual(app.testIterationsValue('test-iterations'), 5);
	assert.strictEqual(app.testIterationsValue('missing'), 1);
	els['test-iterations'].value = String(ITERATIONS_MAX + 1);
	assert.strictEqual(app.testIterationsValue('test-iterations'), ITERATIONS_MAX);
	els['test-iterations'].value = String(ITERATIONS_MAX);
	assert.strictEqual(app.testIterationsValue('test-iterations'), ITERATIONS_MAX);
	els['test-iterations'].value = 'nope';
	assert.strictEqual(app.testIterationsValue('test-iterations'), 1);

	app.testIterationsChanged(els['test-iterations']);
	els['test-iterations'].value = '7';
	app.testIterationsChanged(els['test-iterations']);
	assert.strictEqual(storage.data.simple_test_iterations, '7');
	assert.strictEqual(els['test-suite-iterations'].value, '7', 'the other input follows');

	// init restores the remembered value
	storage.data.simple_test_iterations = '12';
	app.testIterationsInit();
	assert.strictEqual(els['test-iterations'].value, '12');
	assert.strictEqual(els['test-suite-iterations'].value, '12');
});

test('the run bodies carry the iterations (v3.37.0)', () => {
	const script = extractFunc(html, 'testRunStart');
	assert.match(script, /body\.iterations = testIterationsValue\('test-iterations'\)/);
	const suite = extractFunc(html, 'testSuiteRun');
	assert.match(suite, /body\.iterations = testIterationsValue\('test-suite-iterations'\)/);
	// the inputs live next to the two run buttons, with the raised cap
	assert.ok(html.includes('id="test-iterations" type="number" min="1" max="' + ITERATIONS_MAX + '"'));
	assert.ok(html.includes('id="test-suite-iterations" type="number" min="1" max="' + ITERATIONS_MAX + '"'));
});

test('a running load test shows the counter, not the steps (v3.37.0)', () => {
	const els = { 'test-run-status': fakeEl('test-run-status'),
		'test-run-steps': fakeEl('test-run-steps') };
	const curRef = { value: { id: 's1' } };
	const app = build(els, { getItem: () => null, setItem: () => {} }, curRef);
	app.setState({ running: true, script_id: 's1', name: 'S', steps: [],
		load: { enabled: true, iterations: 100, iteration: 3, done: 2 } });
	app.testRenderScriptRun();
	assert.match(els['test-run-status'].innerHTML, /Iteration 3 \/ 100/);
	assert.match(els['test-run-status'].innerHTML, /done: 2/);
	assert.strictEqual(els['test-run-steps'].innerHTML, '', 'steps are not tracked');
});

test('the live load timing computes elapsed, average and left', () => {
	const app = build({}, { getItem: () => null, setItem: () => {} }, { value: null });
	assert.deepStrictEqual(
		app.testLoadTiming({ enabled: true, started: 940, done: 10, iterations: 100 }, 1000000),
		{ elapsed: 60000, avg: 6000, left: 540000, finished: false });
	// no completed iteration yet: elapsed only (no bogus average/ETA)
	const early = app.testLoadTiming({ enabled: true, started: 940, done: 0, iterations: 10 }, 1000000);
	assert.strictEqual(early.elapsed, 60000);
	assert.strictEqual(early.avg, null);
	assert.strictEqual(early.left, null);
	// a finished run uses the server's final numbers and has no estimate
	assert.deepStrictEqual(
		app.testLoadTiming({ enabled: true, started: 1, finished: 2, elapsed_ms: 5000, avg_ms: 500 }, 1000000),
		{ elapsed: 5000, avg: 500, left: null, finished: true });
	assert.strictEqual(app.testLoadTiming({ enabled: false }, 1000000), null);
	assert.strictEqual(app.testDurationText(60000), '1:00');
	assert.strictEqual(app.testDurationText(3661000), '1:01:01');
	assert.strictEqual(app.testDurationText(null), '');
	assert.strictEqual(app.testIterAvgText(500), '500 ms');
	assert.strictEqual(app.testIterAvgText(1500), '1.50 s');
});

test('the run banner carries the live load timing', () => {
	const app = build({}, { getItem: () => null, setItem: () => {} }, { value: null });
	const now = Date.now();
	const text = app.testRunBannerText({ running: true, name: 'Load suite',
		load: { enabled: true, suite: true, iterations: 50, iteration: 6, done: 5,
			started: now / 1000 - 10 } });
	assert.match(text, /Test suite running: Load suite/);
	assert.match(text, /Iteration 6\/50/);
	assert.match(text, /elapsed 0:10/);
	assert.match(text, /avg 2\.00 s/);
	assert.match(text, /left ~1:30/);
	// a non-load run has no timing part (only its position)
	const plain = app.testRunBannerText({ running: true, name: 'S', index: 2, total: 5 });
	assert.match(plain, /Test script running: S \(3\/5\)/);
	assert.ok(!plain.includes('elapsed'), plain);
});

test('a running load test shows elapsed, average and left', () => {
	const els = { 'test-run-status': fakeEl('test-run-status'),
		'test-run-steps': fakeEl('test-run-steps') };
	const app = build(els, { getItem: () => null, setItem: () => {} },
		{ value: { id: 's1' } });
	const now = Date.now();
	app.setState({ running: true, script_id: 's1', name: 'S', steps: [],
		load: { enabled: true, iterations: 100, iteration: 11, done: 10,
			started: now / 1000 - 60 } });
	app.testRenderScriptRun();
	const head = els['test-run-status'].innerHTML;
	assert.match(head, /elapsed 1:00/);
	assert.match(head, /avg 6\.00 s/);
	assert.match(head, /left ~9:00/);
});

test('the running load suite summary shows the live timing', () => {
	const els = { 'test-report-title': fakeEl('test-report-title'),
		'test-report-summary': fakeEl('test-report-summary'),
		'test-report-members': fakeEl('test-report-members') };
	const app = build(els, { getItem: () => null, setItem: () => {} }, { value: null });
	const now = Date.now();
	app.setState({ running: true, status: null, name: 'Load suite',
		load: { enabled: true, iterations: 50, iteration: 6, done: 5,
			started: now / 1000 - 10 }, suite: null });
	app.testRenderReport();
	const sum = els['test-report-summary'].innerHTML;
	assert.match(sum, /elapsed 0:10/);
	assert.match(sum, /avg 2\.00 s/);
	assert.match(sum, /left ~1:30/);
});

test('a load-test failure shows the failing iteration steps (v3.37.0)', () => {
	const els = { 'test-run-status': fakeEl('test-run-status'),
		'test-run-steps': fakeEl('test-run-steps') };
	const curRef = { value: { id: 's1' } };
	const app = build(els, { getItem: () => null, setItem: () => {} }, curRef);
	app.setState({ running: false, status: 'error', script_id: 's1', name: 'S',
		steps: [{ note: 'boom' }], error: 'iteration 4 failed',
		load: { enabled: true, iterations: 100, iteration: 4, done: 3, failed_iteration: 4 } });
	app.testRenderScriptRun();
	assert.match(els['test-run-status'].innerHTML, /Iteration 4 \/ 100/);
	assert.match(els['test-run-status'].innerHTML, /iteration 4 failed/);
	assert.match(els['test-run-steps'].innerHTML, /<STEP boom>/);
});

test('the suite report summarises a load run and hides successful iterations (v3.37.0)', () => {
	const els = { 'test-report-title': fakeEl('test-report-title'),
		'test-report-summary': fakeEl('test-report-summary'),
		'test-report-members': fakeEl('test-report-members') };
	const app = build(els, { getItem: () => null, setItem: () => {} }, { value: null });
	// a completed load run: no suite state, only the summary
	app.setState({ running: false, status: 'ok', name: 'Load suite',
		load: { enabled: true, iterations: 50, iteration: 50, done: 50,
			elapsed_ms: 12000, avg_ms: 240, failed_iteration: null }, suite: null });
	app.testRenderReport();
	assert.match(els['test-report-title'].innerHTML, /Load suite/);
	assert.match(els['test-report-summary'].innerHTML, /Iteration 50 \/ 50/);
	assert.match(els['test-report-summary'].innerHTML, /done: 50/);
	assert.strictEqual(els['test-report-members'].innerHTML, '');
	// a failure keeps that iteration's members
	app.setState({ running: false, status: 'error', name: 'Load suite',
		error: 'iteration 7 failed',
		load: { enabled: true, iterations: 50, iteration: 7, done: 6,
			elapsed_ms: 2000, avg_ms: 300, failed_iteration: 7 },
		suite: { name: 'Load suite', summary: { ok: 0, warning: 0, error: 1, skipped: 0, stopped: false },
			members: [{ name: 'A', status: 'error' }] } });
	app.testRenderReport();
	assert.match(els['test-report-summary'].innerHTML, /Iteration 7 \/ 50/);
	assert.match(els['test-report-members'].innerHTML, /<MEMBER A>/);
	// while running, the members are not shown (results are not tracked)
	app.setState({ running: true, status: null, name: 'Load suite',
		load: { enabled: true, iterations: 50, iteration: 2, done: 1 },
		suite: { name: 'Load suite', summary: null, members: [{ name: 'A', status: 'running' }] } });
	app.testRenderReport();
	assert.strictEqual(els['test-report-members'].innerHTML, '');
});

test('a successful load run can be exported and reopened after a reload (v3.37.0)', () => {
	// the report is renderable with suite: null, so the export buttons and the
	// reload path must accept a load run too
	for (const fn of ['testReportExportJson', 'testReportExportMarkdown']) {
		const src = extractFunc(html, fn);
		assert.match(src, /_testRunState\.load && _testRunState\.load\.enabled/);
	}
	const init = extractFunc(html, 'testInit');
	assert.match(init, /st\.load && st\.load\.enabled/);
	assert.match(init, /const idx = st\.suite\s*\?/, 'a null suite must not break the reopen');
	// the banner knows a load suite even when its state is cleared
	const banner = extractFunc(html, 'testRunBannerText');
	assert.match(banner, /const isSuite = !!\(snap\.suite \|\| \(load && load\.suite\)\);/);
});
