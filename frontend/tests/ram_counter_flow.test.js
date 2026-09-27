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
// consumed must be persisted into the preset, or the next operation starts
// one behind and is rejected with cntr_low.
let code = '';
for (const fn of ['berLenStr', 'ramDeleteApdu', 'ramIncrementCntr', 'ramSaveCntr',
	'getRamSpParams', 'spPorAccepted', 'ramRemoteSwOk', 'ramShowProgress',
	'ramHideProgress', 'ramDeleteFromExplorer']) {
	code += extractFunc(html, fn) + '\n';
}
eval(code);

function fakeEnv(por, success) {
	const els = {};
	const mk = () => ({ value: '', textContent: '',
		classList: { add() {}, remove() {}, toggle() {} } });
	for (const id of ['sp-spi1', 'sp-spi2', 'sp-kic-hex', 'sp-kid-hex', 'sp-tar',
		'sp-cntr', 'sp-kic-key', 'sp-kid-key', 'ram-result', 'ram-steps',
		'ram-progress', 'ram-progress-text']) els[id] = mk();
	els['ram-card-sel'] = { value: '0' };
	// the preset keys the real spRefreshFromPreset/cardsApply would copy into
	// the form (the delete flow aborts without them)
	els['sp-kic-key'].value = 'AA';
	els['sp-kid-key'].value = 'BB';
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.cards = [{ name: 'C', cntr: '0000000005', kicKey: 'AA', kidKey: 'BB' }];
	const calls = { saved: 0, explored: null, sent: null };
	globalThis.cardsSave = () => { calls.saved++; };
	globalThis.cardsRender = () => {};
	globalThis.ramRender = () => {};
	globalThis.t = s => s;
	globalThis.alert = () => {};
	globalThis.confirm = () => true;
	// the preset re-read (cardsApply -> the sp-* form fields); the real
	// spRefreshFromPreset is covered by cards_counter.test.js
	globalThis.spRefreshFromPreset = () => {
		els['sp-cntr'].value = cards[0].cntr;
		return cards[0].cntr;
	};
	globalThis.ramSendOta = async (apdu, sp) => {
		calls.sent = { apdu: apdu, cntr: sp.cntr };
		return { success: success !== false, por: por };
	};
	globalThis.ramExplore = async sp => { calls.explored = sp.cntr; };
	return { els, calls };
}

test('accepted delete persists the consumed counter and re-explores from it', async () => {
	const { els, calls } = fakeEnv({ response_status: 'por_ok',
		decoded: { last_status_word: '9000' } });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(calls.sent.cntr, '0000000005');
	assert.strictEqual(calls.sent.apdu, '80E40000094F07F0414C4641610100');
	assert.strictEqual(cards[0].cntr, '0000000006',
		'the preset must carry the counter the card consumed');
	assert.strictEqual(els['sp-cntr'].value, '0000000006');
	assert.ok(calls.saved > 0, 'cardsSave() must persist it');
	assert.strictEqual(calls.explored, '0000000006',
		'the re-explore must start at N+1, never replay N');
	assert.strictEqual(els['ram-result'].textContent, 'OK');
});

test('cntr_low leaves the preset untouched (the card did not consume the packet)', async () => {
	const { calls } = fakeEnv({ response_status: 'cntr_low' });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(cards[0].cntr, '0000000005');
	assert.strictEqual(calls.saved, 0);
	assert.strictEqual(calls.explored, null);
});

test('a refused DELETE still advances the counter but does not re-explore', async () => {
	const { calls } = fakeEnv({ response_status: 'por_ok',
		decoded: { last_status_word: '6A88' } });
	await ramDeleteFromExplorer('F0414C46416101', true);
	assert.strictEqual(cards[0].cntr, '0000000006');
	assert.strictEqual(calls.explored, null);
});

test('a send failure leaves the preset untouched', async () => {
	const { calls } = fakeEnv({ response_status: 'por_ok' }, false);
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.strictEqual(cards[0].cntr, '0000000005');
	assert.strictEqual(calls.explored, null);
});
