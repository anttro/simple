const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'tr_results.json'), 'utf8'));

function extractBlock(startMarker, endMarker) {
	const start = html.indexOf(startMarker);
	const end = html.indexOf(endMarker, start);
	if (start < 0 || end < 0) throw new Error('block not found: ' + startMarker);
	return html.slice(start, end);
}

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

// Rewrite top-level const -> var so the tables leak out of sloppy-mode eval.
eval(extractBlock('const TR_RESULTS = [', 'const TR_RESULT_ADD_INFO = [').replace(/^const /gm, 'var '));
eval(extractBlock('const TR_RESULT_ADD_INFO = [', '// TS 102 223 v18.3.0 Tables 6.1/6.2').replace(/^const /gm, 'var '));
eval(extractBlock('const TR_RESULT_BY_COMMAND = {', 'const TR_RESULT_ALIASES = {').replace(/^const /gm, 'var '));
eval(extractBlock('const TR_RESULT_ALIASES = {', 'function trCommandCode').replace(/^const /gm, 'var '));
eval(extractBlock('const CMD_NAMES = {', 'function cmdQualifierShort').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'esc'));
eval(extractFunc(html, 'trCommandCode'));
eval(extractFunc(html, 'trResultOptions'));
eval(extractFunc(html, 'trResultFormValue'));
eval(extractFunc(html, 'trResultValid'));
eval(extractFunc(html, 'trResultCollect'));
eval(extractFunc(html, 'testTrResultChanged'));
eval(extractFunc(html, 'testFormInput'));
eval(extractFunc(html, 'testFormSelect'));
eval(extractFunc(html, 'trResultRowHtml'));

globalThis.t = s => s;

const fixtureResults = fixture.results.map(r => {
	const o = { v: r.v, name: r.name };
	if (r.alias) o.alias = r.alias;
	return o;
});

test('the PWA result tables match the shared fixture', () => {
	assert.deepStrictEqual(TR_RESULTS, fixtureResults);
	assert.deepStrictEqual(TR_RESULT_ADD_INFO, fixture.additional_info);
	assert.deepStrictEqual(TR_RESULT_ALIASES, fixture.aliases);
});

test('the corrected aliases carry the TS 102 223 8.12.0 values', () => {
	assert.strictEqual(TR_RESULT_ALIASES.refused, '22');
	assert.strictEqual(TR_RESULT_ALIASES.not_understood, '32');
	assert.strictEqual(TR_RESULT_ALIASES.modified, '07');
	assert.strictEqual(TR_RESULT_ALIASES.no_response, '12');
	assert.strictEqual(TR_RESULT_ALIASES.ok, '00');
});

test('trResultOptions labels every entry with its hex value', () => {
	const opts = trResultOptions();
	// the full list plus the manual entry
	assert.strictEqual(opts.length, TR_RESULTS.length + 1);
	assert.deepStrictEqual(opts[0], { v: 'ok', l: '00 - ok' });
	const byValue = {};
	opts.forEach(o => { byValue[o.v] = o.l; });
	assert.strictEqual(byValue.modified, '07 - modified');
	assert.strictEqual(byValue['09'], '09 - Command performed successfully, tone not played');
	assert.strictEqual(byValue['20'], '20 - Terminal currently unable to process command');
	assert.strictEqual(byValue['3F'], '3F - Reserved for 3GPP (for future usage)');
	assert.deepStrictEqual(opts[opts.length - 1], { v: 'manual', l: 'manual input' });
	// the compat-only alias is not offered (12's canonical alias is timeout)
	assert.ok(!('no_response' in byValue), 'no_response must not be an option');
});

