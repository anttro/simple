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

const ASYNC_FNS = ['ramBatchFetch', 'ramBatchStart', 'ramBatchStop', 'ramBatchClear'];
let code = '';
for (const fn of ['ramBatchPanelVisible', 'ramBatchAttemptsHtml', 'ramBatchRender',
	'ramBatchApplyStatus', 'ramBatchFetch', 'ramBatchStart', 'ramBatchStop',
	'ramBatchClear', 'esc']) {
	code += extractFunc(html, fn, ASYNC_FNS.indexOf(fn) >= 0) + '\n';
}

const RAM_BATCH_FETCH_STALE_MS = parseInt(
	/const RAM_BATCH_FETCH_STALE_MS = (\d+);/.exec(html)[1], 10);

const els = {};
function setEl(id, value) {
	els[id] = {
		id: id, value: value === undefined ? '' : value, textContent: '', innerHTML: '',
		disabled: false,
		classes: {},
		classList: {
			add(c) { els[id].classes[c] = true; },
			remove(c) { delete els[id].classes[c]; },
			toggle(c, on) { if (on) els[id].classes[c] = true; else delete els[id].classes[c]; },
			contains(c) { return !!els[id].classes[c]; },
		},
	};
	return els[id];
}
function clearEls() {
	for (const k of Object.keys(els)) delete els[k];
}

const app = new Function('els', `
	var sandbox = { fetched: [], replies: {}, fetchError: null, alerts: [], visibility: true };
	var document = { getElementById: id => els[id] || null };
	var t = k => k;
	var isViewVisible = () => sandbox.visibility;
	var alert = m => sandbox.alerts.push(String(m));
	var pysimFetch = async (p) => {
		sandbox.fetched.push(p);
		if (sandbox.hangs) return new Promise(() => {});   // a stalled connection
		return sandbox.replies[p] || null;
	};
	var pysimFetchOk = async (p, body) => {
		sandbox.fetched.push(p);
		sandbox.bodies = sandbox.bodies || {};
		sandbox.bodies[p] = body;
		if (sandbox.fetchError) throw new Error(sandbox.fetchError);
		return sandbox.replies[p] || { ok: true };
	};
	var capGateOk = () => sandbox.capGate !== false;
	var ramReadFileHex = async () => 'AABB';
	var ramInstallCapFields = () => ({ sd_aid: '', install_params: '', ram_format: 'auto' });
	var pysimApplyAvailability = () => {};
	var _ramBatchLast = null;
	var _ramBatchFetching = false;
	var _ramBatchFetchAt = 0;
	var _ramBatchSummary = null;
	var _ramBatchProgress = null;
	var RAM_BATCH_FETCH_STALE_MS = ${RAM_BATCH_FETCH_STALE_MS};
	${code}
	sandbox.getLast = () => _ramBatchLast;
	sandbox.setLast = v => { _ramBatchLast = v; };
	sandbox.getSummary = () => _ramBatchSummary;
	sandbox.isFetching = () => _ramBatchFetching;
	sandbox.setFetchingStale = ms => { _ramBatchFetching = true; _ramBatchFetchAt = Date.now() - ms; };
	sandbox.resetFetching = () => { _ramBatchFetching = false; };
	sandbox.getProgress = () => _ramBatchProgress;
	sandbox.ramBatchPanelVisible = ramBatchPanelVisible;
	sandbox.ramBatchAttemptsHtml = ramBatchAttemptsHtml;
	sandbox.ramBatchRender = ramBatchRender;
	sandbox.ramBatchApplyStatus = ramBatchApplyStatus;
	sandbox.ramBatchFetch = ramBatchFetch;
	sandbox.ramBatchStart = ramBatchStart;
	sandbox.ramBatchStop = ramBatchStop;
	sandbox.ramBatchClear = ramBatchClear;
	return sandbox;
`)(els);

test('the RAM view wires the batch operation and the panel', () => {
	assert.match(html, /<option value="batch" data-l10n="Batch install \(\.cap file, per card\)">/);
	assert.match(html, /<div id="ram-batch-panel"/);
	for (const id of ['ram-batch-start', 'ram-batch-stop', 'ram-batch-clear',
		'ram-batch-state', 'ram-batch-notice', 'ram-batch-counts', 'ram-batch-attempts']) {
		assert.match(html, new RegExp('id="' + id + '"'));
	}
	// the batch start needs the server (not a card) and the CAP analysis
	assert.match(html, /id="ram-batch-start" data-needs="server" data-cap-gate="ram"/);
	assert.match(html, /'Batch install \(\.cap file, per card\)': /);
});

