// The CHV (PIN/PUK/ADM) operations and the PIN / ADM pill (v4.3.0).
//
// The Cards editor only edits presets; the card operations live in the
// Card reader tab's PIN / ADM pill, which posts /api/chv (VERIFY 20, CHANGE
// 24, DISABLE 26, ENABLE 28, UNBLOCK 2C - TS 102 221 §11.1.9-§11.1.13) and
// /api/verify-adm for ADM.  Every attempt is confirmed first.
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

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

function extractConst(src, name) {
	const m = new RegExp('const\\s+' + name + '\\s*=\\s*\\{[\\s\\S]*?\\n\\};').exec(src);
	if (!m) throw new Error('const ' + name + ' not found');
	return m[0].replace(/^const\s+/, 'var ') + '\n';
}

let code = '';
for (const f of ['cardsChvDigits', 'cardsChvState', 'cardsChvReset',
	'cardsChvRetryPrompt', 'pysimPinStateEntry', 'pysimPinState',
	'pysimPinTriesLeft', 'pysimPinNotRequiredText', 'pysimPinStateText',
	'pysimPinStateLabel', 'pysimAdmStateText', 'pysimChvCode',
	'pysimChvAction', 'pysimChvIsSim', 'pysimChvFieldsFor',
	'pysimChvFieldId', 'pysimChvFieldValue', 'pysimChvGate',
	'pysimChvGating', 'pysimChvStateRender', 'pysimChvPrefill',
	'pysimPinLegendRender', 'pysimChvRender']) {
	code += extractFunc(html, f) + '\n';
}
code += extractConst(html, 'PYSIM_CHV_FIELDS');
code += extractFunc(html, 'pysimChvRetriesText') + '\n';
for (const f of ['pysimChvRun', 'pysimChvExecute', 'pysimChvPresetUpdate',
	'pysimVerifyPin']) {
	code += extractFunc(html, f, true) + '\n';
}
code += 'var _cardsChvState = {};\n';
code += 'var _pysimChvState = null;\n';
code += 'var _pysimPinStateKey = null;\n';
code += 'var _pysimAdmAttemptsLeft = null;\n';
code += 'var _pysimAdmBlocked = false;\n';
code += 'var _pysimLastStatus = { connected: true };\n';
eval(code);

// The page globals the CHV code touches: the dialog helpers, the fetch and
// the DOM.  Each test sets what it needs.
let fetchCalls = [];
let fetchOkCalls = [];
let confirmMessages = [];
let confirmAnswers = [];
let matchPreset = null;
let chvUpdates = [];
let availabilityCalls = 0;
let admCalls = [];
let cardsFetchCalls = 0;
function stubPage() {
	fetchCalls = [];
	fetchOkCalls = [];
	confirmMessages = [];
	confirmAnswers = [];
	matchPreset = null;
	chvUpdates = [];
	availabilityCalls = 0;
	admCalls = [];
	cardsFetchCalls = 0;
	for (const key of Object.keys(elements)) delete elements[key];
	cardsChvReset();
	_pysimLastStatus = { connected: true };
	global.t = s => s;
	global.esc = s => String(s);
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true, sw: '9000' };
	};
	global.pysimFetchOk = async (p, body) => {
		fetchOkCalls.push({ path: p, body });
		return { ok: true };
	};
	global.pysimConfirm = async message => {
		confirmMessages.push(message);
		return confirmAnswers.length ? confirmAnswers.shift() : true;
	};
	global.cardsMatchedPreset = () => matchPreset;
	global.pysimVerifyAdm = (el, adm) => { admCalls.push({ el, adm }); };
	global.pysimUpdatePinIndicator = status => { chvUpdates.push(status && status.chv); };
	global.pysimApplyAvailability = () => { availabilityCalls += 1; };
	global.cardsFetch = async () => { cardsFetchCalls += 1; return true; };
}

