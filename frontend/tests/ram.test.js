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

// Extract chain builder functions and dependencies
const FNS = ['berLenStr', 'buildApdu', 'escHtml', 'esc', 'chainInit', 'chainRamBuildRowHex', 'ramFmtLifecycle', 'ramFmtPrivileges', 'ramRenderExploreHtml', 'ramStepLine',
	'ramCardIdxAfterRemove', 'ramClearResults', 'ramHideProgress', 'ramOpChanged', 'ramRender', 'ramApplyCard', 'ramExecute',
	'jcAidNorm', 'jcAidName', 'jcAidSuffix', 'jcAidHtml'];
let code = '';
for (const f of FNS) {
	code += extractFunc(html, f) + '\n';
}
const m = html.match(/const _chains = \{\};/);
if (m) code += m[0].replace(/^const /, 'var ') + '\n';
const lc = html.match(/const RAM_LIFECYCLE = \{[\s\S]*?\n\};/);
if (lc) code += lc[0].replace(/^const /, 'var ') + '\n';
const an = html.match(/const JC_AID_NAMES = \{[\s\S]*?\n\};/);
if (an) code += an[0].replace(/^const /, 'var ') + '\n';
const ar = html.match(/const JC_AID_RIDS = \{[\s\S]*?\n\};/);
if (ar) code += ar[0].replace(/^const /, 'var ') + '\n';
eval(code);
code += 'var _ramCardIdx = null;\nvar _ramOpLast = null;\nvar _ramExplorerData = null;\n';
eval(code);

const els = {};
const doc = { getElementById: (id) => { if (!els[id]) els[id] = {value:''}; return els[id]; } };
global.document = doc;

function reset() { for (const id of Object.keys(els)) delete els[id]; }

function genRamResult(fields) {
	const row = { cmd: fields.cmd, fields: fields };
	return chainRamBuildRowHex(0, row);
}

function genRamApdu() {
	return genRamResult({
		cmd: 'install-install',
		aid: 'A000000151000000',
		elfAid: '',
		modAid: '',
		priv: '00',
		tkEnabled: true,
		tkMode: 'ea',
		tkPriority: '0',
		tkTimers: '0',
		tkTextlen: '0',
		tkMenus: '2',
		tkFirstpos: '1',
		tkFirstid: '01',
		tkLastpos: '2',
		tkLastid: '02',
		tkChannels: '0',
		tkMsl: '16',
		tkTar: 'B00001',
		tkAd: '',
		tkServices: '0',
	});
}

test('UICC toolkit nested inside EA (m=2, services 0)', () => {
	const apdu = genRamApdu();
	assert.ok(apdu.includes('EA13801100000002010102020002011603B0000100'), apdu);
});

test('UICC m=1 emits single pair', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '1', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '0', tkLastid: '00',
		tkChannels: '0', tkMsl: '16', tkTar: 'B00001', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EA11800F0000000101010002011603B0000100'), apdu);
});

test('UICC m=3 fills middle pair with 0000', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '3', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '3', tkLastid: '03',
		tkChannels: '0', tkMsl: '16', tkTar: 'B00001', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EA158013000000030101000003030002011603B0000100'), apdu);
});

test('UICC services 7 appended as final byte', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '2', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '2', tkLastid: '02',
		tkChannels: '0', tkMsl: '16', tkTar: 'B00001', tkAd: '', tkServices: '7',
	});
	assert.ok(apdu.includes('EA13801100000002010102020002011603B0000107'), apdu);
});

test('SIM (CA) access domain FIRST, no services byte', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ca', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '2', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '2', tkLastid: '02',
		tkChannels: '0', tkMsl: '16', tkTar: 'B00001', tkAd: '5A', tkServices: '0',
	});
	assert.ok(apdu.includes('EF14CA12015A00000002010102020002011603B00001'), apdu);
});

test('SIM (CA) blank access domain emits length byte 00', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ca', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '2', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '2', tkLastid: '02',
		tkChannels: '0', tkMsl: '16', tkTar: 'B00001', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EF13CA110000000002010102020002011603B00001'), apdu);
});

test('SIM (CA) m=3, no TAR, blank access domain', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ca', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '3', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '3', tkLastid: '03',
		tkChannels: '0', tkMsl: '16', tkTar: '', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EF12CA1000000000030101000003030002011600'), apdu);
});

test('UICC m=60 uses long-form BER lengths (EA 81 87 / inner 81 84)', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '60', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '60', tkLastid: '3C',
		tkChannels: '0', tkMsl: '16', tkTar: 'B00001', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EA8188808185'), apdu);
});

test('LOAD P1 fixed to 80 (last block)', () => {
	const apdu = genRamResult({ cmd: 'load', data: 'AABBCC', block: '0' });
	assert.ok(apdu.startsWith('80E88000'), apdu);
});

