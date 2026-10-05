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
eval(extractBlock('const EVENT_NAMES = {', 'const EVENT_FORMS = {').replace(/^const /gm, 'var '));
eval(extractBlock('const EVENT_FORMS = {', 'const PLI_QUALIFIERS = [').replace(/^const /gm, 'var '));
// the builders' pure helpers (eventFormFieldsHtml/Values are the shared form)
eval(extractFunc(html, 'bytesToHex'));
eval(extractFunc(html, 'eventAddressTlv'));
eval(extractFunc(html, 'encPlmn'));
eval(extractFunc(html, 'eventFormFieldsHtml'));
eval(extractFunc(html, 'eventFormValues'));
eval(extractFunc(html, 'eventFormPatternError'));
eval(extractFunc(html, 'eventFormSrc'));
eval(html.match(/const EVENT_INNER_MAX = \d+;/)[0].replace('const ', 'var '));
eval(extractFunc(html, 'eventEnvelopeSize'));
eval(extractFunc(html, 'eventFitsOneEnvelope'));
eval(extractBlock('const TEST_ACTION_KINDS = [', 'let _testScripts').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'testTemplate'));
eval(extractFunc(html, 'testMenuSelectProblem'));
eval(extractFunc(html, 'testScriptProblem'));
eval(extractFunc(html, 'testPorSpecSummary'));
eval(extractFunc(html, 'testCheckSummary'));
eval(extractFunc(html, 'testStepSummary'));
eval(extractFunc(html, 'testCommandOptions'));
globalThis.t = s => s;
globalThis.esc = s => String(s);
globalThis.testScripts = () => _testScripts || [];
globalThis.testSuites = () => _testSuites || [];
eval(extractFunc(html, 'testFormRow'));
eval(extractFunc(html, 'testFormInput'));
eval(extractFunc(html, 'testFormSelect'));
eval(extractFunc(html, 'testStepRender'));
eval(extractFunc(html, 'testRenderChecks'));
eval(extractFunc(html, 'testStepCollect'));
eval(extractFunc(html, 'testStepFormError'));
eval(extractFunc(html, 'testWriteBackCounter'));
eval(extractFunc(html, 'testRunPreset'));
eval(extractFunc(html, 'testScriptsWriteback', true));
eval(extractFunc(html, 'testScriptsWritebackQueued'));
eval(extractFunc(html, 'testScriptsSave'));
eval(extractFunc(html, 'testRunStart', true));
eval(extractFunc(html, 'testSuiteDeleteScript', true));
eval(extractFunc(html, 'testSuitesRefetch', true));
eval(extractFunc(html, 'testChecksCollect'));
eval('var _testEditStep = null; var _testEditChecks = []; var _testEditStepIndex = -1;'
	+ ' var _testScripts = null; var _testSuites = []; var _testScriptId = null;'
	+ ' var _testScriptDraft = null; var _testRunState = null; var _testSaveTimer = null;'
	+ ' var _testWriteChains = new WeakMap(); var _testLastPresetIdx = -1;'
	+ ' var _testRunScriptRef = null; var _testSuiteIdx = -1;');
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
	// the item can be selected by its text instead of the install-dependent id
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'menu-select',
		params: { text: 'My menu', mode: 'contains' } }] }), '');
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'menu-select',
		params: {} }] }), /item id or text/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'menu-select',
		params: { item_id: 1, text: 'My menu' } }] }), /not both/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'event',
		params: {} }] }), /event is required/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'event',
		params: { event: '03', fields: [] } }] }), /object/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'event',
		params: { event: '00' } }] }), /event data is required/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'event',
		params: { event: '00', data: 'ZZ' } }] }), /hex/);
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'event',
		params: { event: '03', fields: { status: 0 } } }] }), '');
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'event',
		params: { event: '00', data: '1C0101', src: '83' } }] }), '');
	assert.match(testScriptProblem({
		steps: [{ type: 'action', kind: 'file-read', params: {} }] }), /file path/);
	assert.match(testScriptProblem({ steps: [{ type: 'expect' }] }), /command is required/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'proactive',
		params: { attempts: 0 } }] }), /attempts/);
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'proactive',
		params: {} }] }), '');
	// the selected SCP80 source decides which value is required
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { source: 'apdu', sp: 'AA' } }] }), /SCP80/);
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { source: 'apdu', apdu: '80E2' } }] }), '');
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { source: 'sp', sp: 'AA', apdu: '80E2' } }] }), '');
});

