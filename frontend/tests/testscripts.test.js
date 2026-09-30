const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractBlock(startMarker, endMarker) {
	const start = html.indexOf(startMarker);
	const end = html.indexOf(endMarker, start);
	if (start < 0 || end < 0) throw new Error('block not found: ' + startMarker);
	return html.slice(start, end);
}

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

eval(extractBlock('const CMD_NAMES = {', 'function cmdQualifierShort').replace(/^const /gm, 'var '));
eval(extractBlock('const TEST_ACTION_KINDS = [', 'let _testScripts').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'testTemplate'));
eval(extractFunc(html, 'testScriptProblem'));
eval(extractFunc(html, 'testCheckSummary'));
eval(extractFunc(html, 'testStepSummary'));
eval(extractFunc(html, 'testCommandOptions'));
globalThis.t = s => s;
globalThis.esc = s => String(s);
eval(extractFunc(html, 'testFormRow'));
eval(extractFunc(html, 'testFormInput'));
eval(extractFunc(html, 'testFormSelect'));
eval(extractFunc(html, 'testStepRender'));
eval(extractFunc(html, 'testRenderChecks'));
eval(extractFunc(html, 'testStepCollect'));
eval(extractFunc(html, 'testWriteBackCounter'));
eval(extractFunc(html, 'testChecksCollect'));
eval('var _testEditStep = null; var _testEditChecks = []; var _testEditStepIndex = -1;'
	+ ' var _testScripts = null; var _testCurrentIdx = -1; var _testRunState = null;'
	+ ' var _testLastPresetIdx = -1;');
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };

function fakeForm(values) {
	globalThis.document = { getElementById: id => (id in values ? { value: values[id] } : null) };
}

test('the templates contain usable steps', () => {
	const menu = testTemplate('menu');
	assert.strictEqual(menu.steps.length, 3);
	assert.strictEqual(menu.steps[0].kind, 'menu-select');
	assert.deepStrictEqual(menu.steps[0].check.sw, { mode: 'mask', value: '91??' });
	assert.strictEqual(menu.steps[1].command, 'SELECT ITEM');
	assert.strictEqual(menu.steps[1].checks[0].kind, 'item');
	assert.strictEqual(menu.steps[2].command, 'DISPLAY TEXT');
	const scp80 = testTemplate('scp80');
	assert.strictEqual(scp80.steps[0].kind, 'scp80');
	assert.strictEqual(scp80.steps[0].check.por, 'none');
	assert.ok(scp80.steps[0].params.tar);
	assert.strictEqual(testTemplate('empty').steps.length, 0);
});

test('testScriptProblem accepts good scripts and names bad ones', () => {
	assert.strictEqual(testScriptProblem(testTemplate('menu')), '');
	assert.strictEqual(testScriptProblem(testTemplate('scp80')), '');
	assert.match(testScriptProblem(null), /object/);
	assert.match(testScriptProblem({ steps: [] }), /no steps/);
	assert.match(testScriptProblem({ steps: [{ type: 'nope' }] }), /action or an expectation/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'nope' }] }), /unknown action kind/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'apdu', params: {} }] }), /APDU is empty/);
	assert.match(testScriptProblem({
		steps: [{ type: 'action', kind: 'scp80', params: { apdu: '' } }] }), /SCP80/);
	assert.match(testScriptProblem({
		steps: [{ type: 'action', kind: 'menu-select', params: { item_id: 0 } }] }), /item id/);
	assert.match(testScriptProblem({
		steps: [{ type: 'action', kind: 'file-read', params: {} }] }), /file path/);
	assert.match(testScriptProblem({ steps: [{ type: 'expect' }] }), /command is required/);
	// the selected SCP80 source decides which value is required
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { source: 'apdu', sp: 'AA' } }] }), /SCP80/);
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { source: 'apdu', apdu: '80E2' } }] }), '');
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { source: 'sp', sp: 'AA', apdu: '80E2' } }] }), '');
});

