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
	'tarProbeRowsHtml', 'tarProbeCustomLoad', 'tarProbeCustomSave',
	'tarProbeAllRows', 'tarProbeChecked']) {
	code += extractFunc(html, fn) + '\n';
}
code += "var TAR_PROBE_TARS = JSON.parse('" +
	JSON.stringify(eval('(' + html.match(/const TAR_PROBE_TARS = \[[\s\S]*?\n\];/)[0]
		.replace('const TAR_PROBE_TARS = ', '').replace(/;$/, '') + ')')) + "');\n";
code += "var TAR_PROBE_STORAGE = 'simple_tar_probe';\n";
code += 'function t(s){return s;}\n';
eval(code);

function fakeStorage() {
	const map = {};
	globalThis.localStorage = {
		getItem: k => (k in map ? map[k] : null),
		setItem: (k, v) => { map[k] = String(v); },
	};
	return map;
}

test('the standard list covers the Annex D allocations', () => {
	const tars = TAR_PROBE_TARS.map(e => e.tar);
	assert.ok(tars.length >= 10, tars.length);
	for (const want of ['000000', 'B20100', 'B00000', 'B00001', 'B00010',
		'B00120', 'B00130', 'B00140', 'B20000', 'B20200', 'B20201', 'B00200']) {
		assert.ok(tars.includes(want), want);
	}
	// every row carries the spec document it comes from
	TAR_PROBE_TARS.forEach(e => {
		assert.match(e.tar, /^[0-9A-F]{6}$/);
		assert.ok(e.label, e.tar);
	});
});

test('tarProbeRowsHtml renders checked rows and marks custom ones', () => {
	const out = tarProbeRowsHtml([
		{ tar: '000000', label: 'Issuer Security Domain (compact)', doc: 'TS 102 226' },
		{ tar: '123456', label: 'my applet', custom: true },
	]);
	assert.match(out, /value="000000" checked/);
	assert.match(out, /title="TS 102 226"/);
	assert.match(out, /Issuer Security Domain \(compact\)/);
	assert.match(out, /value="123456" checked/);
	assert.match(out, /tarProbeRemoveCustom\('123456'\)/, 'custom rows get a remove button');
	assert.ok(!/tarProbeRemoveCustom\('000000'\)/.test(out), 'standard rows have none');
	// escaping
	const evil = tarProbeRowsHtml([{ tar: 'AA<BB', label: '<i>x', custom: true }]);
	assert.ok(!evil.includes('AA<BB'));
	assert.ok(!evil.includes('<i>x'));
});

test('custom TARs round-trip through localStorage (best effort)', () => {
	fakeStorage();
	assert.deepStrictEqual(tarProbeCustomLoad(), []);
	tarProbeCustomSave([{ tar: '123456', label: 'mine' }, { tar: 'aabbcc' }]);
	const back = tarProbeCustomLoad();
	assert.deepStrictEqual(back, [{ tar: '123456', label: 'mine', custom: true },
		{ tar: 'AABBCC', label: '', custom: true }]);
	// garbage is dropped, never fatal
	localStorage.setItem('simple_tar_probe', '{not json');
	assert.deepStrictEqual(tarProbeCustomLoad(), []);
	localStorage.setItem('simple_tar_probe', JSON.stringify([{ tar: 'zz' }, { tar: 'B0B0B0' }]));
	assert.deepStrictEqual(tarProbeCustomLoad().map(e => e.tar), ['B0B0B0']);
});

test('tarProbeAllRows appends the custom TARs to the standard ones', () => {
	fakeStorage();
	tarProbeCustomSave([{ tar: '123456', label: 'mine' }]);
	const rows = tarProbeAllRows();
	assert.strictEqual(rows.length, TAR_PROBE_TARS.length + 1);
	assert.strictEqual(rows[rows.length - 1].tar, '123456');
	assert.strictEqual(rows[rows.length - 1].custom, true);
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
	// its own preset + keyset selectors and the warning banner
	assert.match(html, /id="tar-card-sel" onchange="tarApplyCard\(this.value\)"/);
	assert.match(html, /id="tar-keyset-sel" onchange="tarKeysetChanged\(\)"/);
	assert.match(html, /id="tar-preset-warning"/);
	// the checklist, the add form, the APDU and the Execute button
	assert.match(html, /id="tar-list"/);
	assert.match(html, /onclick="tarProbeSelectAll\(true\)"/);
	assert.match(html, /onclick="tarProbeAddCustom\(\)"/);
	assert.match(html, /id="tar-apdu"[^>]*value="00A40000023F00"/);
	assert.match(html, /onclick="tarExecute\(\)"/);
	assert.match(html, /id="tar-result"/);
	// the run
	assert.ok(html.includes("pysimFetch('/api/tar-probe', body)"));
	assert.ok(html.includes('tars: tars'));
	assert.ok(html.includes("getRamSpParams('tar-card-sel')"));
	assert.ok(html.includes('if (data.final_cntr) ramSaveCntr(data.final_cntr);'));
	// the RAM-era leftovers are gone
	assert.ok(!html.includes('id="ram-tar-rows"'));
	assert.ok(!html.includes('id="ram-tar-list"'));
	assert.ok(!html.includes("op === 'tar-probe'"));
	assert.ok(!html.includes('function ramTarProbe'));
});