test('trResultFormValue maps the stored alias/hex/manual values', () => {
	assert.deepStrictEqual(trResultFormValue('ok'), { value: 'ok', manual: '' });
	assert.deepStrictEqual(trResultFormValue('OK'), { value: 'ok', manual: '' });
	assert.deepStrictEqual(trResultFormValue('07'), { value: 'modified', manual: '' });
	assert.deepStrictEqual(trResultFormValue('0x09'), { value: '09', manual: '' });
	assert.deepStrictEqual(trResultFormValue('1a'), { value: 'manual', manual: '1A' });
	assert.deepStrictEqual(trResultFormValue(''), { value: '', manual: '' });
	assert.deepStrictEqual(trResultFormValue(undefined), { value: '', manual: '' });
	assert.deepStrictEqual(trResultFormValue('bogus'), { value: 'ok', manual: '' });
	// a numeric stored value is the value, not hex (the engine reads ints)
	assert.deepStrictEqual(trResultFormValue(7), { value: 'modified', manual: '' });
	assert.deepStrictEqual(trResultFormValue(16), { value: 'cancel', manual: '' });
	assert.deepStrictEqual(trResultFormValue(10), { value: 'manual', manual: '0A' });
	assert.deepStrictEqual(trResultFormValue(0), { value: 'ok', manual: '' });
});

test('trResultValid accepts the aliases and hex bytes only', () => {
	['ok', 'refused', '07', '0x0a', 'FF'].forEach(v => assert.ok(trResultValid(v), v));
	['manual', '', 'f', 'zzz'].forEach(v => assert.ok(!trResultValid(v), v));
});

test('the step editor offers the full list for all three result fields', () => {
	const form = extractFunc(html, 'testStepRender');
	assert.ok(form.includes("trResultRowHtml('test-f-result'"), 'the expectation result');
	assert.ok(form.includes("trResultRowHtml('test-f-presult'"), 'the drain result');
	assert.ok(form.includes("trResultRowHtml('test-f-pfirst'"), 'the drain first');
	assert.ok(form.includes("{empty: 'same as above'}"), 'the first keeps its empty option');
	// the suggestion list is rendered by the first result row of each form
	// (the drain's respond and the expectation), never twice in one form
	assert.strictEqual((form.match(/\{list: true/g) || []).length, 2, form);
});

test('trResultRowHtml renders the manual entry and the additional info', () => {
	const out = trResultRowHtml('test-f-result', '07', '04', { list: true });
	assert.ok(out.includes('07 - modified'), out);
	assert.ok(out.includes('value="04"'), out);
	assert.ok(out.includes('id="tr-addinfo-list"'), out);
	assert.ok(out.includes('No specific cause can be given'), out);
	assert.ok(out.includes('id="test-f-result-hexwrap" class="w-24 hidden"'), out);
	const manual = trResultRowHtml('test-f-result', '1A', '');
	assert.ok(manual.includes('id="test-f-result-hexwrap" class="w-24"'), manual);
	assert.ok(manual.includes('value="1A"'), manual);
	assert.ok(manual.includes('<option value="manual" selected>'), manual);
	// a server-normalised (integer) additional info shows as hex
	const intAdd = trResultRowHtml('test-f-result', '07', 10);
	assert.ok(intAdd.includes('value="0A"'), intAdd);
	// the suggestion list is rendered once per form: only the row with the
	// flag carries it (a form may have several result rows)
	assert.ok(!manual.includes('id="tr-addinfo-list"'), manual);
	assert.ok(!intAdd.includes('id="tr-addinfo-list"'), intAdd);
	const twoRows = trResultRowHtml('test-f-presult', 'ok', '', { list: true })
		+ trResultRowHtml('test-f-pfirst', '', '', { empty: 'same as above' });
	assert.strictEqual((twoRows.match(/id="tr-addinfo-list"/g) || []).length, 1, twoRows);
});

test('the command/result grouping table matches the parsed spec fixture', () => {
	const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'tr_result_by_command.json'), 'utf8'));
	assert.deepStrictEqual(TR_RESULT_BY_COMMAND, fixture.commands);
	// the editor's command names resolve to their type codes (the first code
	// wins, matching testCommandOptions' de-duplication)
	assert.strictEqual(trCommandCode('REFRESH'), 0x01);
	assert.strictEqual(trCommandCode('PLAY TONE'), 0x20);
	assert.strictEqual(trCommandCode('TIMER MANAGEMENT'), 0x27);
	assert.strictEqual(trCommandCode('OPEN CHANNEL'), 0x40);
	assert.ok(Number.isNaN(trCommandCode('ANY')));
	assert.ok(Number.isNaN(trCommandCode('NONSENSE')));
	// every mapped code is a known proactive command
	for (const code of Object.keys(TR_RESULT_BY_COMMAND)) {
		assert.ok(CMD_NAMES[code], 'the mapped code 0x' + code + ' must be a known command');
	}
	// independent semantic pins (from the spec's command clauses): a column
	// shift in the parsed table would break these
	const has = (cmd, res) => TR_RESULT_BY_COMMAND[cmd].indexOf(res) >= 0;
	assert.ok(has('01', '03') && has('01', '08'), 'REFRESH: additional EFs / NAA not active');
	assert.ok(has('20', '09'), 'PLAY TONE: tone not played');
	assert.ok(!has('01', '09') && !has('20', '03'), 'the command-specific values do not leak');
	assert.ok(has('27', '24'), 'TIMER MANAGEMENT: timer state');
	assert.ok(has('15', '26'), 'LAUNCH BROWSER: generic error');
	assert.ok(has('40', '28') && has('40', '3A') && has('40', '3B'), 'OPEN CHANNEL: BIP / access technology');
	assert.ok(has('60', '27') && has('60', '3D'), 'the MMS commands: MMS errors');
	assert.ok(has('33', '38'), 'GET READER STATUS: MultipleCard');
	// the spec's tables omit SEND SS / SEND USSD / SEND SHORT MESSAGE /
	// GEOGRAPHICAL LOCATION REQUEST / End of the proactive UICC session -
	// those keep the flat list (the fallback is checked in the grouping test)
	assert.ok(trCommandCode('End of the proactive UICC session') === 0x81,
		'a mixed-case command name must still resolve');
});