// An auto-vivifying DOM: the pill builds its fields by innerHTML, so the field
// elements come into existence on demand.
const elements = {};
function el(id) {
	if (!elements[id]) {
		elements[id] = { id, value: '', textContent: '', innerHTML: '', disabled: false };
	}
	return elements[id];
}
global.document = { getElementById: id => el(id) };

function selectValue(id, value) {
	el(id).value = value;
}

test('cardsChvDigits enforces the PIN and PUK lengths', () => {
	assert.strictEqual(cardsChvDigits('1234', 'pin'), '1234');
	assert.strictEqual(cardsChvDigits(' 12345678 ', 'pin'), '12345678');
	assert.strictEqual(cardsChvDigits('123', 'pin'), null);
	assert.strictEqual(cardsChvDigits('123456789', 'pin'), null);
	assert.strictEqual(cardsChvDigits('12a4', 'pin'), null);
	assert.strictEqual(cardsChvDigits('', 'pin'), null);
	// the PUK is exactly 8 digits (TS 31.101 §9.6)
	assert.strictEqual(cardsChvDigits('12345678', 'puk'), '12345678');
	assert.strictEqual(cardsChvDigits('1234567', 'puk'), null);
	assert.strictEqual(cardsChvDigits('123456789', 'puk'), null);
});

test('cardsChvRetryPrompt follows the attempts and the blocked state', () => {
	stubPage();
	cardsChvReset();
	assert.strictEqual(cardsChvRetryPrompt('pin1'), null);
	cardsChvState('pin1').attempts = 2;
	assert.match(cardsChvRetryPrompt('pin1'), /2 attempt\(s\) left/);
	cardsChvState('pin1').attempts = 1;
	assert.match(cardsChvRetryPrompt('pin1'), /last attempt/);
	cardsChvState('pin1').blocked = true;
	assert.strictEqual(cardsChvRetryPrompt('pin1'), null);
	cardsChvReset();
	cardsChvState('pin2').failed = true;
	assert.match(cardsChvRetryPrompt('pin2'), /already failed/);
});

test('a verify sends the value and reports success', async () => {
	stubPage();
	const status = el('msg');
	const data = await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.strictEqual(data.ok, true);
	assert.deepStrictEqual(fetchCalls[0], {
		path: '/api/chv',
		body: { action: 'verify', code: 'pin1', pin: '1234' },
	});
	assert.strictEqual(status.textContent, 'PIN1 verified');
	// the action is confirmed first (a wrong PIN consumes an attempt)
	assert.match(confirmMessages[0], /Verify PIN1\?/);
});

test('the success line names every action', async () => {
	stubPage();
	const status = el('msg');
	const cases = [
		['verify', 'verified', { pin: '1234' }],
		['change', 'changed', { pin: '1234', new_pin: '5678' }],
		['enable', 'enabled', { pin: '1234' }],
		['disable', 'disabled', { pin: '1234' }],
		['unblock', 'unblocked', { puk: '12345678', new_pin: '4321' }],
	];
	for (const [action, word, values] of cases) {
		global.pysimFetch = async () => ({ ok: true, sw: '9000' });
		await pysimChvRun('pin1', action, values, status);
		assert.strictEqual(status.textContent, 'PIN1 ' + word, action);
	}
	// the old two-way ternary reported "unblocked" for enable/disable
	global.pysimFetch = async () => ({ ok: true, sw: '9000' });
	await pysimChvRun('pin1', 'disable', { pin: '1234' }, status);
	assert.doesNotMatch(status.textContent, /unblocked/);
});