test('the proactive cleanup step collects its fields', () => {
	_testEditStep = { type: 'action', kind: 'proactive', params: {} };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	fakeForm({ 'test-step-kind': 'proactive', 'test-f-presult': 'ok', 'test-f-pfirst': 'cancel',
		'test-f-pattempts': '5', 'test-f-pinterval': '0', 'test-f-prequire': 'refresh',
		'test-f-prequal': '00', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	const p = _testEditStep.params;
	assert.strictEqual(p.respond.result, 'ok');
	assert.strictEqual(p.first.result, 'cancel');
	assert.strictEqual(p.attempts, 5);
	assert.strictEqual(p.interval_ms, 0);
	assert.deepStrictEqual(p.require, { command: 'REFRESH', qualifier: '00' });
	// an empty require field drops the assertion
	fakeForm({ 'test-step-kind': 'proactive', 'test-f-presult': 'ok', 'test-f-pfirst': '',
		'test-f-pattempts': '3', 'test-f-pinterval': '200', 'test-f-prequire': '',
		'test-f-prequal': '', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.ok(!('first' in _testEditStep.params));
	assert.ok(!('require' in _testEditStep.params));
});

test('testStepSummary renders actions', () => {
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'menu-select', params: { item_id: 3 },
			check: { sw: { mode: 'mask', value: '91??' } } }),
		'ENVELOPE(Menu Selection) item=3 · SW ~91??');
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'menu-select',
			params: { text: 'My menu', mode: 'exact' } }),
		'ENVELOPE(Menu Selection) text ="My menu"');
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
		testStepSummary({ type: 'action', kind: 'proactive',
			params: { attempts: 5, respond: { result: 'ok' },
				require: { command: 'REFRESH', qualifier: '00' } } }),
		'PROACTIVE x5 require REFRESH q=00 TR ok');
	assert.strictEqual(
		testStepSummary({ type: 'action', kind: 'proactive',
			params: { attempts: 3, first: { result: 'cancel' } } }),
		'PROACTIVE x3 TR ok first cancel');
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