test('trResultOptions groups the list by the selected command', () => {
	const flat = trResultOptions();
	assert.strictEqual(flat.filter(o => o.group).length, 0, 'no groups without a command');
	assert.strictEqual(flat.length, TR_RESULTS.length + 1);
	assert.strictEqual(trResultOptions({ command: 'ANY' }).filter(o => o.group).length, 0,
		'ANY stays flat');
	// a command the spec's tables omit keeps the flat list too
	assert.strictEqual(trResultOptions({ command: 'SEND SS' }).filter(o => o.group).length, 0,
		'SEND SS (not in the tables) stays flat');
	const grouped = trResultOptions({ command: 'REFRESH' });
	assert.deepStrictEqual(grouped.filter(o => o.group).map(o => o.group),
		['relevant for REFRESH', 'all other results']);
	// the first group carries the spec's REFRESH set in the spec order
	const split = grouped.findIndex(o => o.group === 'all other results');
	const first = grouped.slice(1, split);
	assert.deepStrictEqual(first.map(o => o.v),
		TR_RESULT_BY_COMMAND['01'].map(hex => (TR_RESULTS.find(r => r.v === hex) || {}).alias || hex));
	// nothing is hidden: both groups together are the full list + the manual
	// entry, and the stored value always matches an option
	const values = grouped.filter(o => !o.group).map(o => o.v).sort();
	const all = TR_RESULTS.map(r => r.alias || r.v).concat(['manual']).sort();
	assert.deepStrictEqual(values, all);
});

test('testFormSelect renders the optgroups', () => {
	const out = testFormSelect('x', 'ok', [{ group: 'relevant for REFRESH' }, { v: 'ok', l: '00 - ok' },
		{ group: 'all other results' }, { v: '1F', l: '1F - Slices status change' }]);
	assert.ok(out.includes('<optgroup label="relevant for REFRESH">'), out);
	assert.ok(out.includes('<optgroup label="all other results">'), out);
	assert.strictEqual((out.match(/<optgroup/g) || []).length, 2, out);
	assert.strictEqual((out.match(/<\/optgroup>/g) || []).length, 2, out);
	assert.ok(out.includes('<option value="ok" selected>00 - ok</option>'), out);
	// the flat form is unchanged
	assert.ok(!testFormSelect('y', 'ok', [{ v: 'ok', l: '00 - ok' }]).includes('optgroup'));
});

test('the expectation result row passes the selected command', () => {
	const form = extractFunc(html, 'testStepRender');
	assert.ok(/trResultRowHtml\('test-f-result'[\s\S]*?command: step\.command \|\| 'ANY'/.test(form),
		'the expectation result must group by the selected command');
});