test('a failed verify reports the attempts and confirms a retry', async () => {
	stubPage();
	const status = el('msg');
	global.pysimFetch = async (path, body) => {
		fetchCalls.push({ path, body });
		return { ok: false, sw: '63C2', attempts_left: 2 };
	};
	await pysimChvRun('pin1', 'verify', { pin: '0000' }, status);
	assert.strictEqual(status.textContent, 'PIN1 verification failed — 2 attempt(s) left');
	assert.strictEqual(cardsChvState('pin1').attempts, 2);
	// the next attempt is confirmed with the attempts left
	confirmMessages = [];
	global.pysimFetch = async () => ({ ok: true, sw: '9000' });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.match(confirmMessages[0], /2 attempt\(s\) left/);
	assert.strictEqual(status.textContent, 'PIN1 verified');
});

test('a blocked PIN is refused without a fetch', async () => {
	stubPage();
	const status = el('msg');
	global.pysimFetch = async () => ({ ok: false, sw: '6983', blocked: true });
	await pysimChvRun('pin1', 'verify', { pin: '0000' }, status);
	assert.strictEqual(status.textContent, 'PIN1 is blocked — use Unblock with the PUK');
	assert.strictEqual(cardsChvState('pin1').blocked, true);
	fetchCalls = [];
	const data = await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.strictEqual(data, null);
	assert.strictEqual(fetchCalls.length, 0, 'a blocked PIN must not be sent again');
});

test('a 2G SIM 9804 is a failure with attempts left', async () => {
	stubPage();
	const status = el('msg');
	global.pysimFetch = async () => ({ ok: false, sw: '9804', failed: true });
	await pysimChvRun('pin1', 'verify', { pin: '0000' }, status);
	assert.strictEqual(status.textContent, 'PIN1 verification failed — at least one attempt left');
	assert.strictEqual(cardsChvState('pin1').blocked, undefined);
	assert.strictEqual(cardsChvState('pin1').failed, true);
});

test('a successful enable/disable with internal retries says so', async () => {
	stubPage();
	const status = el('msg');
	// TS 102 221 Table 10.9: 63CX on ENABLE/DISABLE means the command
	// completed after the card's internal retries, so the server answers ok.
	global.pysimFetch = async () => ({ ok: true, sw: '63C2',
		internal_retries: 2, warning: 'SW 63C2: the card completed the disable' });
	await pysimChvRun('pin1', 'disable', { pin: '1234' }, status);
	assert.match(status.textContent, /^PIN1 disabled — the card completed the command with 2 internal retries/);
	assert.match(status.textContent, /check the PIN state/);
	assert.doesNotMatch(status.textContent, /SW 63C2: the card completed the disable/,
		'the translated line, not the server\'s English warning');
});

test('the refreshed counter feeds the retry warning after any action', async () => {
	stubPage();
	const status = el('msg');
	// an enable answers 63C2 (internal retries), but the state readout says
	// only one attempt is left - the next attempt must warn about it
	global.pysimFetch = async () => ({ ok: true, sw: '63C2', internal_retries: 2,
		chv: { pin1: { state: 'required', attempts_left: 1, sw: '63C1',
			enabled: true } } });
	await pysimChvRun('pin1', 'enable', { pin: '1234' }, status);
	assert.strictEqual(cardsChvState('pin1').attempts, 1);
	assert.match(cardsChvRetryPrompt('pin1'), /last attempt/);
});

test('an internal-retries 63CX shows a translated line, not an attempt count', async () => {
	stubPage();
	const status = el('msg');
	// TS 102 221 Table 10.9: for CHANGE a 63CX is the card's internal retries
	// (both readings stay possible - §11.1.10.1 prints no SW for a false old
	// PIN); the PWA composes the line, the server warning is the fallback.
	global.pysimFetch = async () => ({ ok: false, sw: '63C2', internal_retries: 2,
		warning: 'SW 63C2: internal retries of the change' });
	await pysimChvRun('pin1', 'change', { pin: '1234', new_pin: '5678' }, status);
	assert.match(status.textContent,
		/^SW: 63C2 — the card reports 2 internal retries - check the PIN state \(the command may not have completed\)/);
	assert.doesNotMatch(status.textContent, /attempt\(s\) left/);
	assert.doesNotMatch(status.textContent, /SW 63C2: internal retries of the change/,
		'the server\'s English warning is only the fallback');
	// and it never advances the client's attempts tracking
	assert.strictEqual(cardsChvState('pin1').attempts, undefined);
});

