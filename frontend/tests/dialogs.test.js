// In-app dialog tests: the shared alert/confirm/prompt modal, its promise
// queue and the native fallback.  Mirrors the ram_batch harness (extractFunc
// from index.html, a sandbox built with new Function, an els map and a
// document stub).

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

let code = '';
for (const fn of ['pysimAlert', 'pysimConfirm', 'pysimPrompt', '_pysimDialogShow',
	'_pysimDialogNow', '_pysimDialogRun', 'pysimDialogAnswer', '_pysimDialogKeydown']) {
	code += extractFunc(html, fn) + '\n';
}

const els = {};
function setEl(id, value) {
	els[id] = {
		id: id, value: value === undefined ? '' : value, textContent: '', innerHTML: '',
		disabled: false, style: {}, focused: false, selected: false,
		classes: {},
		classList: {
			add(c) { els[id].classes[c] = true; },
			remove(c) { delete els[id].classes[c]; },
			toggle(c, on) { if (on) els[id].classes[c] = true; else delete els[id].classes[c]; },
			contains(c) { return !!els[id].classes[c]; },
		},
		focus() { els[id].focused = true; },
		select() { els[id].selected = true; },
	};
	return els[id];
}
function clearEls() {
	for (const k of Object.keys(els)) delete els[k];
}
// The five elements the dialog touches.
function setDialogEls() {
	for (const id of ['pysim-dialog-modal', 'pysim-dialog-message', 'pysim-dialog-input',
		'pysim-dialog-cancel', 'pysim-dialog-ok']) setEl(id, '');
}

// The document stub records the keydown listener, so the tests exercise the
// open/close wiring instead of calling the handler directly.
const listeners = {};
const documentStub = {
	getElementById: id => els[id] || null,
	activeElement: null,
	addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
	removeEventListener: (type, fn) => {
		const a = listeners[type] || [];
		const i = a.indexOf(fn);
		if (i >= 0) a.splice(i, 1);
	},
};
function pressKey(key, extra) {
	const ev = Object.assign({ key: key, preventDefault() {} }, extra || {});
	for (const fn of (listeners['keydown'] || []).slice()) fn(ev);
}
const tick = () => new Promise(r => setImmediate(r));

const app = new Function('els', 'document', `
	var sandbox = { alerts: [], confirms: [], prompts: [], confirmAnswer: true, promptAnswer: '' };
	var alert = m => sandbox.alerts.push(String(m));
	var confirm = m => { sandbox.confirms.push(String(m)); return sandbox.confirmAnswer; };
	var prompt = (m, v) => { sandbox.prompts.push([String(m), v]); return sandbox.promptAnswer; };
	var _pysimDialogQueue = Promise.resolve();
	var _pysimDialogResolve = null;
	var _pysimDialogKind = null;
	var _pysimDialogOpenedAt = 0;
	${code}
	sandbox.pysimAlert = pysimAlert;
	sandbox.pysimConfirm = pysimConfirm;
	sandbox.pysimPrompt = pysimPrompt;
	sandbox.pysimDialogAnswer = pysimDialogAnswer;
	sandbox.getOpenedAt = () => _pysimDialogOpenedAt;
	return sandbox;
`)(els, documentStub);

test('pysimAlert sets the message, hides Cancel and resolves on OK', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	const p = app.pysimAlert('Hello');
	await tick();
	assert.strictEqual(els['pysim-dialog-message'].textContent, 'Hello');
	assert.ok(els['pysim-dialog-modal'].classes['hidden'] === undefined);
	assert.ok(els['pysim-dialog-cancel'].classes['hidden']);      // an alert has no Cancel
	assert.ok(els['pysim-dialog-input'].classes['hidden']);       // and no input
	assert.strictEqual((listeners.keydown || []).length, 1);      // the keydown listener is armed
	app.pysimDialogAnswer(true);
	assert.ok(els['pysim-dialog-modal'].classes['hidden']);
	assert.strictEqual(await p, undefined);
});

test('pysimConfirm resolves true on OK and false on Cancel/Escape', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	let p = app.pysimConfirm('Sure?');
	await tick();
	assert.ok(els['pysim-dialog-cancel'].classes['hidden'] === undefined);
	app.pysimDialogAnswer(true);
	assert.strictEqual(await p, true);
	// the Cancel button
	p = app.pysimConfirm('Sure?');
	await tick();
	app.pysimDialogAnswer(false);
	assert.strictEqual(await p, false);
	// Escape cancels
	p = app.pysimConfirm('Sure?');
	await tick();
	pressKey('Escape');
	assert.strictEqual(await p, false);
	// Enter accepts
	p = app.pysimConfirm('Sure?');
	await tick();
	pressKey('Enter');
	assert.strictEqual(await p, true);
});