test('ramOpChanged hides the preset cell and Execute in batch mode', () => {
	const src = extractFunc(html, 'ramOpChanged');
	assert.match(src, /const isBatch = op === 'batch';/);
	assert.match(src, /const isCap = op === 'install-cap' \|\| isBatch;/);
	assert.match(src, /setHidden\('ram-preset-cell', isBatch\)/);
	assert.match(src, /setHidden\('ram-exec-btn', isBatch\)/);
	assert.match(src, /setHidden\('ram-batch-panel', !isBatch\)/);
	assert.match(src, /if \(isBatch\) ramBatchFetch\(\);/);
	// the preset warning does not apply to the per-card resolution
	const warn = extractFunc(html, 'spPresetWarningRender');
	assert.match(warn, /batchMode \? '' : spPresetWarning/);
});

test('the status poll drives the batch panel', () => {
	const poll = extractFunc(html, 'pysimStartBackendPoll');
	assert.match(poll, /const status = await pysimFetch\('\/api\/status'\);/);
	assert.match(poll, /ramBatchApplyStatus\(status\);/);
	// the single install and the batch share the field builder
	const inst = extractFunc(html, 'ramInstallCap', true);
	assert.match(inst, /const fields = ramInstallCapFields\(\);/);
	assert.match(inst, /Object\.assign\(\{/);
	const start = extractFunc(html, 'ramBatchStart', true);
	assert.match(start, /ramInstallCapFields\(\)/);
	assert.match(start, /pysimFetchOk\('\/api\/ram-batch\/start', fields\)/);
});

test('ramBatchAttemptsHtml renders every verdict with its detail', () => {
	const out = app.ramBatchAttemptsHtml([
		{ ts: 1700000000, iccid: '8970119000004600098', preset: 'Alfa', result: 'installed', duration_ms: 12000 },
		{ ts: 1700000001, iccid: '8970119000004600106', preset: 'Alfa', result: 'failed',
			failed_step: 'LOAD (3/7)', por_sw: '6A80', duration_ms: 9000 },
		{ ts: 1700000002, iccid: '8970119000004600114', result: 'skipped',
			reason: 'no preset matches this ICCID' },
	]);
	assert.match(out, /8970119000004600098/);
	assert.match(out, /OK/);
	assert.match(out, /LOAD \(3\/7\) — 6A80/);
	assert.match(out, /no preset matches this ICCID/);
	assert.match(out, /12\.0 s/);
	// newest first
	assert.ok(out.indexOf('8970119000004600114') < out.indexOf('8970119000004600098'));
	assert.match(app.ramBatchAttemptsHtml([]), /No cards processed yet\./);
	// a failure without a step (a lost card mid-chain) shows its error
	assert.match(app.ramBatchAttemptsHtml([{ iccid: '8970119000004600122', result: 'failed',
		error: 'card removed' }]), /card removed/);
});

test('the install and the batch send the form\'s ram_format', () => {
	const fields = extractFunc(html, 'ramInstallCapFields');
	assert.match(fields, /ram_format: \(document\.getElementById\('ram-format'\) \|\| \{\}\)\.value \|\| 'auto'/);
	// the single install merges the shared fields into its request body
	assert.match(extractFunc(html, 'ramInstallCap', true), /Object\.assign\(\{/);
	assert.match(extractFunc(html, 'ramBatchStart', true),
		/pysimFetchOk\('\/api\/ram-batch\/start', fields\)/);
});

test('ramBatchRender shows the phase, the counts and the stop button', () => {
	clearEls();
	setEl('ram-op', 'batch');
	setEl('ram-batch-panel', '');
	setEl('ram-batch-start', '');
	setEl('ram-batch-stop', '');
	setEl('ram-batch-state', '');
	setEl('ram-batch-notice', '');
	setEl('ram-batch-counts', '');
	setEl('ram-batch-attempts', '');
	setEl('ram-batch-clear', '');
	setEl('ram-batch-spin', '');
	// waiting for a card: the explicit prompt, no throbber
	app.setLast({ active: true, phase: 'waiting_card', attempts: [], counts: {}, attempts_total: 0, notice: '' });
	app.ramBatchRender();
	assert.strictEqual(els['ram-batch-state'].textContent, 'Insert next card. Waiting for a card...');
	assert.ok(els['ram-batch-spin'].classes['hidden']);
	assert.ok(els['ram-batch-stop'].classes['hidden'] === undefined);
	assert.ok(els['ram-batch-start'].classes['hidden']);
	assert.ok(els['ram-batch-clear'].disabled);      // the server refuses a clear while active
	// installing card X with the live step
	app.setLast({ active: true, phase: 'installing', attempts: [], counts: { installed: 2, failed: 1, skipped: 0 },
		attempts_total: 3, notice: 'Card 897… installed.', current: { iccid: '8970119000004600098', preset: 'Alfa' } });
	app.ramBatchRender();
	assert.match(els['ram-batch-state'].textContent, /Installing\.\.\. 8970119000004600098 — Alfa/);
	assert.ok(els['ram-batch-spin'].classes['hidden'] === undefined);   // the install throbber
	assert.match(els['ram-batch-counts'].textContent, /Installed: 2 · Failed: 1 · Skipped: 0 · 3 attempts/);
	assert.strictEqual(els['ram-batch-notice'].textContent, 'Card 897… installed.');
	// a new install clears the previous notice: an empty one hides the box
	app.setLast({ active: true, phase: 'installing', attempts: [], counts: {}, attempts_total: 3,
		notice: '', current: { iccid: '8970119000004600106' } });
	app.ramBatchRender();
	assert.ok(els['ram-batch-notice'].classes['hidden']);
	// stopping while a card is still being installed: the throbber stays
	app.setLast({ active: true, phase: 'stopping', attempts: [], counts: {}, attempts_total: 0,
		current: { iccid: '8970119000004600098' } });
	app.ramBatchRender();
	assert.ok(els['ram-batch-spin'].classes['hidden'] === undefined);
	assert.match(els['ram-batch-state'].textContent, /Stopping after the current card\.\.\./);
	// ...but not once the install finished (stopping while waiting)
	app.setLast({ active: true, phase: 'stopping', attempts: [], counts: {}, attempts_total: 0, current: null });
	app.ramBatchRender();
	assert.ok(els['ram-batch-spin'].classes['hidden']);
	assert.strictEqual(els['ram-batch-stop'].textContent, 'Stopping...');
	assert.ok(els['ram-batch-stop'].disabled);
	// stopped
	app.setLast({ active: false, phase: 'stopped', attempts: [], counts: {}, attempts_total: 0 });
	app.ramBatchRender();
	assert.strictEqual(els['ram-batch-state'].textContent, 'Batch stopped');
	assert.ok(els['ram-batch-start'].classes['hidden'] === undefined);
	assert.ok(els['ram-batch-stop'].classes['hidden']);
	assert.ok(!els['ram-batch-clear'].disabled);     // clearable again
});

test('ramBatchApplyStatus fetches only when the summary changed', async () => {
	clearEls();
	setEl('ram-op', 'batch');
	setEl('ram-batch-panel', '');
	setEl('ram-batch-start', '');
	setEl('ram-batch-stop', '');
	setEl('ram-batch-state', '');
	setEl('ram-batch-notice', '');
	setEl('ram-batch-counts', '');
	setEl('ram-batch-attempts', '');
	app.fetched = [];
	app.replies['/api/ram-batch/status'] = { active: true, phase: 'waiting_card', attempts: [], counts: {}, attempts_total: 1 };
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: 1, installed: 0, failed: 0, skipped: 0 } });
	await new Promise(r => setImmediate(r));
	assert.deepStrictEqual(app.fetched, ['/api/ram-batch/status']);
	// an unchanged summary does not refetch
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: 1 } });
	await new Promise(r => setImmediate(r));
	assert.strictEqual(app.fetched.length, 1);
	// a new attempt refetches
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: 2 } });
	await new Promise(r => setImmediate(r));
	assert.strictEqual(app.fetched.length, 2);
	// the panel is not visible -> no fetch, no render
	clearEls();
	app.fetched = [];
	app.ramBatchApplyStatus({ ram_batch: { active: true, attempts_total: 5 } });
	await new Promise(r => setImmediate(r));
	assert.deepStrictEqual(app.fetched, []);
});