test('a cancelled confirmation never touches the card', async () => {
	stubPage();
	const status = el('msg');
	global.pysimConfirm = async () => false;
	const data = await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.strictEqual(data, null);
	assert.strictEqual(fetchCalls.length, 0);
});

test('no card session is reported without a fetch', async () => {
	stubPage();
	const status = el('msg');
	_pysimLastStatus = { connected: false };
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.match(status.textContent, /not connected/);
	assert.strictEqual(fetchCalls.length, 0);
	_pysimLastStatus = { connected: true };
});

test('a 6984 reports the invalidated reference, not a failed verification', async () => {
	stubPage();
	const status = el('msg');
	global.pysimFetch = async () => ({
		ok: false, sw: '6984', invalidated: true,
		error: 'Referenced data invalidated',
	});
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.match(status.textContent, /^PIN1 refused by the card \(referenced data invalidated\)/);
	assert.match(status.textContent, /SW: 6984/);
	// Live 2026-11-23: the bench card answers 6984 to a *wrong* value too, so
	// the line must not claim the PIN is disabled - only that the card gave no
	// attempt count and the value may be wrong (or the reference unusable).
	assert.match(status.textContent, /reported no attempt count/);
	assert.match(status.textContent, /value may be wrong/);
	assert.doesNotMatch(status.textContent, /not enabled for this PIN/);
	assert.doesNotMatch(status.textContent, /verification failed/);
	assert.doesNotMatch(status.textContent, /Unblock/i);
	assert.strictEqual(cardsChvState('pin1').invalidated, true);
});

test('a retry after 6984 is confirmed and still reaches the card', async () => {
	stubPage();
	const status = el('msg');
	global.pysimFetch = async () => ({ ok: false, sw: '6984', invalidated: true,
		error: 'Referenced data invalidated' });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.match(cardsChvRetryPrompt('pin1'), /referenced data invalidated/);
	confirmMessages = [];
	fetchCalls = [];
	global.pysimFetch = async (path, body) => {
		fetchCalls.push({ path, body });
		return { ok: true, sw: '9000' };
	};
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.match(confirmMessages[0], /already refused this PIN/);
	assert.strictEqual(fetchCalls.length, 1, 'the retry must still perform the fetch');
	assert.strictEqual(status.textContent, 'PIN1 verified');
	assert.strictEqual(cardsChvState('pin1').invalidated, false);
});

test('a CHV response carrying the PIN state updates the badge and the rows', async () => {
	stubPage();
	const status = el('msg');
	const block = {
		pin1: { state: 'not_required', attempts_left: null, sw: '9000' },
		pin2: { state: 'required', attempts_left: 3, sw: '63C3' },
	};
	global.pysimFetch = async () => ({ ok: true, sw: '9000', chv: block });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.deepStrictEqual(chvUpdates, [block]);
	assert.strictEqual(availabilityCalls, 1);
	assert.deepStrictEqual(_pysimLastStatus.chv, block);
	assert.strictEqual(_pysimLastStatus.connected, true);
});

test('a verify on a disabled PIN does not claim it was verified', async () => {
	stubPage();
	const status = el('msg');
	const block = {
		pin1: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: false },
		pin2: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: true },
	};
	global.pysimFetch = async () => ({ ok: true, sw: '9000', chv: block });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.notStrictEqual(status.textContent, 'PIN1 verified');
	assert.strictEqual(status.textContent,
		'PIN1 is not required on this card (disabled) — nothing was verified');
	global.pysimFetch = async () => ({ ok: true, sw: '9000', chv: {
		pin1: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: true },
		pin2: block.pin2,
	} });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.strictEqual(status.textContent, 'PIN1 verified');
});

