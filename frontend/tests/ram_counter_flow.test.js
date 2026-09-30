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

// The real functions behind the Explore Delete flow: the counter the card
// consumed must be persisted into the preset, a successful delete drops the
// record locally (no re-explore), and a delete with no PoR (the SPI may
// request none) is not a failure - live 2026-09-28 showed "Failed: 9000"
// although the delete executed.
let code = '';
for (const fn of ['berLenStr', 'ramDeleteApdu', 'ramIncrementCntr', 'ramSaveCntr',
	'getRamSpParams', 'spPorAccepted', 'ramRemoteSwOk', 'ramShowProgress',
	'ramHideProgress', 'ramDeleteFromExplorer',
	'spCntrLow', 'ramCntrLowHtml', 'ramCntrLowPresetIdx', 'ramShowCntrLow',
	'escHtml', 'esc']) {
	code += extractFunc(html, fn) + '\n';
}
eval(code);

function fakeEnv(por, success) {
	const els = {};
	const mk = () => ({ value: '', textContent: '',
		classList: { add() {}, remove() {}, toggle() {} } });
	for (const id of ['sp-spi1', 'sp-spi2-hex', 'sp-kic-hex', 'sp-kid-hex', 'sp-tar',
		'sp-cntr', 'sp-kic-key', 'sp-kid-key', 'ram-result', 'ram-steps',
		'ram-progress', 'ram-progress-text']) els[id] = mk();
	els['ram-card-sel'] = { value: '0' };
	// the preset keys the real spRefreshFromPreset/cardsApply would copy into
	// the form (the delete flow aborts without them)
	els['sp-kic-key'].value = 'AA';
	els['sp-kid-key'].value = 'BB';
	els['sp-spi2-hex'].value = '01';
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.cards = [{ name: 'C', cntr: '0000000005', kicKey: 'AA', kidKey: 'BB' }];
	const calls = { saved: 0, explored: null, removed: null, sent: null };
	globalThis.cardsSave = () => { calls.saved++; };
	globalThis.cardsRender = () => {};
	globalThis.ramRender = () => {};
	globalThis.t = s => s;
	globalThis.alert = () => {};
	globalThis.confirm = () => true;
	globalThis.spRefreshFromPreset = () => {
		els['sp-cntr'].value = cards[0].cntr;
		return cards[0].cntr;
	};
	globalThis.ramSendOta = async (apdu, sp) => {
		calls.sent = { apdu: apdu, cntr: sp.cntr, spi2: sp.spi2 };
		return { success: success !== false, por: por };
	};
	globalThis.ramExplore = async sp => { calls.explored = sp.cntr; };
	globalThis.ramRemoveFromExplorer = (aid, cascade) => {
		calls.removed = { aid: aid, cascade: cascade };
		return true;
	};
	return { els, calls };
}

test('accepted delete persists the consumed counter and drops the record', async () => {
	const { els, calls } = fakeEnv({ response_status: 'por_ok',
		decoded: { last_status_word: '9000' } });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(calls.sent.cntr, '0000000005');
	assert.strictEqual(calls.sent.apdu, '80E40000094F07F0414C46416101');
	assert.strictEqual(calls.sent.spi2, '01', 'the computed SPI2 byte must be used');
	assert.strictEqual(cards[0].cntr, '0000000006',
		'the preset must carry the counter the card consumed');
	assert.strictEqual(els['sp-cntr'].value, '0000000006');
	assert.ok(calls.saved > 0, 'cardsSave() must persist it');
	assert.deepStrictEqual(calls.removed, { aid: 'F0414C46416101', cascade: false });
	assert.strictEqual(calls.explored, null, 'no re-explore: the record is dropped locally');
	assert.strictEqual(els['ram-result'].textContent, 'OK');
});

test('a delete with no PoR is accepted (the envelope 9000 is the result)', async () => {
	const { els, calls } = fakeEnv(undefined);
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(cards[0].cntr, '0000000006');
	assert.deepStrictEqual(calls.removed, { aid: 'F0414C46416101', cascade: false });
	assert.strictEqual(els['ram-result'].textContent, 'OK');
	assert.ok(els['ram-steps'].textContent.includes('no PoR'), els['ram-steps'].textContent);
});

test('cntr_low leaves the preset untouched (the card did not consume the packet)', async () => {
	const { calls } = fakeEnv({ response_status: 'cntr_low' });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(cards[0].cntr, '0000000005');
	assert.strictEqual(calls.saved, 0);
	assert.strictEqual(calls.removed, null);
});

test('a refused DELETE still advances the counter but drops nothing', async () => {
	const { calls } = fakeEnv({ response_status: 'por_ok',
		decoded: { last_status_word: '6A88' } });
	await ramDeleteFromExplorer('F0414C46416101', true);
	assert.strictEqual(cards[0].cntr, '0000000006');
	assert.strictEqual(calls.removed, null);
});

test('a delete accepted via actual_response_sms_submit advances and drops the record', async () => {
	const { calls } = fakeEnv({ response_status: 'actual_response_sms_submit' });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(cards[0].cntr, '0000000006');
	assert.deepStrictEqual(calls.removed, { aid: 'F0414C46416101', cascade: false });
});

test('a send failure leaves the preset untouched', async () => {
	const { calls } = fakeEnv({ response_status: 'por_ok' }, false);
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(cards[0].cntr, '0000000005');
	assert.strictEqual(calls.removed, null);
});
