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
for (const fn of ['ramIsBatchOp', 'ramBatchPanelVisible', 'ramBatchMergedState',
	'ramBatchTitle', 'ramBatchModalTitle', 'ramBatchModalOpen', 'ramBatchModalRender',
	'ramBatchStepsText',
	'ramBatchAttemptsHtml', 'ramBatchRender', 'ramBatchApplyStatus', 'ramBatchFetch',
	'ramBatchStart', 'ramBatchStop', 'ramBatchClear', 'ramOpProgressText',
	'ramOpModalClose', 'esc']) {
	code += extractFunc(html, fn, ASYNC_FNS.indexOf(fn) >= 0) + '\n';
}

const RAM_BATCH_FETCH_STALE_MS = parseInt(
	/const RAM_BATCH_FETCH_STALE_MS = (\d+);/.exec(html)[1], 10);

const els = {};
function setEl(id, value) {
	els[id] = {
		id: id, value: value === undefined ? '' : value, textContent: '', innerHTML: '',
		disabled: false, style: {},
		classes: {},
		classList: {
			add(c) { els[id].classes[c] = true; },
			remove(c) { delete els[id].classes[c]; },
			toggle(c, on) { if (on) els[id].classes[c] = true; else delete els[id].classes[c]; },
			contains(c) { return !!els[id].classes[c]; },
		},
	};
	if (id === 'ram-batch-attempts') {
		// like a browser: replacing the content resets the scroll position
		Object.defineProperty(els[id], 'innerHTML', {
			get() { return this._html || ''; },
			set(v) { this._html = v; this.scrollTop = 0; },
		});
	}
	return els[id];
}
function clearEls() {
	for (const k of Object.keys(els)) delete els[k];
}
// The modal chrome and the batch panel: the element sets the modal tests need.
function setModalEls() {
	for (const id of ['ram-op-modal', 'ram-op-title', 'ram-op-status', 'ram-op-bar',
		'ram-op-steps', 'ram-op-spin', 'ram-op-warn', 'ram-op-close',
		'ram-op-batch-info', 'ram-op-batch-stop', 'ram-op-batch-clear']) setEl(id, '');
}
function setPanelEls() {
	for (const id of ['ram-op', 'ram-batch-panel', 'ram-batch-start', 'ram-batch-stop',
		'ram-batch-clear', 'ram-batch-show', 'ram-batch-spin', 'ram-batch-state',
		'ram-batch-notice', 'ram-batch-counts', 'ram-batch-attempts']) setEl(id, '');
	els['ram-op'].value = 'batch';
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
	var ramInstallParamsCompose = () => ({ install_params: 'C900', stk_params: '' });
	var updateRcPriv = () => {};
	var updateStkParamsHex = () => {};
	var pysimApplyAvailability = () => {};
	var _ramOpActive = false;
	var _ramBatchModalShown = false;
	var _ramBatchLastKind = null;
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
	sandbox.setSummary = v => { _ramBatchSummary = v; };
	sandbox.isFetching = () => _ramBatchFetching;
	sandbox.setFetchingStale = ms => { _ramBatchFetching = true; _ramBatchFetchAt = Date.now() - ms; };
	sandbox.resetFetching = () => { _ramBatchFetching = false; };
	sandbox.getProgress = () => _ramBatchProgress;
	sandbox.ramBatchPanelVisible = ramBatchPanelVisible;
	sandbox.ramBatchAttemptsHtml = ramBatchAttemptsHtml;
	sandbox.ramBatchStepsText = ramBatchStepsText;
	sandbox.ramBatchModalOpen = ramBatchModalOpen;
	sandbox.ramOpModalClose = ramOpModalClose;
	sandbox.isBatchModalShown = () => _ramBatchModalShown;
	sandbox.resetBatchModal = () => { _ramBatchModalShown = false; };
	sandbox.resetKind = () => { _ramBatchLastKind = null; };
	sandbox.setProgress = v => { _ramBatchProgress = v; };
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
	assert.match(src, /const isBatch = ramIsBatchOp\(op\);/);
	// the per-card batch set lives in one place (the panel and the preset
	// warning follow it too)
	const helper = extractFunc(html, 'ramIsBatchOp');
	assert.match(helper, /op === 'batch' \|\| op === 'batch-reinstall' \|\| op === 'batch-delete'/);
	assert.match(extractFunc(html, 'ramBatchPanelVisible'), /ramIsBatchOp\(op\.value\)/);
	assert.match(extractFunc(html, 'spPresetWarningRender'), /ramIsBatchOp\(ramOp\.value\)/);
	assert.match(src, /const isCap = op === 'install-cap' \|\| op === 'batch';/);
	assert.match(src, /const isDelete = op === 'batch-delete';/);
	assert.match(src, /setHidden\('ram-preset-cell', isBatch\)/);
	assert.match(src, /setHidden\('ram-exec-btn', isBatch\)/);
	assert.match(src, /setHidden\('ram-batch-panel', !isBatch\)/);
	assert.match(src, /setHidden\('ram-delete-rows', !isDelete\)/);
	assert.match(src, /setHidden\('ram-del-package-row', !isCap\)/);
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

test('the attempts table keeps its scroll position across a poll render (v3.28.8)', () => {
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
	app.setLast({ active: true, phase: 'waiting_card', attempts: [
		{ ts: 1, iccid: '8970119000004600098', preset: 'p', result: 'installed', duration_ms: 1 }],
		counts: { installed: 1, failed: 0, skipped: 0 }, attempts_total: 1 });
	app.ramBatchRender();
	els['ram-batch-attempts'].scrollTop = 120;     // the reader scrolled back
	app.ramBatchRender();                          // the next 2 s poll render
	assert.strictEqual(els['ram-batch-attempts'].scrollTop, 120);
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

test('the batch group carries the three operations (v3.29.0)', () => {
	assert.match(html, /<optgroup label="Batch \(per card\)" data-l10n="Batch \(per card\)">/);
	assert.match(html, /<option value="batch" data-l10n="Batch install \(\.cap file, per card\)">/);
	assert.match(html, /<option value="batch-reinstall" data-l10n="Batch reinstall app \(per card\)">/);
	assert.match(html, /<option value="batch-delete" data-l10n="Batch delete \(per card\)">/);
	// the group sits at the end of the operation list
	const sel = /<select id="ram-op"[\s\S]*?<\/select>/.exec(html)[0];
	assert.ok(sel.indexOf('value="batch-delete"') > sel.indexOf('value="make-selectable"'));
	// the batch delete fields and the delete-package option exist
	assert.match(html, /id="ram-del-aid"/);
	assert.match(html, /id="ram-del-target"/);
	assert.match(html, /id="ram-del-package"/);
	assert.match(html, /'Batch reinstall app \(per card\)': /);
	assert.match(html, /'Batch delete \(per card\)': /);
	assert.match(html, /'Deleted': /);
	assert.match(html, /'Reinstalled': /);
});

test('ramBatchStart sends the kind-specific body (v3.29.0)', async () => {
	// the delete kind: the AID and the target
	clearEls();
	setEl('ram-op', 'batch-delete');
	setEl('ram-del-aid', 'f0414c4641610101');
	setEl('ram-del-target', 'package');
	app.fetched = [];
	await app.ramBatchStart();
	assert.deepStrictEqual(app.bodies['/api/ram-batch/start'],
		{ kind: 'delete', aid: 'F0414C4641610101', target: 'package', ram_format: 'auto' });
	// a short/odd AID is refused before anything is sent
	clearEls();
	setEl('ram-op', 'batch-delete');
	setEl('ram-del-aid', 'F041');
	setEl('ram-del-target', 'app');
	app.fetched = [];
	app.alerts = [];
	await app.ramBatchStart();
	assert.deepStrictEqual(app.fetched, []);
	assert.match(app.alerts.join(' '), /5-16 bytes/);
	// the reinstall kind: the install-app fields, no .cap
	clearEls();
	setEl('ram-op', 'batch-reinstall');
	setEl('ram-app-loadfile', 'f0414c46416101');
	setEl('ram-app-module', 'f0414c4641610101');
	setEl('ram-app-instance', '');
	setEl('rc-priv', '00');
	setEl('rc-toolkit-enable', '');
	setEl('ram-make-sel', '');
	app.fetched = [];
	await app.ramBatchStart();
	const body = app.bodies['/api/ram-batch/start'];
	assert.strictEqual(body.kind, 'reinstall-app');
	assert.strictEqual(body.loadfile_aid, 'F0414C46416101');
	assert.strictEqual(body.module_aid, 'F0414C4641610101');
	assert.strictEqual(body.install_params, 'C900');
	// the cap kind carries the delete-package flag (the shared field builder)
	assert.match(extractFunc(html, 'ramInstallCapFields'),
		/delete_package: !!\(document\.getElementById\('ram-del-package'\) \|\| \{\}\)\.checked/);
});

test('the batch panel labels follow the run kind (v3.29.0)', () => {
	clearEls();
	setEl('ram-op', 'batch-delete');
	setEl('ram-batch-panel', '');
	setEl('ram-batch-start', '');
	setEl('ram-batch-stop', '');
	setEl('ram-batch-state', '');
	setEl('ram-batch-notice', '');
	setEl('ram-batch-counts', '');
	setEl('ram-batch-attempts', '');
	setEl('ram-batch-clear', '');
	setEl('ram-batch-spin', '');
	app.setLast({ kind: 'delete', active: true, phase: 'installing', attempts: [],
		counts: { installed: 2, failed: 0, skipped: 0 }, attempts_total: 2,
		current: { iccid: '8970119000004600098' } });
	app.ramBatchRender();
	assert.match(els['ram-batch-state'].textContent, /Deleting\.\.\. 8970119000004600098/);
	assert.match(els['ram-batch-counts'].textContent, /Deleted: 2/);
	app.setLast({ kind: 'reinstall-app', active: true, phase: 'waiting_card',
		attempts: [], counts: {}, attempts_total: 0 });
	app.ramBatchRender();
	assert.match(els['ram-batch-counts'].textContent, /Reinstalled: 0/);
});

test('the attempts render the per-step tail for short chains (v3.29.0)', () => {
	const out = app.ramBatchAttemptsHtml([
		{ iccid: '8970119000004600098', result: 'installed',
			steps: [{ name: 'DELETE (application)', sw: '6101', ok: true },
				{ name: 'INSTALL [for install]', sw: '6101', ok: true }] },
		{ iccid: '8970119000004600106', result: 'installed',
			steps: [{ name: 'DELETE (application)', sw: '6A88', ok: true, not_found: true },
				{ name: 'INSTALL [for install]', sw: '6101', ok: true }] },
	]);
	assert.match(out, /DELETE \(application\) 6101 · INSTALL \[for install\] 6101/);
	assert.match(out, /DELETE \(application\) 6A88 \(not found\)/);
	// a long LOAD chain keeps no tail (the failing-step detail covers it)
	assert.strictEqual(app.ramBatchStepsText({ steps: [{}, {}, {}, {}], reason: '' }), '');
});

test('the batch status modal opens on Start and on a poll with a run in flight (v3.30.0)', async () => {
	// the markup: the window's Stop/Clear/info and the panel's Show status
	assert.match(html, /id="ram-op-batch-stop"/);
	assert.match(html, /id="ram-op-batch-clear"/);
	assert.match(html, /id="ram-op-batch-info"/);
	assert.match(html, /id="ram-batch-show" onclick="ramBatchModalOpen\(\)"/);
	assert.match(html, /'Show status': /);
	// Start opens it
	clearEls();
	setModalEls();
	setPanelEls();
	app.setSummary(null);
	els['ram-op'].value = 'batch-delete';
	setEl('ram-del-aid', 'F0414C4641610101');
	setEl('ram-del-target', 'app');
	app.fetched = [];
	await app.ramBatchStart();
	assert.ok(app.isBatchModalShown());
	assert.ok(els['ram-op-modal'].classes['hidden'] === undefined);
	// a page load with an active run reopens it on the first poll
	clearEls();
	setModalEls();
	setPanelEls();
	app.resetBatchModal();
	app.ramBatchApplyStatus({ ram_batch: { active: true, phase: 'waiting_card',
		attempts_total: 0, counts: { installed: 0, failed: 0, skipped: 0 } },
		ram_progress: null });
	assert.ok(app.isBatchModalShown());
	assert.ok(els['ram-op-modal'].classes['hidden'] === undefined);
});

test('the batch modal follows the run: the step, the bar, the cards (v3.30.0)', () => {
	clearEls();
	setModalEls();
	setPanelEls();
	app.setSummary(null);
	app.ramBatchModalOpen();
	// waiting for a card
	app.setLast({ kind: 'reinstall-app', active: true, phase: 'waiting_card',
		attempts: [], counts: {}, attempts_total: 0 });
	app.ramBatchRender();
	assert.strictEqual(els['ram-op-title'].textContent, 'Batch reinstall app (per card)');
	assert.strictEqual(els['ram-op-status'].textContent, 'Insert next card. Waiting for a card...');
	assert.strictEqual(els['ram-op-bar'].style.width, '0%');
	assert.ok(els['ram-op-close'].disabled);
	assert.ok(els['ram-op-batch-stop'].classes['hidden'] === undefined);
	assert.ok(els['ram-op-batch-clear'].disabled);
	// installing: the live step and the bar
	app.setLast({ kind: 'reinstall-app', active: true, phase: 'installing',
		attempts: [], counts: { installed: 1, failed: 0, skipped: 0 },
		attempts_total: 1, current: { iccid: '8970119000004600098' } });
	app.setProgress({ active: true, step: 2, total: 4, name: 'INSTALL [for install]' });
	app.ramBatchRender();
	assert.match(els['ram-op-status'].textContent, /Step 2\/4 — INSTALL \[for install\]/);
	assert.strictEqual(els['ram-op-bar'].style.width, '50%');
	assert.match(els['ram-op-batch-info'].textContent, /Reinstalled: 1/);
	// the finished report: the cards list, the notice, Close enabled
	app.setLast({ kind: 'reinstall-app', active: false, phase: 'finished', attempts: [
		{ iccid: '8970119000004600098', result: 'installed', steps: [
			{ name: 'DELETE (application)', sw: '6101', ok: true },
			{ name: 'INSTALL [for install]', sw: '6101', ok: true }] }],
		counts: { installed: 1, failed: 0, skipped: 0 }, attempts_total: 1,
		notice: 'Card 8970119000004600098 reinstalled.' });
	app.setProgress(null);
	app.ramBatchRender();
	assert.match(els['ram-op-steps'].innerHTML, /DELETE \(application\) 6101 · INSTALL \[for install\] 6101/);
	assert.match(els['ram-op-batch-info'].textContent, /Card 8970119000004600098 reinstalled\./);
	assert.strictEqual(els['ram-op-bar'].style.width, '100%');
	assert.ok(!els['ram-op-close'].disabled);
	assert.ok(els['ram-op-batch-stop'].classes['hidden']);      // the run ended
	assert.ok(!els['ram-op-batch-clear'].disabled);
});

test('a cleared report keeps the run kind in the modal title (v3.30.1)', () => {
	clearEls();
	setModalEls();
	setPanelEls();
	app.setSummary(null);
	app.ramBatchModalOpen();
	app.setLast({ kind: 'delete', active: false, phase: 'finished', attempts: [],
		counts: { installed: 2 }, attempts_total: 2 });
	app.ramBatchRender();
	assert.strictEqual(els['ram-op-title'].textContent, 'Batch delete (per card)');
	// Clear empties the state (kind: null): the title must not flip to install
	app.setLast({ kind: null, active: false, phase: 'idle', attempts: [],
		counts: {}, attempts_total: 0 });
	app.ramBatchRender();
	assert.strictEqual(els['ram-op-title'].textContent, 'Batch delete (per card)');
	// a fresh page (no kind ever seen) keeps the install default
	app.resetBatchModal();
	app.resetKind();
	app.setLast({ kind: null, active: false, phase: 'idle', attempts: [], counts: {}, attempts_total: 0 });
	app.ramBatchModalOpen();
	assert.strictEqual(els['ram-op-title'].textContent, 'Batch install (.cap file, per card)');
});

test('the batch modal stays open while the run is active (v3.30.0)', () => {
	clearEls();
	setModalEls();
	setPanelEls();
	app.setSummary(null);
	app.ramBatchModalOpen();
	app.setLast({ active: true, phase: 'waiting_card', attempts: [], counts: {}, attempts_total: 0 });
	app.ramBatchRender();
	app.ramOpModalClose();
	assert.ok(app.isBatchModalShown(), 'the run keeps the window open');
	assert.ok(els['ram-op-modal'].classes['hidden'] === undefined);
	// the run ended: Close works, and the panel offers the reopen button
	app.setLast({ active: false, phase: 'stopped', attempts: [], counts: { installed: 1 },
		attempts_total: 1 });
	app.ramBatchRender();
	app.ramOpModalClose();
	assert.ok(!app.isBatchModalShown());
	assert.ok(els['ram-op-modal'].classes['hidden']);
	app.ramBatchRender();
	assert.ok(els['ram-batch-show'].classes['hidden'] === undefined);
	// and it reopens for the finished report
	app.ramBatchModalOpen();
	assert.ok(app.isBatchModalShown());
	assert.ok(els['ram-op-modal'].classes['hidden'] === undefined);
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