test('the pill maps each action to its fields', () => {
	stubPage();
	const cases = [
		['pin1', 'verify', ['pin']],
		['pin2', 'verify', ['pin']],
		['adm', 'verify', ['adm']],
		['pin1', 'change', ['pin', 'new_pin']],
		['pin2', 'unblock', ['puk', 'new_pin']],
		['pin1', 'enable', ['pin']],
		['pin2', 'disable', ['pin']],
	];
	for (const [codeName, action, expected] of cases) {
		selectValue('pysim-chv-code', codeName);
		selectValue('pysim-chv-action', action);
		assert.deepStrictEqual(pysimChvFieldsFor(codeName, action), expected,
			codeName + ' ' + action);
	}
	// ADM folds every action onto Verify (§9.5.1: no change/unblock clause)
	selectValue('pysim-chv-code', 'adm');
	selectValue('pysim-chv-action', 'change');
	assert.strictEqual(pysimChvAction(), 'verify');
});

test('the pill builds the action fields and its live state', () => {
	stubPage();
	_pysimChvState = {
		pin1: { state: 'required', attempts_left: 3, sw: '63C3', enabled: true },
		pin2: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: false },
	};
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'change');
	pysimChvRender();
	assert.match(el('pysim-chv-fields').innerHTML, /id="pysim-chv-pin"/);
	assert.match(el('pysim-chv-fields').innerHTML, /id="pysim-chv-new-pin"/);
	assert.strictEqual(el('pysim-chv-state').textContent,
		'PIN1: required, 3 tries left');
	selectValue('pysim-chv-code', 'adm');
	pysimChvRender();
	assert.match(el('pysim-chv-fields').innerHTML, /id="pysim-chv-adm"/);
	assert.match(el('pysim-chv-state').textContent, /^ADM: /);
});

test('the pill prefills from the matched preset and typing overrides', () => {
	stubPage();
	matchPreset = { id: 'p1', pin1: '1111', pin2: '2222', puk1: '12345678',
		adm: 'AABBCC' };
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'verify');
	pysimChvRender();                      // rebuilds the (empty) pin field
	el('pysim-chv-pin').value = '9999';    // typed by the operator
	pysimChvPrefill(false);
	assert.strictEqual(el('pysim-chv-pin').value, '9999', 'typing wins');
	el('pysim-chv-pin').value = '';
	pysimChvPrefill(false);
	assert.strictEqual(el('pysim-chv-pin').value, '1111');
	// the button forces the preset value over a typed one
	el('pysim-chv-pin').value = '9999';
	pysimChvPrefill(true);
	assert.strictEqual(el('pysim-chv-pin').value, '1111');
	// unblock prefills the PUK
	selectValue('pysim-chv-action', 'unblock');
	pysimChvRender();
	pysimChvPrefill(true);
	assert.strictEqual(el('pysim-chv-puk').value, '12345678');
	// ADM prefills its key
	selectValue('pysim-chv-code', 'adm');
	pysimChvRender();
	pysimChvPrefill(true);
	assert.strictEqual(el('pysim-chv-adm').value, 'AABBCC');
});

test('the prefill button does nothing without a matched preset', () => {
	stubPage();
	// no matched preset: the forced path used to blank every field
	matchPreset = null;
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'verify');
	pysimChvRender();
	el('pysim-chv-pin').value = '9999';
	pysimChvPrefill(true);
	assert.strictEqual(el('pysim-chv-pin').value, '9999',
		'a typed value must survive the button without a preset');
	// with a matched preset the force path fills every field again
	matchPreset = { id: 'p1', pin1: '1111' };
	pysimChvPrefill(true);
	assert.strictEqual(el('pysim-chv-pin').value, '1111');
	el('pysim-chv-pin').value = '9999';
	pysimChvPrefill(true);
	assert.strictEqual(el('pysim-chv-pin').value, '1111', 'force overrides typing');
});