test('DELETE: P1=00, mode in P2', () => {
	let apdu = genRamResult({ cmd: 'delete', aid: 'AA1902BC225501', delMode: '00' });
	assert.ok(apdu.startsWith('80E40000'), apdu);

	apdu = genRamResult({ cmd: 'delete', aid: 'AA1902BC225501', delMode: '80' });
	assert.ok(apdu.startsWith('80E40080'), apdu);
});

test('STORE DATA ram-enc P1 values 00/40/80/C0/E0', () => {
	for (const [enc, p1] of [['00','00'],['40','40'],['80','80'],['C0','C0'],['E0','E0']]) {
		const apdu = genRamResult({ cmd: 'store-data', data: 'AABB', enc: enc, block: '0' });
		assert.ok(apdu.startsWith('80E2' + p1 + '00'), enc + ' -> P1 ' + p1);
	}
});

test('ramRenderExploreHtml localizes every label and button', () => {
	const seen = [];
	global.t = s => { seen.push(s); return 'XX' + s; };
	const out = ramRenderExploreHtml(
		{ appCount: 5, freeNV: 100, freeV: 50 },
		[{ aid: 'A000000151000000', lifecycle: '07', privileges: '', sdAid: 'A000000151000000' }],
		[{ aid: 'A1130001180001', lifecycle: '07', privileges: '80', implicitSel: '00', elfAid: 'ELF1' }],
		[{ aid: 'ELF1', lifecycle: '01', version: '1.0', moduleAids: ['M1'], sdAid: null }]
	);
	delete global.t;
	assert.ok(out.includes('XXDelete'), out);
	assert.ok(out.includes('XXDelete All'), out);
	assert.ok(out.includes('XXApplications:'), out);
	assert.ok(out.includes('XXFree NV:'), out);
	assert.ok(out.includes('XXFree Volatile:'), out);
	assert.ok(out.includes('XXAID:'), out);
	assert.ok(out.includes('XXLifecycle:'), out);
	assert.ok(out.includes('XXPrivileges:'), out);
	assert.ok(out.includes('XXSD AID:'), out);
	assert.ok(out.includes('XXImplicit sel:'), out);
	assert.ok(out.includes('XXVersion:'), out);
	assert.ok(seen.includes('Application / Instance AID:'));
	assert.ok(seen.includes('Load File AID / Package AID:'));
	assert.ok(seen.includes('Executable Module AIDs / Applet Class AIDs:'));
	assert.ok(!out.includes('data-l10n'), out);
	// the well-known ISD AID is annotated (GP Card Spec v2.3.1 H.1.3)
	assert.ok(out.includes('A000000151000000 <span class="text-gray-400 dark:text-slate-500">(GlobalPlatform Issuer Security Domain)</span>'), out);
});

test('ramRenderExploreHtml annotates standard package AIDs, vendor AIDs stay bare', () => {
	global.t = s => s;
	const out = ramRenderExploreHtml(null, [], [], [
		{ aid: 'A0000000871005FFFFFFFF8913200000', lifecycle: '01', version: '1.0',
		  moduleAids: ['A000000062010101'], sdAid: 'A0000001515350' },
	]);
	delete global.t;
	assert.ok(out.includes('(uicc.usim.toolkit)'), out);
	assert.ok(out.includes('(javacard.framework.service)'), out);
	assert.ok(out.includes('(GlobalPlatform RID)'), out);
});

test('ramFmtPrivileges uses the translated (none) placeholder', () => {
	global.t = s => 'XX' + s;
	assert.strictEqual(ramFmtPrivileges(''), 'XX(none)');
	assert.strictEqual(ramFmtPrivileges('00'), 'XX(none)');
	delete global.t;
});

function fakeClassList() {
	const set = new Set();
	return {
		add: (...cs) => cs.forEach(c => set.add(c)),
		remove: (...cs) => cs.forEach(c => set.delete(c)),
		contains: c => set.has(c),
		toggle: (c, on) => { if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); },
	};
}

function fakeEl(id) {
	return {
		id,
		value: '',
		innerHTML: '',
		textContent: '',
		classList: fakeClassList(),
		options: [],
		appendChild(opt) { this.options.push(opt); },
	};
}

function fakeRamDocument(ids) {
	const els = {};
	for (const id of ids) els[id] = fakeEl(id);
	const sel = els['ram-card-sel'];
	if (sel) {
		Object.defineProperty(sel, 'innerHTML', {
			get() { return this._html || ''; },
			set(v) { this._html = v; this.value = ''; },
		});
	}
	globalThis.document = {
		getElementById: id => els[id] || null,
		createElement: () => fakeEl('option'),
	};
	return els;
}

