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
for (const fn of ['esc', 'escHtml', 'ramTarVerdictLabel', 'ramTarProbeHtml']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'function t(s){return s;}\n';
eval(code);

const DATA = {
	success: true,
	registered: 2,
	total: 4,
	final_cntr: '0000000187',
	results: [
		{ tar: '000000', label: 'Issuer Security Domain (compact)', verdict: 'registered',
		  sw: '9000', por_status: 'por_ok', por_sw: '6D00', por_data: '' },
		{ tar: 'B00000', label: 'UICC shared file system RFM (compact)', verdict: 'registered',
		  sw: '9000', por_status: 'por_ok', por_sw: '6B00', por_data: '3F00' },
		{ tar: 'B00001', label: 'ADF RFM (compact)', verdict: 'no_por',
		  sw: '9000', por_status: 'no_por', por_sw: null, por_data: null },
		{ tar: 'B00200', label: 'RFU (control)', verdict: 'refused',
		  sw: '6200', por_status: 'envelope_error', por_sw: null, por_data: null },
	],
};

test('ramTarVerdictLabel names every verdict', () => {
	assert.strictEqual(ramTarVerdictLabel('registered'), 'registered');
	assert.strictEqual(ramTarVerdictLabel('no_answer'), 'accepted, no answer');
	assert.strictEqual(ramTarVerdictLabel('no_por'), 'accepted, no PoR');
	assert.strictEqual(ramTarVerdictLabel('refused'), 'refused');
	assert.strictEqual(ramTarVerdictLabel('something'), 'something');
});

test('ramTarProbeHtml renders the summary and one row per TAR', () => {
	const out = ramTarProbeHtml(DATA);
	assert.match(out, /Registered applications: <b>2 \/ 4<\/b>/);
	assert.match(out, /counter 0000000187/);
	for (const r of DATA.results) {
		assert.ok(out.includes(r.tar), r.tar);
	}
	assert.ok(out.includes('6D00') && out.includes('6B00'), 'the application SWs are shown');
	assert.ok(out.includes('6200'), 'the refused ENVELOPE SW is shown');
	// the verdict colours: registered = green, no_por = amber, refused = red
	assert.match(out, /text-emerald-700 dark:text-emerald-400">registered/);
	assert.match(out, /text-amber-600 dark:text-amber-400">accepted, no PoR/);
	assert.match(out, /text-red-600 dark:text-red-400">refused/);
	// an empty payload renders nothing
	assert.strictEqual(ramTarProbeHtml(null), '');
	assert.strictEqual(ramTarProbeHtml({}), '');
});

test('the probe escapes what the card returned', () => {
	const out = ramTarProbeHtml({ results: [
		{ tar: '<b>x', label: '<i>y', verdict: 'registered', sw: '<', por_sw: '>', por_data: '&' },
	], registered: 1, total: 1 });
	assert.ok(!out.includes('<b>x'), 'the TAR must be escaped');
	assert.ok(!out.includes('<i>y'), 'the label must be escaped');
});

test('the RAM form wires the TAR probe', () => {
	assert.match(html, /<option value="tar-probe" data-l10n="Probe TARs \(registered OTA applications\)">/);
	assert.match(html, /id="ram-tar-rows" class="hidden mb-3"/);
	assert.match(html, /id="ram-tar-apdu"[^>]*value="00A40000023F00"/);
	assert.match(html, /id="ram-tar-list"/);
	assert.ok(html.includes("setHidden('ram-tar-rows', !isProbe);"));
	assert.ok(html.includes("else if (op === 'tar-probe') await ramTarProbe(sp);"));
	assert.ok(html.includes("pysimFetch('/api/tar-probe', body)"));
	assert.ok(html.includes("if (data.final_cntr) ramSaveCntr(data.final_cntr);"));
});