test('the SCP80 step collects the format and the custom PoR object', () => {
	_testEditStep = { type: 'action', kind: 'scp80', params: { apdu: '80E2900000' } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	fakeForm({ 'test-step-kind': 'scp80', 'test-f-src': 'apdu', 'test-f-apdu': '80E2900000',
		'test-f-fmt': 'expanded', 'test-f-kvn': '', 'test-f-tar': '', 'test-f-spi1': '',
		'test-f-spi2': '', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-por': 'custom',
		'test-f-porstatus': 'por_ok', 'test-f-por-sw': '6a8?', 'test-f-por-data': 'aa??',
		'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.format, 'expanded');
	assert.deepStrictEqual(_testEditStep.check.por,
		{ status: 'por_ok', sw: { mode: 'mask', value: '6A8?' },
			data: { mode: 'mask', value: 'AA??' } });
	// a custom PoR check with no fields refuses to save
	fakeForm({ 'test-step-kind': 'scp80', 'test-f-src': 'apdu', 'test-f-apdu': '80E2900000',
		'test-f-fmt': 'compact', 'test-f-kvn': '', 'test-f-tar': '', 'test-f-spi1': '',
		'test-f-spi2': '', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-por': 'custom',
		'test-f-fail': 'error' });
	assert.match(testStepFormError(), /PoR check/);
	assert.ok(!('format' in _testEditStep.params));    // compact is the default
	// the plain string forms stay and their object is dropped on switch
	fakeForm({ 'test-step-kind': 'scp80', 'test-f-src': 'apdu', 'test-f-apdu': '80E2900000',
		'test-f-fmt': '', 'test-f-kvn': '', 'test-f-tar': '', 'test-f-spi1': '',
		'test-f-spi2': '', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-por': 'none',
		'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.check.por, 'none');
	// the summary renders the effective format and the object's fields
	const s = testStepSummary({ type: 'action', kind: 'scp80',
		params: { apdu: '80E2', format: 'expanded-ae' },
		check: { por: { status: 'por_ok', sw: { mode: 'mask', value: '6A8?' } } } });
	assert.ok(s.includes('fmt=expanded-ae'), s);
	assert.ok(s.includes('PoR status por_ok SW ~6A8?'), s);
});

test('the SCP80 step form renders the format select and the custom PoR fields', () => {
	const els = {
		'test-step-modal': { classList: { add: () => {}, remove: () => {} } },
		'test-step-title': {}, 'test-step-body': {},
		'test-step-error': { classList: { add: () => {}, remove: () => {} } },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	_testEditStep = { type: 'action', kind: 'scp80',
		params: { apdu: '80E2900000', format: 'expanded' },
		check: { por: { status: 'por_ok' } } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	testStepRender();
	const body = els['test-step-body'].innerHTML;
	assert.match(body, /id="test-f-fmt"/);
	assert.match(body, /<option value="expanded" selected>/);
	assert.match(body, /<option value="custom" selected>/);
	assert.match(body, /id="test-f-porstatus" value="por_ok"/);
	assert.match(body, /id="test-f-por-sw" value=""/);
	// the block explains where PoR SW comes from (the applet-TAR confusion)
	assert.ok(body.includes('R-APDU SW inside a remote-management'), body);
	assert.ok(body.includes('leave SW empty'), body);
	// the pre-built packet source has no format select (the wrapper applies
	// to a C-APDU only)
	_testEditStep = { type: 'action', kind: 'scp80', params: { source: 'sp', sp: 'AABB' } };
	testStepRender();
	assert.ok(!els['test-step-body'].innerHTML.includes('id="test-f-fmt"'));
});

test('the alpha and sms checks collect and summarise', () => {
	assert.strictEqual(testCheckSummary({ kind: 'alpha', mode: 'exact', value: 'Alfa' }),
		'alpha ="Alfa"');
	assert.strictEqual(testCheckSummary({ kind: 'sms', da: '12345',
		pid: { mode: 'exact', value: '7F' }, dcs: { mode: 'exact', value: '00' },
		udl: 5, ud: { mode: 'mask', value: 'AA??' } }),
		'sms DA 12345 PID 7F DCS 00 UDL 5 UD ~AA??');
	// the sms fields collect with hex masks and a decimal UDL (0 included)
	_testEditChecks = [{ kind: 'sms' }];
	fakeForm({ 'test-check-kind-0': 'sms', 'test-check-smsda-0': '12345',
		'test-check-smspid-0': '7f', 'test-check-smsdcs-0': '00',
		'test-check-smsudl-0': '0', 'test-check-smsud-0': 'aa??',
		'test-check-fail-0': 'error' });
	testChecksCollect();
	assert.deepStrictEqual(_testEditChecks, [{ kind: 'sms', on_fail: 'error', da: '12345',
		pid: { mode: 'exact', value: '7F' }, dcs: { mode: 'exact', value: '00' }, udl: 0,
		ud: { mode: 'mask', value: 'AA??' } }]);
	// an empty sms check is dropped on save; an alpha with a value survives
	_testEditStep = { type: 'expect', command: 'SEND SHORT MESSAGE', checks: [],
		respond: { result: 'ok' } };
	_testEditChecks = [{ kind: 'sms' }, { kind: 'alpha', mode: 'contains', value: 'Alfa' }];
	fakeForm({ 'test-check-kind-0': 'sms', 'test-check-fail-0': 'error',
		'test-check-kind-1': 'alpha', 'test-check-mode-1': 'contains',
		'test-check-value-1': 'Alfa', 'test-check-fail-1': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.checks,
		[{ kind: 'alpha', on_fail: 'error', mode: 'contains', value: 'Alfa' }]);
});

test('the menu-select editor collects the item id or the text', () => {
	// by text (the id varies with the applet's install parameters)
	_testEditStep = { type: 'action', kind: 'menu-select', params: {} };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	fakeForm({ 'test-step-kind': 'menu-select', 'test-f-item': '', 'test-f-itemtext': 'My menu',
		'test-f-itemmode': 'contains', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params, { text: 'My menu', mode: 'contains' });
	// by id when no text is given
	_testEditStep = { type: 'action', kind: 'menu-select', params: {} };
	fakeForm({ 'test-step-kind': 'menu-select', 'test-f-item': '3', 'test-f-itemtext': '',
		'test-f-itemmode': 'exact', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params, { item_id: 3 });
});

test('the menu-select editor authors the case-sensitive match', () => {
	// the select stores the flag only when set (the server default is
	// case-insensitive), so a step can keep an exact-case match
	_testEditStep = { type: 'action', kind: 'menu-select',
		params: { text: 'My menu', mode: 'contains' } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	fakeForm({ 'test-step-kind': 'menu-select', 'test-f-item': '', 'test-f-itemtext': 'My menu',
		'test-f-itemmode': 'contains', 'test-f-itemcase': 'true',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params,
		{ text: 'My menu', mode: 'contains', case_sensitive: true });
	// the default is omitted from the stored params
	_testEditStep = { type: 'action', kind: 'menu-select',
		params: { text: 'My menu', mode: 'contains' } };
	fakeForm({ 'test-step-kind': 'menu-select', 'test-f-item': '', 'test-f-itemtext': 'My menu',
		'test-f-itemmode': 'contains', 'test-f-itemcase': 'false',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params, { text: 'My menu', mode: 'contains' });
	// the summary flags it
	assert.ok(testStepSummary({ type: 'action', kind: 'menu-select',
		params: { text: 'My menu', case_sensitive: true } }).includes(' (case)'));
	// switching to the item id drops it (there is no text match then)
	_testEditStep = { type: 'action', kind: 'menu-select',
		params: { text: 'My menu', case_sensitive: true } };
	fakeForm({ 'test-step-kind': 'menu-select', 'test-f-item': '3', 'test-f-itemtext': '',
		'test-f-itemmode': 'exact', 'test-f-itemcase': 'true',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params, { item_id: 3 });
});

test('the menu-select step form renders the case select', () => {
	const els = {
		'test-step-modal': { classList: { add: () => {}, remove: () => {} } },
		'test-step-title': {}, 'test-step-body': {},
		'test-step-error': { classList: { add: () => {}, remove: () => {} } },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	_testEditStep = { type: 'action', kind: 'menu-select',
		params: { text: 'My menu', case_sensitive: true } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	testStepRender();
	const body = els['test-step-body'].innerHTML;
	assert.match(body, /id="test-f-itemcase"/);
	assert.match(body, /<option value="true" selected>/);
	// the default selection is case-insensitive
	_testEditStep = { type: 'action', kind: 'menu-select', params: { text: 'My menu' } };
	testStepRender();
	assert.match(els['test-step-body'].innerHTML, /<option value="false" selected>/);
});

test('the envelope step collects the source override', () => {
	_testEditStep = { type: 'action', kind: 'envelope', params: {} };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	fakeForm({ 'test-step-kind': 'envelope', 'test-f-event': '3', 'test-f-data': '9B0100',
		'test-f-eventsrc': '83', 'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.deepStrictEqual(_testEditStep.params, { event: 3, data: '9B0100', src: '83' });
});

test('the event step collects the form values, the built data and the source', () => {
	_testEditStep = { type: 'action', kind: 'event', params: {} };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	// a server-built event: the fields travel for the server's own build
	fakeForm({ 'test-step-kind': 'event', 'test-f-evtype': '03',
		'test-f-ev-status': '0', 'test-f-ev-mcc': '250', 'test-f-ev-mnc': '01',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.event, '03');
	assert.strictEqual(_testEditStep.params.fields.status, '0');
	assert.strictEqual(_testEditStep.params.fields.mcc, '250');
	assert.match(_testEditStep.params.data, /^[0-9A-F]+$/);
	assert.strictEqual(testStepFormError(), '');
	// an event without a server builder (MT call): the built hex + its source
	_testEditStep = { type: 'action', kind: 'event', params: {} };
	fakeForm({ 'test-step-kind': 'event', 'test-f-evtype': '00', 'test-f-ev-ti': '01',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.event, '00');
	assert.strictEqual(_testEditStep.params.data, '1C0101');
	assert.strictEqual(_testEditStep.params.src, '83');
	assert.strictEqual(testStepFormError(), '');
	// a srcField event takes the source from the form (call disconnected)
	_testEditStep = { type: 'action', kind: 'event', params: {} };
	fakeForm({ 'test-step-kind': 'event', 'test-f-evtype': '02', 'test-f-ev-src': '83',
		'test-f-ev-ti': '00', 'test-f-ev-cause_mode': 'rlt',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	testStepCollect();
	assert.strictEqual(_testEditStep.params.src, '83');
	assert.strictEqual(_testEditStep.params.data, '1C01009A00');
	// a bad field pattern refuses the save; an empty build does too
	fakeForm({ 'test-step-kind': 'event', 'test-f-evtype': '00', 'test-f-ev-ti': 'ZZZ',
		'test-f-sw': '', 'test-f-cdata': '', 'test-f-fail': 'error' });
	assert.match(testStepFormError(), /Invalid/);
	// the summary names the event and its source
	assert.strictEqual(testStepSummary({ type: 'action', kind: 'event',
		params: { event: '03', fields: {} } }), 'EVENT Location status');
	assert.strictEqual(testStepSummary({ type: 'action', kind: 'event',
		params: { event: '00', data: '1C0101', src: '83' } }),
		'EVENT MT call src=83');
});

test('the shared event-form helpers render, prefill and collect', () => {
	const cfg = EVENT_FORMS[0x00];
	const htmlOut = eventFormFieldsHtml(cfg, cfg.fields, {ti: 'AB', ton: 1}, 'x-', false);
	assert.match(htmlOut, /id="x-ti"/);
	assert.match(htmlOut, /value="AB"/);
	assert.match(htmlOut, /id="x-ton"/);
	assert.match(htmlOut, /value="1" selected/);
	// a srcField event reports its source; a fixed-source event its own
	assert.strictEqual(eventFormSrc(EVENT_FORMS[0x00], {}), '83');
	assert.strictEqual(eventFormSrc(EVENT_FORMS[0x02], {src: '82'}), '82');
	assert.strictEqual(eventFormSrc(EVENT_FORMS[0x03], {}), '');
	// the pattern check names the first offending field
	assert.strictEqual(eventFormPatternError(cfg, {ti: 'AB'}), '');
	assert.strictEqual(eventFormPatternError(cfg, {ti: 'ZZZ'}), 'Transaction identifier');
});

test('two quick saves of a draft create one script, then update it', async () => {
	// the per-script write chain serializes saves: without it both would see
	// the draft's missing id and create two server-side scripts
	const calls = [];
	globalThis.pysimFetch = async (path) => {
		calls.push(path);
		await new Promise(r => setTimeout(r, 5));
		return { ok: true, script: { id: 'd'.repeat(32) } };
	};
	globalThis.ioStatus = () => {};
	_testScripts = [{ name: 'one', steps: [{ type: 'action', kind: 'status', params: { attempts: 1 } }] }];
	_testScriptId = null;
	globalThis.testCurrentScript = () => _testScripts[0];
	testScriptsSave(true);
	testScriptsSave(true);
	await new Promise(r => setTimeout(r, 40));
	// the create is followed by a suites refetch (the suite gained the member)
	assert.deepStrictEqual(calls, ['/api/test/scripts', '/api/test/suites',
		'/api/test/scripts/update']);
	assert.strictEqual(_testScripts[0].id, 'd'.repeat(32));
});

test('deleting a script cancels a pending debounced save', async () => {
	const calls = [];
	globalThis.pysimFetch = async (path) => { calls.push(path); return {}; };
	globalThis.ioStatus = () => {};
	globalThis.confirm = () => true;
	globalThis.testRender = () => {};
	globalThis.testStoresFetch = async () => {};
	const script = {id: 'e'.repeat(32), name: 'edited',
		steps: [{type: 'action', kind: 'status', params: {attempts: 1}}]};
	_testScripts = [script];
	globalThis.testCurrentScript = () => script;
	globalThis.testScriptById = () => script;
	globalThis.testCurrentSuite = () => ({id: 'a'.repeat(32),
		scripts: [{script_id: script.id, role: 'member', on_fail: 'stop'}]});
	globalThis.testSuiteEntry = () => ({script_id: script.id});
	globalThis.testSuites = () => [];
	testScriptsSave();                       // schedules the 600 ms write
	await testSuiteDeleteScript(0);          // the delete path
	await new Promise(r => setTimeout(r, 700));
	assert.deepStrictEqual(calls, ['/api/test/scripts/delete']);
	assert.strictEqual(script._deleted, true);
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

test('the event step form renders the event select and the Simulator fields', () => {
	const els = {
		'test-step-modal': { classList: { add: () => {}, remove: () => {} } },
		'test-step-title': {}, 'test-step-body': {},
		'test-step-error': { classList: { add: () => {}, remove: () => {} } },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	_testEditStep = { type: 'action', kind: 'event',
		params: { event: '12', fields: { reg_type: '9', mcc: '250' } } };
	_testEditChecks = [];
	_testEditStepIndex = 0;
	testStepRender();
	const body = els['test-step-body'].innerHTML;
	assert.match(body, /id="test-f-evtype"/);
	assert.match(body, /<option value="12" selected>/);
	// the event's own form (EVENT_FORMS) with the stored values prefilled and
	// the built hex preview, not a JSON textarea
	assert.match(body, /id="test-f-ev-reg_type"/);
	assert.match(body, /id="test-f-ev-mcc"/);
	assert.match(body, /value="250"/);
	assert.match(body, /id="test-f-evdata"/);
	assert.doesNotMatch(body, /test-f-evfields/);
	// every modelled event is selectable (the four server-built ones included)
	assert.match(body, /<option value="00"/);
	assert.match(body, /<option value="1F"/);
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

test('the run sends the stored preset id, not the removed flat fields', () => {
	const preset = { id: 'p1', name: 'Card 1',
		keysets: [{ kic: '15', kid: '15', kicKey: 'AA'.repeat(16),
			kidKey: 'BB'.repeat(16), cntr: '0000000A' }],
		tars: [{ role: 'isd', tar: '000000', msl: '16' }] };
	globalThis.cards = [preset];
	globalThis.cardsMatchedPreset = () => preset;
	globalThis.cardsScp80Complete = () => true;
	const found = testRunPreset();
	// the preset body used to carry flat kic/kid/... fields that no longer
	// exist on a stored preset - the server resolved an empty keyset and
	// refused every SCP80 run
	assert.deepStrictEqual(found, { preset_id: 'p1' });
	assert.strictEqual(_testLastPresetIdx, 0);
});

test('the run refuses without a matched or complete preset', () => {
	globalThis.cards = [];
	globalThis.cardsMatchedPreset = () => null;
	assert.match(testRunPreset(true).error, /No card preset matches/);
	const preset = { id: 'p2' };
	globalThis.cards = [preset];
	globalThis.cardsMatchedPreset = () => preset;
	globalThis.cardsScp80Complete = () => false;
	assert.match(testRunPreset(true).error, /incomplete SCP80/);
	// an ADM-only run needs the preset but not the SCP80 completeness
	assert.strictEqual(testRunPreset(false).preset_id, 'p2');
	globalThis.cardsScp80Complete = () => true;
	assert.strictEqual(testRunPreset(true).preset_id, 'p2');
});

test('the client-side problem check bounds the keyset number', () => {
	assert.strictEqual(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { apdu: '80E2', kvn: 2 } }] }), '');
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { apdu: '80E2', kvn: 16 } }] }), /keyset number/);
	assert.match(testScriptProblem({ steps: [{ type: 'action', kind: 'scp80',
		params: { apdu: '80E2', kvn: 0 } }] }), /keyset number/);
});

test('the PoR and file-list checks render and collect', () => {
	const els = { 'test-checks': {} };
	globalThis.document = { getElementById: id => els[id] || null };
	_testEditChecks = [{ kind: 'por', status: 'por_ok',
		sw: { mode: 'mask', value: '6A8?' }, on_fail: 'error' }];
	testRenderChecks();
	const rendered = els['test-checks'].innerHTML;
	assert.ok(rendered.includes('value="por" selected'), rendered);
	assert.match(rendered, /id="test-check-porstatus-0"/);
	assert.match(rendered, /id="test-check-por-sw-0"/);

	const values = {
		'test-check-kind-0': 'por',
		'test-check-porstatus-0': 'por_ok',
		'test-check-por-sw-0': '6A8?',
		'test-check-por-data-0': 'aabb',
		'test-check-fail-0': 'warning',
	};
	globalThis.document = { getElementById: id => (id in values ? { value: values[id] } : null) };
	testChecksCollect();
	assert.deepStrictEqual(_testEditChecks[0], {
		kind: 'por', on_fail: 'warning', status: 'por_ok',
		sw: { mode: 'mask', value: '6A8?' },
		data: { mode: 'exact', value: 'AABB' },
	});

	_testEditChecks = [{ kind: 'files', files: ['3F007F106F3A'], on_fail: 'error' }];
	globalThis.document = { getElementById: id => els[id] || null };
	testRenderChecks();
	assert.match(els['test-checks'].innerHTML, /id="test-check-files-0"/);
	const values2 = { 'test-check-kind-0': 'files',
		'test-check-files-0': '3F007F106F3A, 3f002fe2', 'test-check-fail-0': 'error' };
	globalThis.document = { getElementById: id => (id in values2 ? { value: values2[id] } : null) };
	testChecksCollect();
	assert.deepStrictEqual(_testEditChecks[0],
		{ kind: 'files', on_fail: 'error', files: ['3F007F106F3A', '3F002FE2'] });
});

test('the check summary names the PoR and file-list checks', () => {
	assert.strictEqual(testCheckSummary({ kind: 'por', status: 'por_ok',
		sw: { mode: 'mask', value: '6A8?' }, data: { mode: 'exact', value: 'AABB' } }),
		'por status por_ok SW ~6A8? data AABB');
	assert.strictEqual(testCheckSummary({ kind: 'files', files: ['3F002FE2'] }),
		'files 3F002FE2');
});

test('a new script is created in the server store and gets its id', async () => {
	const calls = [];
	globalThis.pysimFetch = async (path, body) => {
		calls.push([path, body]);
		return {ok: true, script: {id: 'a'.repeat(32)}};
	};
	globalThis.ioStatus = () => {};
	const s = {name: 'new', steps: [{type: 'action', kind: 'status', params: {attempts: 1}}]};
	await testScriptsWriteback(s);
	assert.strictEqual(calls[0][0], '/api/test/scripts');
	assert.strictEqual(calls[0][1].name, 'new');
	assert.strictEqual(s.id, 'a'.repeat(32));      // the store id is adopted
	assert.strictEqual(calls[1][0], '/api/test/suites');   // the member refetch
	// every later write updates by id
	await testScriptsWriteback(s);
	assert.strictEqual(calls[2][0], '/api/test/scripts/update');
	assert.strictEqual(calls[2][1].id, 'a'.repeat(32));
});

test('an incomplete draft is not written to the store', async () => {
	const calls = [];
	globalThis.pysimFetch = async (path) => { calls.push(path); return {}; };
	globalThis.ioStatus = () => {};
	await testScriptsWriteback({name: 'draft', steps: []});
	await testScriptsWriteback({name: 'draft', steps: [{type: 'action'}]});
	assert.deepStrictEqual(calls, []);
});

test('an immediate save writes the current script through', async () => {
	const calls = [];
	globalThis.pysimFetch = async (path) => { calls.push(path); return {ok: true, script: {id: 'c'.repeat(32)}}; };
	globalThis.ioStatus = () => {};
	_testScripts = [{name: 'one', steps: [{type: 'action', kind: 'status', params: {attempts: 1}}]}];
	_testScriptId = null;
	globalThis.testCurrentScript = () => _testScripts[0];
	testScriptsSave(true);
	await new Promise(r => setTimeout(r, 0));      // the writeback is async
	assert.deepStrictEqual(calls, ['/api/test/scripts', '/api/test/suites']);
	assert.strictEqual(_testScripts[0].id, 'c'.repeat(32));
});

test('a store failure is reported, not swallowed', async () => {
	let msg = '';
	globalThis.pysimFetch = async () => ({error: 'read-only file system'});
	globalThis.ioStatus = (id, text) => { msg = text; };
	await testScriptsWriteback({name: 'x', steps: [{type: 'action', kind: 'status', params: {}}]});
	assert.match(msg, /read-only file system/);
});

test('the run sends the stored script id with the edited script', async () => {
	let sent = null;
	globalThis.pysimFetch = async (path, body) => {
		sent = {path, body};
		return {running: true, name: 'saved', index: 0, total: 1, steps: [], status: null};
	};
	globalThis.testCurrentScript = () => ({id: 'b'.repeat(32), name: 'saved',
		steps: [{type: 'action', kind: 'status', params: {attempts: 1}}]});
	globalThis.testRunPreset = () => { throw new Error('no preset for a status-only script'); };
	globalThis.testRunMessage = () => {};
	globalThis.testRenderRun = () => {};
	globalThis.testStartRunTimer = () => {};
	globalThis.pysimApplyAvailability = () => {};
	await testRunStart();
	assert.strictEqual(sent.path, '/api/test/run');
	assert.strictEqual(sent.body.script_id, 'b'.repeat(32));
	assert.strictEqual(sent.body.script.name, 'saved');
	assert.strictEqual(sent.body.preset_id, undefined);
});
