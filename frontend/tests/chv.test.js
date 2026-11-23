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

let code = '';
for (const f of ['cardsChvDigits', 'cardsChvState', 'cardsChvReset', 'cardsChvRetryPrompt']) {
	code += extractFunc(html, f) + '\n';
}
code += extractFunc(html, 'pysimChvRun', true) + '\n';
code += extractFunc(html, 'cardsChv', true) + '\n';
code += extractFunc(html, 'cardsAdmVerify') + '\n';
code += extractFunc(html, 'pysimVerifyPin', true) + '\n';
code += 'var _cardsChvState = {};\n';
code += 'var _pysimChvState = null;\n';
code += 'var _pysimPinStateKey = null;\n';
code += 'var _pysimLastStatus = { connected: true };\n';
eval(code);

// The page globals the CHV code touches: the dialog helpers, the fetch and
// the DOM.  Each test sets the fields it needs.
let fetchCalls = [];
let confirmMessages = [];
let promptAnswer = null;
let matchPreset = null;
let chvUpdates = [];
let availabilityCalls = 0;
function stubPage() {
	fetchCalls = [];
	confirmMessages = [];
	promptAnswer = null;
	matchPreset = null;
	chvUpdates = [];
	availabilityCalls = 0;
	cardsChvReset();
	_pysimLastStatus = { connected: true };
	global.t = s => s;
	global.pysimFetch = async (path, body) => {
		fetchCalls.push({ path, body });
		return { ok: true, sw: '9000' };
	};
	global.pysimConfirm = async message => {
		confirmMessages.push(message);
		return true;
	};
	global.pysimPrompt = async () => promptAnswer;
	global.cardsMatchedPreset = () => matchPreset;
	global.pysimVerifyAdm = () => { fetchCalls.push({ path: 'adm' }); };
	// The per-PIN state (v4.2.0): a CHV response's block reaches the badge/rows.
	global.pysimUpdatePinIndicator = status => { chvUpdates.push(status && status.chv); };
	global.pysimApplyAvailability = () => { availabilityCalls += 1; };
}

const fields = {};
function field(id) {
	if (!fields[id]) fields[id] = { value: '', textContent: '' };
	return fields[id];
}
function resetFields() {
	for (const key of Object.keys(fields)) delete fields[key];
}
global.document = {
	getElementById: id => (id in fields ? fields[id] : null),
};

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
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
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

test('a failed verify reports the attempts and confirms a retry', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
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
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
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
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	global.pysimFetch = async () => ({ ok: false, sw: '9804', failed: true });
	await pysimChvRun('pin1', 'verify', { pin: '0000' }, status);
	assert.strictEqual(status.textContent, 'PIN1 verification failed — at least one attempt left');
	assert.strictEqual(cardsChvState('pin1').blocked, undefined);
	assert.strictEqual(cardsChvState('pin1').failed, true);
});

test('a cancelled confirmation never touches the card', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	global.pysimConfirm = async () => false;
	const data = await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.strictEqual(data, null);
	assert.strictEqual(fetchCalls.length, 0);
});

test('no card session is reported without a fetch', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	_pysimLastStatus = { connected: false };
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.match(status.textContent, /not connected/);
	assert.strictEqual(fetchCalls.length, 0);
	_pysimLastStatus = { connected: true };
});

test('a 6984 reports the invalidated reference, not a failed verification', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	global.pysimFetch = async (path, body) => {
		fetchCalls.push({ path, body });
		return {
			ok: false, sw: '6984', invalidated: true,
			error: 'Referenced data invalidated',
		};
	};
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	// TS 102 221 Table 10.13: a generic refusal - no attempt count, and the
	// UI must not call it a failed verification or promise UNBLOCK (a
	// disabled/not-initialized PIN answers this way).
	assert.match(status.textContent, /^PIN1 refused by the card \(referenced data invalidated\)/);
	assert.match(status.textContent, /SW: 6984/);
	assert.match(status.textContent, /not enabled for this PIN/);
	assert.doesNotMatch(status.textContent, /verification failed/);
	assert.doesNotMatch(status.textContent, /Unblock/i);
	assert.strictEqual(cardsChvState('pin1').invalidated, true);
	assert.strictEqual(cardsChvState('pin1').attempts, undefined);
	assert.strictEqual(cardsChvState('pin1').blocked, undefined);
});

test('a retry after 6984 is confirmed and still reaches the card', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	global.pysimFetch = async (path, body) => {
		fetchCalls.push({ path, body });
		return { ok: false, sw: '6984', invalidated: true,
			error: 'Referenced data invalidated' };
	};
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	// the retry prompt states the refusal without an attempt promise
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
	// a later success clears the invalidated state
	assert.strictEqual(cardsChvState('pin1').invalidated, false);
	assert.strictEqual(cardsChvRetryPrompt('pin1'), null);
});

