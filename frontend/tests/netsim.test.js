const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name) {
	const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
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
for (const f of ['netSimRandomHex', 'netSimDice', 'netSimVal', 'netSimChk', 'netSimParams', 'netSimStepText', 'netSimHomeOperator']) {
	code += extractFunc(html, f) + '\n';
}
code += 'function t(k){return k;}\n';
code += 'var _fields = {};\nvar document = { getElementById: function(id){ return _fields[id] || null; } };\n';
eval(code);

test('netSimRandomHex produces uppercase hex of the requested length', () => {
	const h = netSimRandomHex(4);
	assert.match(h, /^[0-9A-F]{8}$/);
	assert.match(netSimRandomHex(1), /^[0-9A-F]{2}$/);
});

test('netSimParams maps form fields and checkboxes', () => {
	_fields = {
		'netsim-mcc': { value: '262' },
		'netsim-mnc': { value: '01' },
		'netsim-lac': { value: '  6CD7  ' },
		'netsim-lac-empty': { value: '' },
		'netsim-churn-count': { value: '5' },
		'netsim-send-event': { checked: true },
		'netsim-dummy-locations': { checked: false },
		'netsim-invalidate-epsnsc': { checked: true },
		'netsim-keep-kasme': { checked: true },
		'netsim-write-kc': { checked: false },
		'netsim-sms-location': { checked: true },
		'netsim-cb-clear': { checked: false },
	};
	const p = netSimParams();
	assert.strictEqual(p.mcc, '262');
	assert.strictEqual(p.mnc, '01');
	assert.strictEqual(p.lac, '6CD7');
	assert.strictEqual(p.churn_count, '5');
	assert.strictEqual(p.send_event, true);
	assert.strictEqual(p.dummy_locations, false);
	assert.strictEqual(p.invalidate_epsnsc, true);
	assert.strictEqual(p.keep_kasme, true);
	assert.strictEqual(p.write_kc, false);
	assert.strictEqual(p.sms_location, true);
	assert.strictEqual(p.cb_clear, false);
	// unset fields are omitted so the server fills its own defaults
	assert.ok(!('tmsi' in p));
	assert.ok(!('kasme' in p));
});

test('netSimStepText renders the step log lines', () => {
	assert.strictEqual(
		netSimStepText({ action: 'update_binary', file: 'loci', path: 'ADF.USIM/6F7E', data: 'FF01', sw: '9000' }),
		'update_binary loci (ADF.USIM/6F7E) FF01 → SW 9000');
	assert.strictEqual(
		netSimStepText({ action: 'update_record', file: 'epsnsc', path: 'ADF.USIM/6FE4', data: 'A0', sw: '9000' }),
		'update_record epsnsc (ADF.USIM/6FE4) A0 → SW 9000');
	assert.strictEqual(
		netSimStepText({ action: 'event', file: 'location_status', data: '9B0102', sw: '9000' }),
		'ENVELOPE location_status 9B0102 → SW 9000');
	assert.strictEqual(
		netSimStepText({ action: 'skip', file: 'location_status', note: 'event 0x03 not in SET UP EVENT LIST' }),
		'skip location_status — event 0x03 not in SET UP EVENT LIST');
	assert.match(
		netSimStepText({ action: 'authenticate', parsed: { type: 'synchronisation_failure' }, sw: '9000', response: 'DC10AA' }),
		/^AUTHENTICATE synchronisation_failure → SW 9000 DC10AA$/);
});

test('netSimHomeOperator fills the home PLMN and reports its source', async () => {
	_fields = {
		'netsim-mcc': { value: '' },
		'netsim-mnc': { value: '' },
		'netsim-status': { textContent: '', className: '' },
	};
	globalThis.pysimFetch = async () => ({
		available: true,
		state: { network: { home: { mcc: '250', mnc: '99', source: 'hplmnwact' } } },
	});
	await netSimHomeOperator();
	assert.strictEqual(_fields['netsim-mcc'].value, '250');
	assert.strictEqual(_fields['netsim-mnc'].value, '99');
	assert.ok(_fields['netsim-status'].textContent.includes('250/99'));
	assert.ok(_fields['netsim-status'].textContent.includes('from EF.HPLMNwAcT'));
	assert.ok(_fields['netsim-status'].className.includes('text-gray-500'));
});

test('netSimHomeOperator reports the IMSI source and warns when unavailable', async () => {
	_fields = {
		'netsim-mcc': { value: '' },
		'netsim-mnc': { value: '' },
		'netsim-status': { textContent: '', className: '' },
	};
	globalThis.pysimFetch = async () => ({
		available: true,
		state: { network: { home: { mcc: '228', mnc: '06', source: 'imsi' } } },
	});
	await netSimHomeOperator();
	assert.ok(_fields['netsim-status'].textContent.includes('from IMSI'));
	// no home PLMN -> the fields stay and the status warns
	_fields['netsim-mcc'].value = '001';
	_fields['netsim-mnc'].value = '01';
	globalThis.pysimFetch = async () => ({ available: true, state: { network: { home: null } } });
	await netSimHomeOperator();
	assert.strictEqual(_fields['netsim-mcc'].value, '001');
	assert.strictEqual(_fields['netsim-mnc'].value, '01');
	assert.ok(_fields['netsim-status'].textContent.includes('not available'));
	assert.ok(_fields['netsim-status'].className.includes('text-yellow-600'));
});
