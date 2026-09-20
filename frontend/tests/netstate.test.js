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
for (const fn of ['netStateStatusWord', 'netStateFlattenTitle', 'netStatePlmnList',
	'netStateSummary', 'netStateRender']) {
	code += extractFunc(html, fn) + '\n';
}
const CONSTS = {
	NET_STATE_FILES: ['[', '];'],
	NET_STATE_STATUS: ['{', '};'],
	NET_STATE_AREA: ['{', '};'],
	NET_STATE_ROAMING: ['{', '};'],
	NET_STATE_SERVICE: ['{', '};'],
	NET_STATE_SOURCE: ['{', '};'],
};
for (const [name, [open, close]] of Object.entries(CONSTS)) {
	const re = new RegExp('const ' + name + ' = \\' + open + '[\\s\\S]*?\\' + close);
	const m = re.exec(html);
	if (!m) throw new Error('const ' + name + ' not found');
	code += m[0].replace(/^const /, 'var ') + '\n';
}
code += 'globalThis.t = s => s;\n';
code += 'globalThis.esc = s => String(s);\n';
code += 'globalThis.efFlatten = d => Object.entries(d || {}).map(([k, v]) => [k, String(v)]);\n';
eval(code);

function setup() {
	const els = {};
	for (const id of ['netstate-badge', 'netstate-location',
		'netstate-read', 'netstate-body']) {
		els[id] = { className: '', textContent: '', innerHTML: '' };
	}
	globalThis.document = { getElementById: id => els[id] || null };
	return els;
}

function decoder(map) {
	globalThis.efDecodeFile = (name, fid, content) =>
		map[name] ? { data: map[name] } : null;
}

test('netStateSummary renders decoded file contents compactly', () => {
	setup();
	decoder({
		'EF.LOCI': { tmsi: '3E905D6B', mcc: '262', mnc: '01', lac: '0x6CD7',
			tmsi_time: '0xFF', location_update_status: '0x00' },
		'EF.FPLMN': { plmns: [{ mcc: '262', mnc: '01' }, { mcc: '246', mnc: '81' }] },
		'EF.HPLMNwAcT': { plmns: [{ mcc: '262', mnc: '01', access_tech: 'LTE' }] },
	});
	const loci = netStateSummary('loci', { present: true, kind: 'transparent', data: 'AA' });
	assert.strictEqual(loci.text, 'LAI 262-01/6CD7 · updated');
	assert.ok(loci.title.includes('3E905D6B'));
	const fplmn = netStateSummary('fplmn', { present: true, kind: 'transparent', data: 'AA' });
	assert.strictEqual(fplmn.text, '262-01, 246-81');
	const hplmn = netStateSummary('hplmnwact', { present: true, kind: 'transparent', data: 'AA' });
	assert.strictEqual(hplmn.text, '262-01 (LTE)');
	const absent = netStateSummary('loci', { present: false });
	assert.strictEqual(absent.text, '—');
	assert.strictEqual(absent.title, 'not present');
});

test('netStateSummary marks rejection statuses', () => {
	setup();
	decoder({
		'EF.LOCI': { mcc: '246', mnc: '81', lac: '0xFFFE', location_update_status: '0x02' },
	});
	const loci = netStateSummary('loci', { present: true, kind: 'transparent', data: 'AA' });
	assert.strictEqual(loci.text, 'LAI 246-81/FFFE · PLMN not allowed');
});

test('netStatePlmnList limits long lists with a count', () => {
	const d = { plmns: [
		{ mcc: '262', mnc: '01' }, { mcc: '246', mnc: '81' },
		{ mcc: '204', mnc: '04' }, { mcc: '234', mnc: '15' },
	] };
	assert.strictEqual(netStatePlmnList(d, false, 3), '262-01, 246-81, 204-04 … +1');
	assert.strictEqual(netStatePlmnList(d, false), '262-01, 246-81, 204-04, 234-15');
});

test('netStateSummary abbreviates a long HPLMNwAcT list', () => {
	setup();
	decoder({
		'EF.HPLMNwAcT': { plmns: [
			{ mcc: '250', mnc: '99', access_tech: 'LTE' },
			{ mcc: '250', mnc: '32', access_tech: 'LTE' },
			{ mcc: '250', mnc: '54', access_tech: 'LTE' },
			{ mcc: '262', mnc: '01', access_tech: 'GSM' },
			{ mcc: '246', mnc: '81', access_tech: 'GSM' },
		] },
	});
	const s = netStateSummary('hplmnwact', { present: true, kind: 'transparent', data: 'AA' });
	assert.strictEqual(s.text, '250-99 (LTE), 250-32 (LTE), 250-54 (LTE) … +2');
});

test('netStateRender paints the service badge, location and rows', () => {
	const els = setup();
	decoder({ 'EF.IMSI': { imsi: '262011234567890' } });
	netStateRender({
		service: { state: 'limited', source: 'net-sim:roaming_denied' },
		read_at: 1700000000,
		files: { imsi: { present: true, kind: 'transparent', data: 'AA', source: 'write' } },
		network: { location: { mcc: '262', mnc: '01', country: 'Germany',
			operator: 'Telekom', roaming: 'guest', rejected: true,
			area: 'LAI', lac: '6CD7', source: 'write' } },
	});
	assert.strictEqual(els['netstate-badge'].textContent, '⚠ Limited service');
	assert.ok(els['netstate-badge'].className.includes('text-amber-600'));
	assert.ok(els['netstate-location'].innerHTML.includes('262-01'));
	assert.ok(els['netstate-location'].innerHTML.includes('Germany'));
	assert.ok(els['netstate-location'].innerHTML.includes('Guest (roaming)'));
	assert.ok(els['netstate-location'].innerHTML.includes('PLMN not allowed'));
	assert.ok(els['netstate-location'].innerHTML.includes(
		'Telekom<span class="block">Guest (roaming) · <span class="text-red-500">⛔ PLMN not allowed</span></span>'));
	assert.ok(!els['netstate-location'].innerHTML.includes('Telekom · '));
	assert.strictEqual(els['netstate-read'].textContent, new Date(1700000000 * 1000).toLocaleTimeString());
	assert.ok(els['netstate-body'].innerHTML.includes('EF.IMSI'));
	assert.ok(els['netstate-body'].innerHTML.includes('262011234567890'));
	assert.ok(els['netstate-body'].innerHTML.includes('>write<'));
});

test('netStateRender keeps the location on one line when there is no status', () => {
	const els = setup();
	decoder({});
	netStateRender({ service: { state: 'normal' }, files: {},
		network: { location: { mcc: '262', mnc: '01', country: 'Germany',
			operator: 'Telekom', area: 'LAI', lac: '6CD7' } } });
	assert.strictEqual(els['netstate-location'].innerHTML,
		'<span class="font-mono">262-01</span> · Germany · Telekom');
});

test('netStateRender shows Undefined until something is simulated', () => {
	const els = setup();
	decoder({});
	netStateRender(null);
	assert.strictEqual(els['netstate-badge'].textContent, '○ Undefined');
	assert.ok(els['netstate-badge'].className.includes('text-gray-400'));
	assert.ok(els['netstate-body'].innerHTML.includes('EF.LOCI'));
	assert.ok(els['netstate-body'].innerHTML.includes('—'));
});

test('netStateRender shows the green normal-service badge', () => {
	const els = setup();
	decoder({});
	netStateRender({ service: { state: 'normal' }, files: {} });
	assert.strictEqual(els['netstate-badge'].textContent, '● Normal service');
	assert.ok(els['netstate-badge'].className.includes('text-emerald-600'));
});