test('testStepSummary renders actions', () => {
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'menu-select', params: { item_id: 3 },
			check: { sw: { mode: 'mask', value: '91??' } } }),
		'ENVELOPE(Menu Selection) item=3 · SW ~91??');
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'apdu', params: { apdu: '00A4' } }),
		'APDU 00A4');
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'scp80', params: { apdu: '80E2', tar: 'B00000' },
			check: { sw: { mode: 'mask', value: '91??' }, por: 'none' } }),
		'SCP80 80E2 TAR=B00000 · SW ~91??, PoR none');
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'status', params: { attempts: 5 } }),
		'STATUS x5 (poll)');
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'file-read', params: { path: 'MF/2FE2' } }),
		'READ MF/2FE2');
	assert.ok(testStepSummary({ type: 'action', kind: 'status', params: {},
		check: { sw: { mode: 'exact', value: '9000' } }, on_fail: 'warning' }).includes('⚠'));
});

test('testStepSummary renders expectations with checks and the response', () => {
	assert.strictEqual(
		testStepSummary({ type: 'expect', command: 'SELECT ITEM',
			checks: [{ kind: 'item', id: 1, text: 'test', mode: 'contains' }],
			respond: { result: 'ok', item_id: 1 } }),
		'EXPECT SELECT ITEM · item #1 ~"test" · TR item=1');
	assert.strictEqual(
		testStepSummary({ type: 'expect', command: 'DISPLAY TEXT',
			checks: [{ kind: 'text', mode: 'contains', value: 'hello' }], respond: { result: 'ok' } }),
		'EXPECT DISPLAY TEXT · text ~"hello" · TR ok');
	assert.ok(testStepSummary({ type: 'expect', command: 'DISPLAY TEXT',
		checks: [{ kind: 'text', value: 'x' }], respond: { result: 'cancel' } }).includes('TR cancel'));
	assert.ok(testStepSummary({ type: 'expect', command: 'GET INPUT', respond: { result: 'ok', text: 'hi' } })
		.includes('text="hi"'));
	assert.strictEqual(testCheckSummary({ kind: 'raw', mode: 'mask', value: 'AA??' }), 'raw ~AA??');
});

test('testCommandOptions covers the proactive names and keeps custom values', () => {
	const opts = testCommandOptions('DISPLAY TEXT');
	assert.ok(opts.some(o => o.v === 'ANY'));
	assert.ok(opts.some(o => o.v === 'DISPLAY TEXT'));
	assert.ok(opts.some(o => o.v === 'SELECT ITEM'));
	const custom = testCommandOptions('0x74');
	assert.ok(custom.some(o => o.v === '0x74'));
});

