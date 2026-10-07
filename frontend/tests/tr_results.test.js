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
eval(extractBlock('const TR_RESULT_ADD_INFO = [', 'const TR_RESULT_ALIASES = {').replace(/^const /gm, 'var '));
eval(extractBlock('const TR_RESULT_ALIASES = {', 'function trResultOptions').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'esc'));
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
});

test('trResultRowHtml renders the manual entry and the additional info', () => {
	const out = trResultRowHtml('test-f-result', '07', '04');
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
});