test('a CHV response carrying the PIN state updates the badge and the rows', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	const block = {
		pin1: { state: 'not_required', attempts_left: null, sw: '9000' },
		pin2: { state: 'required', attempts_left: 3, sw: '63C3' },
	};
	global.pysimFetch = async () => ({ ok: true, sw: '9000', chv: block });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	// the server's refreshed block reaches the indicator (one call) and the
	// Verify gates are re-evaluated
	assert.deepStrictEqual(chvUpdates, [block]);
	assert.strictEqual(availabilityCalls, 1);
	// the last status itself is kept in step (a language switch re-renders it)
	assert.deepStrictEqual(_pysimLastStatus.chv, block);
	assert.strictEqual(_pysimLastStatus.connected, true);
});

test('a verify on a disabled PIN does not claim it was verified', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	const status = field('cards-chv-status');
	const block = {
		pin1: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: false },
		pin2: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: true },
	};
	global.pysimFetch = async () => ({ ok: true, sw: '9000', chv: block });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	// 9000 from a PIN the FCP says is disabled: nothing was verified
	assert.notStrictEqual(status.textContent, 'PIN1 verified');
	assert.strictEqual(status.textContent,
		'PIN1 is not required on this card (disabled) — nothing was verified');
	// an enabled PIN keeps the ordinary line
	global.pysimFetch = async () => ({ ok: true, sw: '9000', chv: {
		pin1: { state: 'not_required', attempts_left: null, sw: '9000',
			enabled: true },
		pin2: block.pin2,
	} });
	await pysimChvRun('pin1', 'verify', { pin: '1234' }, status);
	assert.strictEqual(status.textContent, 'PIN1 verified');
});

test('the Cards editor change prompts the new PIN and updates the field', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	field('cards-pin1').value = '1234';
	promptAnswer = '5678';
	await cardsChv('pin1', 'change');
	assert.deepStrictEqual(fetchCalls[0].body, {
		action: 'change', code: 'pin1', pin: '1234', new_pin: '5678',
	});
	assert.strictEqual(field('cards-pin1').value, '5678');
	assert.match(field('cards-chv-status').textContent,
		/save the preset to store the new PIN/);
});

test('the Cards editor unblock uses the PUK field and the new PIN', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	field('cards-puk1').value = '12345678';
	promptAnswer = '4321';
	await cardsChv('pin1', 'unblock');
	assert.deepStrictEqual(fetchCalls[0].body, {
		action: 'unblock', code: 'pin1', puk: '12345678',
		new_pin: '4321',
	});
});

test('the Cards editor refuses a short PUK before the card', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	field('cards-puk1').value = '1234';
	await cardsChv('pin1', 'unblock');
	assert.match(field('cards-chv-status').textContent, /PUK must be exactly 8/);
	assert.strictEqual(fetchCalls.length, 0);
});

test('the Cards editor verify refuses an empty PIN', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	await cardsChv('pin1', 'verify');
	assert.match(field('cards-chv-status').textContent, /PIN must be 4-8/);
	assert.strictEqual(fetchCalls.length, 0);
});

test('the ADM editor verify sends the field value normalized', async () => {
	stubPage();
	resetFields();
	field('cards-chv-status');
	field('cards-adm').value = 'aabb cc';
	cardsAdmVerify();
	assert.deepStrictEqual(fetchCalls[0], { path: 'adm' });
	assert.strictEqual(fetchCalls.length, 1);
	global.pysimVerifyAdm = (el, adm) => { fetchCalls.push({ path: 'adm', adm }); };
	field('cards-adm').value = 'aabb cc';
	cardsAdmVerify();
	assert.strictEqual(fetchCalls[1].adm, 'AABBCC');
	field('cards-adm').value = '';
	cardsAdmVerify();
	assert.match(field('cards-chv-status').textContent, /ADM is required/);
});

test('the Status tab verify uses the matched preset PIN', async () => {
	stubPage();
	resetFields();
	field('pysim-pin-msg');
	matchPreset = { pin1: '1234' };
	await pysimVerifyPin('pin1');
	assert.deepStrictEqual(fetchCalls[0].body,
		{ action: 'verify', code: 'pin1', pin: '1234' });
	assert.strictEqual(field('pysim-pin-msg').textContent, 'PIN1 verified');
	matchPreset = null;
	fetchCalls = [];
	await pysimVerifyPin('pin2');
	assert.match(field('pysim-pin-msg').textContent, /No PIN in the matched preset/);
	assert.strictEqual(fetchCalls.length, 0);
});

test('the markup wires the CHV actions and the result lines', () => {
	assert.ok(html.includes("cardsChv('pin1','verify')"));
	assert.ok(html.includes("cardsChv('pin1','change')"));
	assert.ok(html.includes("cardsChv('pin1','unblock')"));
	assert.ok(html.includes("cardsChv('pin2','verify')"));
	assert.ok(html.includes('id="cards-chv-status"'));
	assert.ok(html.includes('onclick="cardsAdmVerify()"'));
	assert.ok(html.includes('id="pysim-pin-msg"'));
	assert.ok(html.includes("pysimVerifyPin('pin1')"));
	assert.ok(html.includes("pysimVerifyPin('pin2')"));
});
