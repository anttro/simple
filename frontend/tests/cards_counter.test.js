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
for (const fn of ['cardsTarValue', 'cardsApplyFields', 'cardsApply',
	'spRefreshFromPreset', 'spPorAccepted', 'spNextCntr', 'spPresetIdx',
	'spKeysetKvnOf', 'spKeysetList', 'spKeysetFor', 'spKeysetCheck',
	'spKeysetOptionsHtml', 'cardsKeysetRowHtml', 'cardsKeysetRowsRender',
	'cardsKeysetAdd', 'cardsKeysetRemove', 'cardsKeysetKvnUpdate',
	'cardsKeysetsFromForm', 'spKeysetSync', 'spKeysetApply',
	'spKeysetChanged', 'ramKeysetChanged', 'ramPresetIdx', 'tarPresetIdx', 'spKeysetGuard',
	'ramKeysetGuard',
]) {
	code += extractFunc(html, fn) + '\n';
}
code = 'var _cardsKeysetCount = 0;\nglobalThis.spPresetWarningRender = function() {};\n'
	+ 'globalThis.pysimApplyAvailability = function() {};\n' + code;
code = 'var _genSpBuildStub = function() {};\n' + code;
eval(code);
globalThis._genSpBuild = () => {};
globalThis.spShowSizeInfo = () => {};
globalThis.spKeysetSync = globalThis.spKeysetSync || (() => {});

// The SP form is a working copy: the preset is the source of truth, and
// every SCP80/RAM operation re-reads it before starting (v3.6.3).  The plain
// SCP80 send keeps a hand-edited packet TAR (v3.6.21).
function fakeEnv(selValue, selId, presetCntr) {
	const els = {};
	for (const id of ['sp-spi1', 'sp-spi2-hex', 'sp-kic-idx', 'sp-kic-alg',
		'sp-kid-idx', 'sp-kid-alg', 'sp-tar', 'sp-cntr', 'sp-kic-key', 'sp-kid-key']) {
		els[id] = { value: '' };
	}
	els[selId || 'sp-card-sel'] = { value: selValue };
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis.cards = [{ name: 'C', cntr: presetCntr, spi1: '16', spi2: '01',
		kic: '15', kid: '15', tar: '000000', uiccTar: 'B00000',
		kicKey: 'AA', kidKey: 'BB' }];
	globalThis._spTarKey = 'uiccTar';
	globalThis.updateSpKic = () => {};
	globalThis.updateSpKid = () => {};
	globalThis.spInvalidate = () => {};
	globalThis.updateSp = () => {};
	let gen = 0;
	globalThis.genSp = () => { gen++; };
	// cardsApply refreshes the preview directly (the guarded genSp() is the
	// Generate button's path), so the counter stub is _genSpBuild
	globalThis._genSpBuild = () => { gen++; };
	return { els, gen: () => gen };
}

test('spRefreshFromPreset re-reads the edited preset before an operation', () => {
	const env = fakeEnv('0', 'sp-card-sel', '00000000AA');
	env.els['sp-cntr'].value = '0000000001';        // stale form copy
	assert.strictEqual(spRefreshFromPreset('sp-card-sel'), '00000000AA');
	assert.strictEqual(env.els['sp-cntr'].value, '00000000AA');
	assert.ok(env.gen() > 0, 'the packet must be regenerated from the new counter');
	// no preset selected: the manual form is left alone
	globalThis.cards = [];
	assert.strictEqual(spRefreshFromPreset('sp-card-sel'), '');
	assert.strictEqual(env.els['sp-cntr'].value, '00000000AA');
});

test('spRefreshFromPreset for the RAM selector targets the ISD TAR', () => {
	const env = fakeEnv('0', 'ram-card-sel', '0000000010');
	assert.strictEqual(spRefreshFromPreset('ram-card-sel'), '0000000010');
	assert.strictEqual(_spTarKey, 'tar');
	assert.strictEqual(env.els['sp-tar'].value, '000000');
});

test('the pre-send refresh keeps a hand-edited packet TAR (v3.6.21)', () => {
	const env = fakeEnv('0', 'sp-card-sel', '0000000020');
	env.els['sp-tar'].value = 'AF4D01';   // e.g. a push/link trigger target
	assert.strictEqual(spRefreshFromPreset('sp-card-sel', true), '0000000020');
	assert.strictEqual(env.els['sp-tar'].value, 'AF4D01', 'the typed TAR must survive the send');
	assert.strictEqual(env.els['sp-cntr'].value, '0000000020', 'the counter still refreshes');
	// an explicit preset apply (or a pack) still sets the TAR
	cardsApplyFields(0);
	assert.strictEqual(env.els['sp-tar'].value, 'B00000');
});

test('cardsApplyFields fills the form without generating a packet', () => {
	const env = fakeEnv('0', 'sp-card-sel', '0000000007');
	env.els['sp-cntr'].value = 'nonsense';
	assert.strictEqual(cardsApplyFields(0), true);
	assert.strictEqual(env.els['sp-cntr'].value, '0000000007');
	assert.strictEqual(env.gen(), 0, 'a plain field refresh must not rebuild the packet');
	cardsApply(0);
	assert.strictEqual(env.gen(), 1);
	assert.strictEqual(cardsApplyFields(3), false);
});

test('spPorAccepted treats a missing PoR and por_ok as accepted', () => {
	assert.strictEqual(spPorAccepted(undefined), true);
	assert.strictEqual(spPorAccepted({ response_status: 'por_ok' }), true);
	// 0x0B: the real response follows as an SMS-SUBMIT - the packet was consumed
	assert.strictEqual(spPorAccepted({ response_status: 'actual_response_sms_submit' }), true);
	assert.strictEqual(spPorAccepted({ response_status: 'cntr_low' }), false);
	assert.strictEqual(spPorAccepted({ response_status: 'rc_cc_ds_failed' }), false);
});