test('pysimPrompt prefills the default, returns the typed value or null', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	let p = app.pysimPrompt('Name?', 'default');
	await tick();
	assert.ok(els['pysim-dialog-input'].classes['hidden'] === undefined);
	assert.strictEqual(els['pysim-dialog-input'].value, 'default');
	assert.ok(els['pysim-dialog-input'].focused);                 // the input is focused
	assert.ok(els['pysim-dialog-input'].selected);                // with its text selected
	els['pysim-dialog-input'].value = 'typed';
	app.pysimDialogAnswer(true);
	assert.strictEqual(await p, 'typed');
	// Cancel discards the typed value
	p = app.pysimPrompt('Name?', 'default');
	await tick();
	els['pysim-dialog-input'].value = 'typed';
	app.pysimDialogAnswer(false);
	assert.strictEqual(await p, null);
	// Escape is a cancel too
	p = app.pysimPrompt('Name?', 'x');
	await tick();
	pressKey('Escape');
	assert.strictEqual(await p, null);
});

test('overlapping calls queue in order (one dialog at a time)', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	let firstDone = false;
	let secondDone = false;
	const p1 = app.pysimAlert('first');
	p1.then(() => { firstDone = true; });
	const p2 = app.pysimConfirm('second');
	p2.then(() => { secondDone = true; });
	await tick();
	// only the first is open; the second waits
	assert.strictEqual(els['pysim-dialog-message'].textContent, 'first');
	assert.ok(els['pysim-dialog-cancel'].classes['hidden']);
	assert.ok(!firstDone && !secondDone);
	app.pysimDialogAnswer(true);
	await tick();
	// the second opens now, with its own message and Cancel
	assert.strictEqual(els['pysim-dialog-message'].textContent, 'second');
	assert.ok(els['pysim-dialog-cancel'].classes['hidden'] === undefined);
	assert.ok(firstDone && !secondDone);
	app.pysimDialogAnswer(false);
	assert.strictEqual(await p2, false);
});

test('a second answer after the dialog resolved is a no-op', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	const p = app.pysimConfirm('once');
	await tick();
	app.pysimDialogAnswer(true);
	app.pysimDialogAnswer(false);          // must not change the settled result
	assert.strictEqual(await p, true);
	// the keydown listener is removed when the dialog closes
	assert.strictEqual((listeners.keydown || []).length, 0);
});

test('without the modal the native dialogs are used', async () => {
	clearEls();                            // no #pysim-dialog-modal in the els map
	listeners.keydown = [];
	app.alerts = [];
	app.confirms = [];
	app.prompts = [];
	app.confirmAnswer = true;
	app.promptAnswer = 'native';
	assert.strictEqual(await app.pysimAlert('A'), undefined);
	assert.deepStrictEqual(app.alerts, ['A']);
	assert.strictEqual(await app.pysimConfirm('C'), true);
	assert.deepStrictEqual(app.confirms, ['C']);
	app.confirmAnswer = false;
	assert.strictEqual(await app.pysimConfirm('C2'), false);
	assert.strictEqual(await app.pysimPrompt('P', 'def'), 'native');
	assert.deepStrictEqual(app.prompts, [['P', 'def']]);
	assert.strictEqual((listeners.keydown || []).length, 0);
});

test('Enter with the Cancel button focused cancels', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	documentStub.activeElement = els['pysim-dialog-cancel'];
	const p = app.pysimConfirm('Cancel focus?');
	await tick();
	pressKey('Enter');
	assert.strictEqual(await p, false);
	documentStub.activeElement = null;
});

test('a repeated Enter does not answer a queued dialog', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	documentStub.activeElement = null;
	const p1 = app.pysimConfirm('first');
	const p2 = app.pysimConfirm('second');
	await tick();
	pressKey('Enter', { repeat: true });          // a held key: ignored
	pressKey('Enter', { repeat: true });
	assert.strictEqual(els['pysim-dialog-message'].textContent, 'first');
	assert.ok(els['pysim-dialog-modal'].classes['hidden'] === undefined);
	pressKey('Enter');                            // a real press answers it
	await tick();
	assert.strictEqual(await p1, true);
	assert.strictEqual(els['pysim-dialog-message'].textContent, 'second');
	app.pysimDialogAnswer(true);
	assert.strictEqual(await p2, true);
});