test('the panel headline follows the live summary (phase and step)', () => {
	clearEls();
	setEl('ram-op', 'batch');
	setEl('ram-batch-panel', '');
	setEl('ram-batch-start', '');
	setEl('ram-batch-stop', '');
	setEl('ram-batch-state', '');
	setEl('ram-batch-notice', '');
	setEl('ram-batch-counts', '');
	setEl('ram-batch-attempts', '');
	// the detailed status is stale (waiting), the 2 s summary says installing
	app.setLast({ active: true, phase: 'waiting_card', attempts: [], counts: {}, attempts_total: 0 });
	app.ramBatchApplyStatus({
		ram_batch: { active: true, phase: 'installing', attempts_total: 0,
			current: { iccid: '8970119000004600098', preset: 'Alfa' },
			counts: { installed: 0, failed: 0, skipped: 0 } },
		ram_progress: { active: true, step: 3, total: 8, name: 'LOAD (3/7)' },
	});
	assert.match(els['ram-batch-state'].textContent, /Installing\.\.\. 8970119000004600098 — Alfa/);
	assert.match(els['ram-batch-counts'].textContent, /LOAD \(3\/7\)/);
	// ...and back to waiting when the card is gone
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: 0,
		counts: { installed: 0, failed: 0, skipped: 0 } } });
	assert.strictEqual(els['ram-batch-state'].textContent, 'Insert next card. Waiting for a card...');
});