test('ramStepLine shows the PoR verdict and the remote status word', () => {
	globalThis.t = s => s;
	globalThis.lookupSw = (a, b) => (a + b === '6700' ? 'Wrong length in Lc' : '');
	const okLine = ramStepLine({ name: 'INSTALL [for load]', por_status: 'por_ok',
		por_sw: '9000', sw: '9000', bytes: 50, segments: 1 }, 0);
	assert.ok(okLine.startsWith('\u2705'), okLine);
	assert.ok(okLine.includes('PoR ok'), okLine);
	assert.ok(okLine.includes('remote SW 9000'), okLine);
	assert.ok(okLine.includes('50 bytes / 1 SMS'), okLine);
	const badLine = ramStepLine({ name: 'LOAD (1/9)', por_status: 'por_ok', por_sw: '6700',
		por_error: 'remote SW 6700', sw: '9000', bytes: 274, segments: 3 }, 1);
	assert.ok(badLine.startsWith('\u274c'), badLine);
	assert.ok(badLine.includes('remote SW 6700 (Wrong length in Lc)'), badLine);
	const porLine = ramStepLine({ name: 'LOAD', por_status: 'por_error_cntr_low' }, 2);
	assert.ok(porLine.includes('PoR error cntr_low'), porLine);
	const noPor = ramStepLine({ name: 'LOAD', por_status: 'no_por' }, 3);
	assert.ok(noPor.startsWith('\u2705') && noPor.includes('no PoR'), noPor);
});

test('ramOpChanged clears the executed status only on a real op change', () => {
	const els = fakeRamDocument(['ram-op', 'ram-install-params', 'ram-result', 'ram-explorer', 'ram-steps', 'ram-progress']);
	_ramOpLast = null;
	els['ram-op'].value = 'explore';
	ramOpChanged();
	assert.ok(!els['ram-result'].classList.contains('hidden'));
	els['ram-result'].classList.remove('hidden');
	els['ram-steps'].classList.remove('hidden');
	ramOpChanged();
	assert.ok(!els['ram-result'].classList.contains('hidden'), 'same op must keep the result');
	els['ram-op'].value = 'install-cap';
	ramOpChanged();
	assert.ok(els['ram-result'].classList.contains('hidden'));
	assert.ok(els['ram-steps'].classList.contains('hidden'));
	assert.ok(els['ram-explorer'].classList.contains('hidden'));
	assert.ok(els['ram-progress'].classList.contains('hidden'));
	assert.ok(!els['ram-install-params'].classList.contains('hidden'));
});

test('ramRender keeps the selected card preset across rebuilds', () => {
	const els = fakeRamDocument(['ram-card-sel', 'ram-op', 'ram-install-params', 'ram-result', 'ram-explorer', 'ram-steps', 'ram-progress']);
	globalThis.cards = [{ name: 'A' }, { name: 'B' }, { name: 'C' }];
	_ramCardIdx = null;
	_ramOpLast = 'explore';
	els['ram-op'].value = 'explore';
	ramRender();
	assert.strictEqual(els['ram-card-sel'].value, '');
	els['ram-card-sel'].value = '1';
	ramRender();
	assert.strictEqual(els['ram-card-sel'].value, '1');
	els['ram-card-sel'].value = '';
	_ramCardIdx = 2;
	ramRender();
	assert.strictEqual(els['ram-card-sel'].value, '2');
	globalThis.cards = [{ name: 'A' }];
	_ramCardIdx = 2;
	ramRender();
	assert.strictEqual(els['ram-card-sel'].value, '');
	delete globalThis.cards;
});

test('ramApplyCard remembers a valid picked preset', () => {
	globalThis.cards = [{ name: 'A' }, { name: 'B' }];
	let applied = null;
	globalThis.cardsApply = i => { applied = i; };
	_ramCardIdx = null;
	ramApplyCard('1');
	assert.strictEqual(_ramCardIdx, 1);
	assert.strictEqual(applied, '1');
	ramApplyCard('');
	assert.strictEqual(_ramCardIdx, 1, 'invalid pick must not forget the preset');
	delete globalThis.cards;
	delete globalThis.cardsApply;
});

test('ramExecute commits the dropdown selection before running', async () => {
	const els = fakeRamDocument(['ram-card-sel', 'ram-op', 'ram-install-params', 'ram-result', 'ram-explorer', 'ram-steps', 'ram-progress']);
	globalThis.cards = [{ name: 'A' }];
	globalThis.getRamSpParams = () => ({ kicKey: '11', kidKey: '22' });
	let explored = false;
	globalThis.ramExplore = async () => { explored = true; };
	globalThis.alert = () => {};
	_ramCardIdx = null;
	els['ram-card-sel'].value = '0';
	els['ram-op'].value = 'explore';
	await ramExecute();
	assert.strictEqual(_ramCardIdx, 0);
	assert.ok(explored);
	delete globalThis.cards;
	delete globalThis.getRamSpParams;
	delete globalThis.ramExplore;
	delete globalThis.alert;
});

test('ramCardIdxAfterRemove keeps the remembered index aligned', () => {
	assert.strictEqual(ramCardIdxAfterRemove(2, 0), 1);
	assert.strictEqual(ramCardIdxAfterRemove(0, 0), null);
	assert.strictEqual(ramCardIdxAfterRemove(0, 2), 0);
	assert.strictEqual(ramCardIdxAfterRemove(null, 1), null);
});