test('an event answer inside the grace window is ignored, a later one is accepted', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	documentStub.activeElement = null;
	const p = app.pysimConfirm('grace');
	await tick();
	// a pointer answer within the grace window: ignored (a double-click's 2nd
	// click on the freshly opened dialog)
	app.pysimDialogAnswer(true, { type: 'click', timeStamp: app.getOpenedAt() + 50 });
	assert.ok(els['pysim-dialog-modal'].classes['hidden'] === undefined);
	// a programmatic answer without an event is never ignored
	app.pysimDialogAnswer(true);
	assert.strictEqual(await p, true);
	// a keyboard answer is never grace-checked (a deliberate Enter must work)
	const p2 = app.pysimConfirm('keyboard');
	await tick();
	app.pysimDialogAnswer(false, { type: 'keydown', timeStamp: app.getOpenedAt() + 5 });
	assert.strictEqual(await p2, false);
	// a pointer answer outside the window: accepted
	const p3 = app.pysimConfirm('later');
	await tick();
	app.pysimDialogAnswer(false, { type: 'click', timeStamp: app.getOpenedAt() + 600 });
	assert.strictEqual(await p3, false);
});

// The dialog's own controls, in tab order; the trap must cycle them.
const FOCUS_IDS = ['pysim-dialog-cancel', 'pysim-dialog-input', 'pysim-dialog-ok'];
function resetFocus() { for (const id of FOCUS_IDS) if (els[id]) els[id].focused = false; }
function focusedIds() { return FOCUS_IDS.filter(id => els[id] && els[id].focused); }
function tab(extra) {
	resetFocus();
	let prevented = false;
	pressKey('Tab', Object.assign({ preventDefault: () => { prevented = true; } }, extra || {}));
	return prevented;
}

test('Tab cycles the dialog controls and cannot leave the modal', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	documentStub.activeElement = null;
	const p = app.pysimConfirm('trap');
	await tick();
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-ok']);   // the default focus
	// the browser's own focus move is suppressed
	assert.ok(tab(), 'Tab must be handled, not left to the browser');
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-cancel']);  // wraps to the first
	documentStub.activeElement = els['pysim-dialog-cancel'];
	assert.ok(tab());
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-ok']);
	// Shift+Tab walks back
	documentStub.activeElement = els['pysim-dialog-ok'];
	assert.ok(tab({ shiftKey: true }));
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-cancel']);
	// a focus that escaped the dialog is pulled back in
	documentStub.activeElement = null;
	assert.ok(tab());
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-cancel']);
	documentStub.activeElement = null;
	assert.ok(tab({ shiftKey: true }));
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-ok']);
	app.pysimDialogAnswer(true);
	await p;
});

test('the trap follows the dialog kind (alert: OK only, prompt: Cancel/input/OK)', async () => {
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	documentStub.activeElement = null;
	// an alert has a single control: Tab stays on it
	let p = app.pysimAlert('only ok');
	await tick();
	documentStub.activeElement = els['pysim-dialog-ok'];
	tab();
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-ok']);
	app.pysimDialogAnswer(true);
	await p;
	// a prompt: Cancel -> input -> OK -> Cancel
	clearEls();
	setDialogEls();
	listeners.keydown = [];
	documentStub.activeElement = null;
	p = app.pysimPrompt('q', 'v');
	await tick();
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-input']);
	documentStub.activeElement = els['pysim-dialog-input'];
	tab();
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-ok']);
	documentStub.activeElement = els['pysim-dialog-ok'];
	tab();
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-cancel']);
	documentStub.activeElement = els['pysim-dialog-cancel'];
	tab();
	assert.deepStrictEqual(focusedIds(), ['pysim-dialog-input']);
	app.pysimDialogAnswer(true);
	await p;
});

test('a missing inner element falls back to the native dialog', async () => {
	clearEls();
	// the modal and every element but the OK button exist
	for (const id of ['pysim-dialog-modal', 'pysim-dialog-message', 'pysim-dialog-input',
		'pysim-dialog-cancel']) setEl(id, '');
	listeners.keydown = [];
	app.alerts = [];
	app.confirms = [];
	app.confirmAnswer = false;
	assert.strictEqual(await app.pysimConfirm('C'), false);
	assert.deepStrictEqual(app.confirms, ['C']);
	assert.strictEqual(await app.pysimAlert('A'), undefined);
	assert.deepStrictEqual(app.alerts, ['A']);
	assert.strictEqual((listeners.keydown || []).length, 0);
});