test('the pill gates enable/disable by the reported flag', () => {
	stubPage();
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'disable');
	// enabled: allowed
	_pysimChvState = { pin1: { state: 'required', attempts_left: 3,
		sw: '63C3', enabled: true }, pin2: { state: 'unknown' } };
	assert.strictEqual(pysimChvGate(), null);
	// already disabled: refused
	_pysimChvState.pin1.enabled = false;
	assert.match(pysimChvGate().stop, /already disabled/);
	// blocked first: refuse with "unblock"
	_pysimChvState.pin1 = { state: 'blocked', attempts_left: 0, sw: '63C0',
		enabled: true };
	assert.match(pysimChvGate().stop, /blocked/);
	// enable of an enabled PIN: refused
	selectValue('pysim-chv-action', 'enable');
	_pysimChvState.pin1 = { state: 'not_required', attempts_left: null,
		sw: '9000', enabled: true };
	assert.match(pysimChvGate().stop, /already enabled/);
	// no flag at all: allowed with a warning
	_pysimChvState.pin1 = { state: 'required', attempts_left: 2, sw: '63C2',
		enabled: null };
	assert.match(pysimChvGate().warning, /does not report/);
	// 2G SIM: PIN2 cannot be enabled/disabled
	_pysimLastStatus = { connected: true, card: { kind: 'sim' } };
	selectValue('pysim-chv-code', 'pin2');
	assert.match(pysimChvGate().stop, /2G SIM/);
});

test('the pill executes and offers the preset update on change', async () => {
	stubPage();
	matchPreset = { id: 'p1', pin1: '1111' };
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'change');
	pysimChvRender();
	el('pysim-chv-pin').value = '1111';
	el('pysim-chv-new-pin').value = '5678';
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true, sw: '9000',
			chv: { pin1: { state: 'not_required', enabled: true },
				pin2: { state: 'not_required', enabled: true } } };
	};
	await pysimChvExecute();
	assert.deepStrictEqual(fetchCalls[0].body, {
		action: 'change', code: 'pin1', pin: '1111', new_pin: '5678',
	});
	// the confirmation texts: the action, then the preset update
	assert.match(confirmMessages[0], /Change PIN1\?/);
	assert.match(confirmMessages[1], /Update the matched preset with the new PIN\?/);
	assert.deepStrictEqual(fetchOkCalls, [{
		path: '/api/presets/update',
		body: { id: 'p1', fields: { pin1: '5678' } },
	}]);
	assert.strictEqual(cardsFetchCalls, 1);
	assert.match(el('pysim-chv-result').textContent, /preset updated/);
});

test('the preset update is offered only when asked and only with a preset', async () => {
	// a declined update writes nothing
	stubPage();
	matchPreset = { id: 'p1', pin1: '1111' };
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'unblock');
	pysimChvRender();
	el('pysim-chv-puk').value = '12345678';
	el('pysim-chv-new-pin').value = '4321';
	confirmAnswers = [true, false];     // run yes, update no
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true, sw: '9000', chv: {
			pin1: { state: 'not_required', enabled: true } } };
	};
	await pysimChvExecute();
	assert.strictEqual(fetchOkCalls.length, 0);
	// no matched preset: no prompt at all
	stubPage();
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true, sw: '9000', chv: {
			pin1: { state: 'not_required', enabled: true } } };
	};
	selectValue('pysim-chv-code', 'pin1');
	selectValue('pysim-chv-action', 'change');
	pysimChvRender();
	el('pysim-chv-pin').value = '1111';
	el('pysim-chv-new-pin').value = '5678';
	await pysimChvExecute();
	assert.strictEqual(confirmMessages.length, 1, 'only the action confirmation');
	assert.strictEqual(fetchOkCalls.length, 0);
});

