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
for (const fn of ['esc', 'escHtml', 'tarProbeVerdictLabel', 'tarProbeHtml',
	'tarProbeRowsHtml', 'tarProbeAllRows', 'tarProbeChecked', 'cardsTarList']) {
	code += extractFunc(html, fn) + '\n';
}
code += 'function t(s){return s;}\n';
eval(code);

test('tarProbeRowsHtml renders the preset rows checked', () => {
	// v3.26.0: the rows come from the preset - no custom rows, no remove
	// buttons
	const out = tarProbeRowsHtml([
		{ tar: '000000', label: 'ISD', doc: 'MSL 16 \u00b7 Keyset 1' },
		{ tar: 'AF4D02', label: 'My applet' },
	]);
	assert.match(out, /value="000000" checked/);
	assert.match(out, /title="MSL 16/);
	assert.match(out, /ISD/);
	assert.match(out, /value="AF4D02" checked/);
	assert.ok(!out.includes('tarProbeRemoveCustom'), 'no custom rows anymore');
	// escaping
	const evil = tarProbeRowsHtml([{ tar: 'AA<BB', label: '<i>x' }]);
	assert.ok(!evil.includes('AA<BB'));
	assert.ok(!evil.includes('<i>x'));
});

test('tarProbeAllRows lists the selected preset TARs (v3.26.0)', () => {
	globalThis.cards = [{ name: 'C', tars: [
		{ role: 'isd', tar: '000000', msl: '16', keyset: 1 },
		{ tar: 'AF4D02', msl: '0A', desc: 'My applet' }] }];
	globalThis.tarPresetIdx = () => 0;
	assert.deepStrictEqual(tarProbeAllRows(), [
		{ tar: '000000', label: 'ISD', doc: 'MSL 16 \u00b7 Keyset 1' },
		{ tar: 'AF4D02', label: 'My applet', doc: 'MSL 0A' },
	]);
	// no preset: no rows
	globalThis.tarPresetIdx = () => -1;
	assert.deepStrictEqual(tarProbeAllRows(), []);
	delete globalThis.tarPresetIdx;
});

test('tarProbeChecked reads the checked boxes in list order', () => {
	const boxes = [
		{ value: '000000', checked: true },
		{ value: 'B00000', checked: false },
		{ value: 'b00120', checked: true },
	];
	globalThis.document = { querySelectorAll: () => boxes };
	assert.deepStrictEqual(tarProbeChecked(), ['000000', 'B00120']);
	boxes.forEach(b => { b.checked = false; });
	assert.deepStrictEqual(tarProbeChecked(), []);
});

test('tarProbeVerdictLabel names every verdict', () => {
	assert.strictEqual(tarProbeVerdictLabel('registered'), 'registered');
	assert.strictEqual(tarProbeVerdictLabel('no_answer'), 'accepted, no answer');
	assert.strictEqual(tarProbeVerdictLabel('no_por'), 'accepted, no PoR');
	assert.strictEqual(tarProbeVerdictLabel('refused'), 'refused');
	assert.strictEqual(tarProbeVerdictLabel('something'), 'something');
});

test('tarProbeHtml renders the summary and one row per TAR', () => {
	const out = tarProbeHtml({
		success: true, registered: 2, total: 4, final_cntr: '0000000187',
		results: [
			{ tar: '000000', label: 'ISD', verdict: 'registered', sw: '9000', por_sw: '6D00', por_data: '' },
			{ tar: 'B00000', label: 'RFM', verdict: 'registered', sw: '9000', por_sw: '6B00', por_data: '3F00' },
			{ tar: 'B00001', label: 'ADF', verdict: 'no_por', sw: '9000' },
			{ tar: 'B00200', label: 'RFU', verdict: 'refused', sw: '6200' },
		],
	});
	assert.match(out, /Registered applications: <b>2 \/ 4<\/b>/);
	assert.match(out, /counter 0000000187/);
	assert.match(out, /text-emerald-700 dark:text-emerald-400">registered/);
	assert.match(out, /text-amber-600 dark:text-amber-400">accepted, no PoR/);
	assert.match(out, /text-red-600 dark:text-red-400">refused/);
	assert.ok(out.includes('6D00') && out.includes('6200'));
	assert.strictEqual(tarProbeHtml(null), '');
});

test('the SCP80 tab wires the TAR probe pill', () => {
	assert.match(html, /data-scp80-sub="tar" onclick="scp80SwitchSubtab\('tar'\)" data-l10n="TAR probe"/);
	assert.match(html, /<div id="scp80-sub-tar" class="hidden">/);
	assert.ok(html.includes("document.getElementById('scp80-sub-tar').classList.toggle('hidden', name !== 'tar');"));
	assert.ok(html.includes("tar: 'tar-probe'"), 'the help anchor points at the new section');
	// its own preset selector and the warning banner; v3.26.0: the probe
	// checks the preset's TAR list - no keyset selector, no custom TARs
	assert.match(html, /id="tar-card-sel" onchange="tarApplyCard\(this.value\)"/);
	assert.match(html, /id="tar-preset-warning"/);
	assert.ok(!html.includes('id="tar-keyset-sel"'), 'the probe keyset selector is gone');
	assert.ok(!html.includes('tarProbeAddCustom'), 'no arbitrary TARs anymore');
	assert.match(html, /TARs to probe \(the preset's TAR list\)/);
	// the checklist, the APDU and the Execute button
	assert.match(html, /id="tar-list"/);
	assert.match(html, /onclick="tarProbeSelectAll\(true\)"/);
	assert.match(html, /id="tar-apdu"[^>]*value="00A40000023F00"/);
	assert.match(html, /onclick="tarExecute\(\)"/);
	assert.match(html, /id="tar-result"/);
	// the run: the preset and the checked TARs travel - the server resolves
	// each row's keyset
	assert.ok(html.includes("pysimFetch('/api/tar-probe', body)"));
	assert.ok(html.includes('tars: tars'));
	assert.ok(!html.includes("getRamSpParams('tar-card-sel')"));
	assert.ok(html.includes('if (data.final_cntr) ramSaveCntr(data.final_cntr);'));
	// the RAM-era leftovers are gone
	assert.ok(!html.includes('id="ram-tar-rows"'));
	assert.ok(!html.includes('id="ram-tar-list"'));
	assert.ok(!html.includes("op === 'tar-probe'"));
	assert.ok(!html.includes('function ramTarProbe'));
});