test('the dialog markup and the helpers exist in index.html', () => {
	assert.match(html, /<div id="pysim-dialog-modal"[^>]*role="dialog"[^>]*aria-modal="true"[^>]*class="fixed inset-0 bg-black\/50 z-50 flex items-center justify-center hidden">/);
	assert.match(html, /id="pysim-dialog-message"/);
	assert.match(html, /id="pysim-dialog-input"[^>]*type="text"/);
	assert.match(html, /id="pysim-dialog-cancel"[^>]*onclick="pysimDialogAnswer\(false, event\)"[^>]*data-l10n="Cancel"/);
	assert.match(html, /id="pysim-dialog-ok"[^>]*onclick="pysimDialogAnswer\(true, event\)"[^>]*data-l10n="OK"/);
	for (const fn of ['pysimAlert', 'pysimConfirm', 'pysimPrompt', '_pysimDialogShow',
		'pysimDialogAnswer', '_pysimDialogKeydown']) {
		assert.ok(html.includes('function ' + fn), 'missing ' + fn);
	}
});

test('every dialog class exists in the prebuilt stylesheet', () => {
	// The stale-style trap: style.css is a prebuilt Tailwind output, so a
	// class used only in index.html has no rule until `npm run build` runs.
	// Tailwind escapes ':', '/' and the bracket characters in the selectors;
	// stripping the backslashes mirrors the class names as written.
	const css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8').replace(/\\/g, '');
	const start = html.indexOf('<div id="pysim-dialog-modal"');
	assert.ok(start > 0, 'the dialog markup is missing');
	const block = html.slice(start, html.indexOf('<script>', start));
	const classes = new Set();
	for (const m of block.matchAll(/class="([^"]+)"/g)) {
		for (const c of m[1].split(/\s+/)) if (c) classes.add(c);
	}
	assert.ok(classes.size >= 10, 'expected the dialog classes, got ' + classes.size);
	// Escape regex metacharacters and anchor the class end, so `.mx-4` cannot
	// be satisfied by a `.mx-40` rule (a substring check would accept it).
	const selRe = c => new RegExp('\\.' + c.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&') + '(?![\\w-])');
	for (const c of classes) {
		assert.match(css, selRe(c), 'missing stylesheet rule for .' + c);
	}
});

// A pure comment line is prose, not a call: "... with its prompt (v3.6.38)"
// must not trip the space-tolerant scan below.
function isCommentLine(line) {
	const t = line.trim();
	return t.startsWith('//') || t.startsWith('*') || t.startsWith('/*');
}

test('no native dialog call site is left outside the fallback branch', () => {
	// The sweep moved every alert/confirm/prompt call site onto the helpers.
	// Only the intentional native fallback inside _pysimDialogRun and the PWA
	// install prompt (deferredPrompt.prompt()) may remain; a future bare call
	// names its line in the failure message.
	const lines = html.split('\n');
	const start = lines.findIndex(l => l.includes('function _pysimDialogRun('));
	assert.ok(start > 0, '_pysimDialogRun is missing');
	const end = lines.findIndex((l, i) => i > start && /^function /.test(l));
	assert.ok(end > start, 'the end of _pysimDialogRun is missing');
	const fallback = lines.slice(start, end).join('\n');
	assert.match(fallback, /\balert\(/, 'the native alert fallback is missing');
	assert.match(fallback, /\bconfirm\(/, 'the native confirm fallback is missing');
	assert.match(fallback, /\bprompt\(/, 'the native prompt fallback is missing');
	// catch a qualified native (window.alert / globalThis.confirm) and a
	// space before '(' too
	const re = /(?<![\w.])(?:(?:window|globalThis)\.)?(alert|confirm|prompt)\s*\(/;
	const violations = [];
	const missingAwait = [];
	lines.forEach((line, i) => {
		if (re.test(line) && !(i >= start && i < end) &&
			!line.includes('deferredPrompt.prompt()') && !isCommentLine(line)) {
			violations.push((i + 1) + ': ' + line.trim());
		}
		// A missing await is the dangerous case: a Promise is always truthy,
		// so `if (pysimConfirm(x))` would silently never fire the guard.
		for (const fn of ['pysimConfirm', 'pysimPrompt']) {
			if (!new RegExp('\\b' + fn + '\\s*\\(').test(line)) continue;
			if (new RegExp('function\\s+' + fn + '\\s*\\(').test(line)) continue;
			if (new RegExp('await\\s+' + fn + '\\s*\\(').test(line)) continue;
			missingAwait.push((i + 1) + ': ' + line.trim());
		}
	});
	assert.deepStrictEqual(violations, [],
		'bare native dialog calls left outside _pysimDialogRun:\n' + violations.join('\n'));
	assert.deepStrictEqual(missingAwait, [],
		'pysimConfirm/pysimPrompt calls without await:\n' + missingAwait.join('\n'));
});