test('a failed change or unblock offers no preset update', async () => {
	for (const action of ('change', 'unblock')) {
		stubPage();
		matchPreset = { id: 'p1', pin1: '1111' };
		selectValue('pysim-chv-code', 'pin1');
		selectValue('pysim-chv-action', action);
		pysimChvRender();
		if (action === 'change') {
			el('pysim-chv-pin').value = '1111';
			el('pysim-chv-new-pin').value = '5678';
		} else {
			el('pysim-chv-puk').value = '12345678';
			el('pysim-chv-new-pin').value = '4321';
		}
		global.pysimFetch = async () => ({ ok: false, sw: '6983',
			blocked: true });
		await pysimChvExecute();
		assert.strictEqual(fetchOkCalls.length, 0,
			action + ': a failed operation must not touch the preset');
		assert.strictEqual(confirmMessages.length, 1,
			action + ': only the action confirmation');
	}
});

test('the ADM entry of the pill verifies through /api/verify-adm', async () => {
	stubPage();
	selectValue('pysim-chv-code', 'adm');
	pysimChvRender();
	el('pysim-chv-adm').value = 'aabb cc';
	await pysimChvExecute();
	assert.strictEqual(fetchCalls.length, 0, 'ADM does not go through /api/chv');
	assert.deepStrictEqual(admCalls, [{ el: el('pysim-chv-result'), adm: 'AABBCC' }]);
	// every attempt is confirmed first, the ADM one included
	assert.match(confirmMessages[0], /^Verify ADM\?/);
	// a cancelled confirmation sends nothing
	confirmMessages = [];
	admCalls = [];
	confirmAnswers = [false];
	await pysimChvExecute();
	assert.strictEqual(admCalls.length, 0);
});

test('the reader rows verify with the matched preset value', async () => {
	stubPage();
	matchPreset = { pin1: '1111', pin2: '2222' };
	global.pysimFetch = async (p, body) => {
		fetchCalls.push({ path: p, body });
		return { ok: true, sw: '9000' };
	};
	await pysimVerifyPin('pin1');
	assert.deepStrictEqual(fetchCalls[0].body,
		{ action: 'verify', code: 'pin1', pin: '1111' });
	assert.strictEqual(el('pysim-pin-msg').textContent, 'PIN1 verified');
	fetchCalls = [];
	matchPreset = null;
	await pysimVerifyPin('pin2');
	assert.match(el('pysim-pin-msg').textContent, /No PIN in the matched preset/);
	assert.strictEqual(fetchCalls.length, 0);
});

test('the markup wires the reader rows and the pill', () => {
	assert.ok(html.includes('id="pysim-pin1-state"'));
	assert.ok(html.includes('id="pysim-pin2-state"'));
	assert.ok(html.includes('id="pysim-adm-state"'));
	assert.ok(html.includes('data-preset-code="pin1"'));
	assert.ok(html.includes('data-preset-code="pin2"'));
	assert.ok(html.includes('data-preset-code="adm"'));
	assert.ok(html.includes('data-pysim-sub="chv"'));
	assert.ok(html.includes('id="pysim-sub-chv"'));
	assert.ok(html.includes('id="pysim-chv-code"'));
	assert.ok(html.includes('id="pysim-chv-action"'));
	assert.ok(html.includes('id="pysim-chv-fields"'));
	assert.ok(html.includes('id="pysim-chv-result"'));
	assert.ok(html.includes('onclick="pysimChvExecute()"'));
	assert.ok(html.includes('onclick="pysimChvPrefill(true)"'));
	// the prefill button belongs to the matched preset
	assert.match(html,
		/<button[^>]*data-preset-gate="matched"[^>]*onclick="pysimChvPrefill\(true\)"/);
	// the Cards editor is pure data entry now (v4.3.0)
	for (const gone of ['cardsChv(', 'cardsAdmVerify', 'id="cards-chv-status"',
		'id="cards-pin-state"']) {
		assert.ok(!html.includes(gone), 'still in the markup: ' + gone);
	}
});