test('a hung detailed fetch does not freeze the counts (v3.28.7)', async () => {
	clearEls();
	setEl('ram-op', 'batch');
	setEl('ram-batch-panel', '');
	setEl('ram-batch-start', '');
	setEl('ram-batch-stop', '');
	setEl('ram-batch-clear', '');
	setEl('ram-batch-spin', '');
	setEl('ram-batch-state', '');
	setEl('ram-batch-notice', '');
	setEl('ram-batch-counts', '');
	setEl('ram-batch-attempts', '');
	app.hangs = true;                        // every detailed fetch stalls
	const sum = n => ({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: n,
		counts: { installed: n, failed: 0, skipped: 0 } } });
	app.ramBatchApplyStatus(sum(12));
	await new Promise(r => setImmediate(r));
	assert.match(els['ram-batch-counts'].textContent, /Installed: 12/);
	// the fetch never settles, but the headline must follow the summary
	app.ramBatchApplyStatus(sum(31));
	await new Promise(r => setImmediate(r));
	assert.match(els['ram-batch-counts'].textContent, /Installed: 31/);
	app.hangs = false;
});

test('a stale fetch guard is released so the report recovers (v3.28.7)', async () => {
	clearEls();
	setEl('ram-op', 'batch');
	setEl('ram-batch-panel', '');
	setEl('ram-batch-start', '');
	setEl('ram-batch-stop', '');
	setEl('ram-batch-clear', '');
	setEl('ram-batch-spin', '');
	setEl('ram-batch-state', '');
	setEl('ram-batch-notice', '');
	setEl('ram-batch-counts', '');
	setEl('ram-batch-attempts', '');
	app.fetched = [];
	app.replies['/api/ram-batch/status'] = { active: true, phase: 'waiting_card', attempts: [],
		attempts_total: 31, counts: { installed: 31, failed: 0, skipped: 0 } };
	// the guard stuck for 11 s: the poll releases it and retries
	app.setFetchingStale(11000);
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: 31,
		counts: { installed: 31, failed: 0, skipped: 0 } } });
	// the stale guard is released and this poll retries (the fetch is issued)
	await new Promise(r => setImmediate(r));
	assert.deepStrictEqual(app.fetched, ['/api/ram-batch/status']);
	assert.ok(!app.isFetching(), 'the guard must be clear again after the fetch');
	assert.match(els['ram-batch-counts'].textContent, /Installed: 31/);
	// a fresh guard is kept (no duplicate fetch)
	app.fetched = [];
	app.setFetchingStale(1000);
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card', attempts_total: 31,
		counts: { installed: 31, failed: 0, skipped: 0 } } });
	await new Promise(r => setImmediate(r));
	assert.deepStrictEqual(app.fetched, []);
	app.resetFetching();
});

test('the stop and clear calls hit their endpoints', async () => {
	clearEls();
	app.fetched = [];
	app.replies['/api/ram-batch/stop'] = { active: true, phase: 'stopping', attempts: [], counts: {}, attempts_total: 0 };
	app.replies['/api/ram-batch/clear'] = { ok: true };
	app.replies['/api/ram-batch/status'] = { active: false, phase: 'idle', attempts: [], counts: {}, attempts_total: 0 };
	setEl('ram-op', 'batch');
	setEl('ram-batch-panel', '');
	await app.ramBatchStop();
	await app.ramBatchClear();
	await new Promise(r => setImmediate(r));
	assert.deepStrictEqual(app.fetched, ['/api/ram-batch/stop', '/api/ram-batch/clear', '/api/ram-batch/status']);
});