test('the SCP80 source switch sticks and preserves the other value', () => {
	_testEditStep = { type: 'action', kind: 'scp80', params: { apdu: '80E2900000' } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	// switch to the pre-built packet: the packet field is not rendered yet
	fakeForm({ 'test-step-kind': 'scp80', 'test-f-src': 'sp', 'test-f-apdu': '80E2900000',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-por': 'any', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.source, 'sp');
	assert.strictEqual(_testEditStep.params.sp, '');
	assert.strictEqual(_testEditStep.params.apdu, '80E2900000');   // preserved
	assert.ok(!('sw' in _testEditStep.check));                     // server default
	// the packet field is rendered now and gets a value
	fakeForm({ 'test-step-kind': 'scp80', 'test-f-src': 'sp', 'test-f-sp': 'aabbcc',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-por': 'none', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.sp, 'AABBCC');
	assert.strictEqual(_testEditStep.params.source, 'sp');
	assert.strictEqual(_testEditStep.check.por, 'none');
	// switching back keeps both values
	fakeForm({ 'test-step-kind': 'scp80', 'test-f-src': 'apdu', 'test-f-apdu': '80E2900000',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-por': 'any', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.source, 'apdu');
	assert.strictEqual(_testEditStep.params.sp, 'AABBCC');
});

test('the STATUS step leaves the SW check to the server default', () => {
	_testEditStep = { type: 'action', kind: 'status', params: { attempts: 5, interval_ms: 200 } };
	fakeForm({ 'test-step-kind': 'status', 'test-f-attempts': '5', 'test-f-interval': '200',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params, { attempts: 5, interval_ms: 200 });
	assert.deepStrictEqual(_testEditStep.check, {});
});

test('the step form renders the chosen source and no pre-filled SW', () => {
	const els = {
		'test-step-modal': { classList: { add: () => {}, remove: () => {} } },
		'test-step-title': {}, 'test-step-body': {},
		'test-step-error': { classList: { add: () => {}, remove: () => {} } },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	_testEditStep = { type: 'action', kind: 'scp80', params: { source: 'sp', sp: 'AABB', apdu: '80E2' } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	testStepRender();
	const body = els['test-step-body'].innerHTML;
	assert.match(body, /id="test-f-sp" value="AABB"/);
	assert.match(body, /<option value="sp" selected>/);
	assert.match(body, /id="test-f-sw" value=""/);
});

test('hex fields strip mask wildcards, check values keep them', () => {
	_testEditStep = { type: 'action', kind: 'apdu', params: {} };
	fakeForm({ 'test-step-kind': 'apdu', 'test-f-apdu': '80E2??',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.apdu, '80E2');
	fakeForm({ 'test-step-kind': 'apdu', 'test-f-apdu': '80E2', 'test-f-sw': '91??',
		'test-f-sw-mode': 'mask', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.check.sw, { mode: 'mask', value: '91??' });
});

test('the counter write-back finds the preset by name after a reload', () => {
	let renders = 0;
	globalThis.cards = [{ name: 'Card 1', iccid: '8970119000004600098', cntr: '0000000A' }];
	globalThis.cardsRender = () => { renders++; };
	globalThis.ioStatus = () => {};
	// the name lookup must win: the ICCID helpers are not even called
	globalThis.cardsNormIccid = () => { throw new Error('ICCID lookup for a name'); };
	globalThis.cardsFindByIccid = () => { throw new Error('ICCID lookup for a name'); };
	_testLastPresetIdx = -1;
	_testRunState = { running: false, scp80_counter: '0000000B', preset: 'Card 1' };
	testWriteBackCounter();
	assert.strictEqual(cards[0].cntr, '0000000B');
	assert.strictEqual(renders, 1);
	testWriteBackCounter();                       // idempotent
	assert.strictEqual(renders, 1);
});

test('the counter write-back falls back to an ICCID snapshot', () => {
	let renders = 0;
	globalThis.cards = [{ name: 'Other', iccid: '8970119000004600098', cntr: '0000000A' }];
	globalThis.cardsRender = () => { renders++; };
	globalThis.ioStatus = () => {};
	globalThis.cardsNormIccid = v => String(v).replace(/\D/g, '');
	globalThis.cardsFindByIccid = v => (globalThis.cardsNormIccid(v) === '8970119000004600098' ? 0 : -1);
	_testLastPresetIdx = -1;
	_testRunState = { running: false, scp80_counter: '0000000C', preset: '8970119000004600098' };
	testWriteBackCounter();
	assert.strictEqual(cards[0].cntr, '0000000C');
	assert.strictEqual(renders, 1);
});

test('the item text check offers contains/exact only', () => {
	const els = {
		'test-step-modal': { classList: { add: () => {}, remove: () => {} } },
		'test-step-title': {}, 'test-step-body': {},
		'test-step-error': { classList: { add: () => {}, remove: () => {} } },
		'test-checks': {},
	};
	globalThis.document = { getElementById: id => els[id] || null };
	_testEditStep = { type: 'expect', command: 'SELECT ITEM',
		checks: [{ kind: 'item', text: 'test', mode: 'contains' }], respond: { result: 'ok' } };
	_testEditChecks = [{ kind: 'item', text: 'test', mode: 'contains' }];
	_testEditStepIndex = 0;
	testStepRender();
	const checks = els['test-checks'].innerHTML;
	assert.match(checks, /<option value="contains" selected>/);
	assert.ok(!checks.includes('value="mask"'), checks);
});

test('the Simulator hosts the Test script pill and its wiring', () => {
	assert.match(html, /data-phone-sub="test"[^>]*data-l10n="Test script"/);
	assert.match(html, /id="phone-sub-test"/);
	assert.match(html, /\['phone', 'tr', 'bip', 'esim', 'test'\]/);
	assert.match(html, /else if \(name === 'test'\) \{\s*testInit\(\);/);
	assert.match(html, /test: 'test-script'/);
	assert.match(html, /testRunActive\(\) && el\.closest && !el\.closest\('#phone-sub-test'\)/);
	assert.match(html, /\/api\/test\/run/);
	assert.match(html, /\/api\/test\/status/);
	assert.match(html, /id="test-step-modal"/);
});
