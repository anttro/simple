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
const FNS = ['berLenStr', 'buildApdu', 'escHtml', 'esc', 'chainInit', 'chainRamBuildRowHex', 'ramFmtLifecycle', 'ramFmtPrivileges', 'ramRenderExploreHtml', 'ramStepLine', 'ramGetStatusApdu', 'ramDeleteApdu',
	'stkParamsBuild', 'ramRemoteSwOk', 'spPorAccepted', 'ramIncrementCntr', 'ramDeleteFromExplorer', 'ramListingSpi2', 'ramRemoveFromExplorer', 'ramHasInstance', 'ramExpandedQueryApdu',
	'_parseRawElfEntry', '_parseRawAppEntry', 'ramParseElfStatus', 'ramParseAppStatus', 'parseTLV', '_parseE3Entry',
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

// ramApplyCard/ramExecute delegate to the SP-form helpers; their real
// behaviour (and the preset re-read before an operation) is covered by
// cards_counter.test.js, so keep them as global stubs here.
globalThis.cardsApply = () => {};
globalThis.spRefreshFromPreset = () => '';

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
		tkTar: 'AF4D01',
		tkAd: '',
		tkServices: '0',
	});
}

test('UICC toolkit nested inside EA (m=2, services 0)', () => {
	const apdu = genRamApdu();
	assert.ok(apdu.includes('EA13801100000002010102020002011603AF4D0100'), apdu);
});

test('UICC m=1 emits single pair', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '1', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '0', tkLastid: '00',
		tkChannels: '0', tkMsl: '16', tkTar: 'AF4D01', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EA11800F0000000101010002011603AF4D0100'), apdu);
});

test('UICC m=3 fills middle pair with 0000', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '3', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '3', tkLastid: '03',
		tkChannels: '0', tkMsl: '16', tkTar: 'AF4D01', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EA158013000000030101000003030002011603AF4D0100'), apdu);
});

test('UICC services 7 appended as final byte', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ea', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '2', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '2', tkLastid: '02',
		tkChannels: '0', tkMsl: '16', tkTar: 'AF4D01', tkAd: '', tkServices: '7',
	});
	assert.ok(apdu.includes('EA13801100000002010102020002011603AF4D0107'), apdu);
});

test('SIM (CA) access domain FIRST, no services byte', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ca', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '2', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '2', tkLastid: '02',
		tkChannels: '0', tkMsl: '16', tkTar: 'AF4D01', tkAd: '5A', tkServices: '0',
	});
	assert.ok(apdu.includes('EF14CA12015A00000002010102020002011603AF4D01'), apdu);
});

test('SIM (CA) blank access domain emits length byte 00', () => {
	const apdu = genRamResult({
		cmd: 'install-install', aid: 'A000000151000000', priv: '00',
		tkEnabled: true, tkMode: 'ca', tkPriority: '0', tkTimers: '0', tkTextlen: '0',
		tkMenus: '2', tkFirstpos: '1', tkFirstid: '01', tkLastpos: '2', tkLastid: '02',
		tkChannels: '0', tkMsl: '16', tkTar: 'AF4D01', tkAd: '', tkServices: '0',
	});
	assert.ok(apdu.includes('EF13CA110000000002010102020002011603AF4D01'), apdu);
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
		tkChannels: '0', tkMsl: '16', tkTar: 'AF4D01', tkAd: '', tkServices: '0',
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

test('ramGetStatusApdu builds the compact chain, never the expanded form', () => {
	// GP 11.4.2.2: compact listings (P2.b2=0) carry the chained GET RESPONSE
	assert.strictEqual(ramGetStatusApdu('80', '00'), '80F28000024F0000C0000000');
	assert.strictEqual(ramGetStatusApdu('20', '01'), '80F22001024F0000C0000000');
	// the expanded TLV form (P2=02/03) takes no GET RESPONSE and is not built:
	// its GET STATUS bytes would be `80F2<P1>02 04 4F00 5C.. 00`
	assert.ok(!ramGetStatusApdu('20', '01').startsWith('80F2200204'),
		'expanded form must not be generated by this builder');
	assert.ok(!html.includes("for (const p2Init of ['02', '00'])"),
		'the P2=02 attempt must be gone');
	// the ISD-only query is a single occurrence: no next-occurrence paging
	assert.ok(/if \(p1 === '80'\) break;/.test(html), 'ISD next-occurrence guard missing');
});

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
	globalThis.cardsApply = () => {};
	delete globalThis.cards;
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

test('ramExpandedQueryApdu matches the reference trace bytes', () => {
	// TCA Loader: `AA14 2212 80F28002 0C 4F00 5C08 4F9F70C5C4CCCEEA 00`
	for (const p1 of ['80', '40', '20', '10']) {
		assert.strictEqual(ramExpandedQueryApdu(p1),
			'80F2' + p1 + '020C4F005C084F9F70C5C4CCCEEA00');
	}
});

test('ramParseAppStatus parses the expanded E3 listing (vendor traces)', () => {
	// Explore_NC.log, ISD: E3 { 4F AID, 9F70 LC, C5 privileges }
	const isd = ramParseAppStatus('E3134F08A0000000030000009F70010FC5039AFE80');
	assert.strictEqual(isd.length, 1);
	assert.strictEqual(isd[0].aid, 'A000000003000000');
	assert.strictEqual(isd[0].lifecycle, '0F');
	assert.strictEqual(isd[0].privileges, '9AFE80');
	assert.strictEqual(isd[0].type, 'app');
	// Explore_ET.log, application entry: adds the ELF AID (C4) and SD AID (CC)
	const apps = ramParseAppStatus('E3374F10A1130001180001FFFFFFFF89A10039089F700107C503000000C410A1130001180001FFFFFFFF89A1003900CC08A000000151000000');
	assert.strictEqual(apps.length, 1);
	assert.strictEqual(apps[0].aid, 'A1130001180001FFFFFFFF89A1003908');
	assert.strictEqual(apps[0].elfAid, 'A1130001180001FFFFFFFF89A1003900');
	assert.strictEqual(apps[0].sdAid, 'A000000151000000');
	assert.strictEqual(apps[0].privileges, '000000');
});

test('ramParseElfStatus parses the expanded E3 listing (NP trace)', () => {
	const elfs = ramParseElfStatus('E31B4F07A00000015153509F700101CE020100CC08A000000151000000');
	assert.strictEqual(elfs.length, 1);
	assert.strictEqual(elfs[0].aid, 'A0000001515350');
	assert.strictEqual(elfs[0].lifecycle, '01');
	assert.strictEqual(elfs[0].version, '0100');
	assert.strictEqual(elfs[0].sdAid, 'A000000151000000');
	assert.strictEqual(elfs[0].type, 'elf');
});

test('ramParseElfStatus lists the compact ELF and module listings (F0414C46416101)', () => {
	// Exact bytes from a live RAM Explore: the P1=20 ELF page and the P1=10
	// (ELF+modules) page.  The old 0x10-scan walk dropped everything after the
	// first entry; the deterministic AID walk lists them all.
	const elfPage = '10A0000000090005FFFFFFFF8911000000010010A0000000871005FFFFFFFF8913100000010010A0000000871005FFFFFFFF8914100000010010A0000000090005FFFFFFFF8912000000010010A0000000871005FFFFFFFF8913200000010010A0000000090005FFFFFFFF8913000000010010A0000000090005FFFFFFFF8911010000010010D2760001180002FF49100A89AA060F00010010A1130001180001FFFFFFFF89A1003900010010A1130001180002FFF7100E8904000200010007F0414C464161010100';
	const elfs = ramParseElfStatus(elfPage);
	const f041 = elfs.find(r => r.aid === 'F0414C46416101');
	assert.ok(f041, JSON.stringify(elfs.map(r => r.aid)));
	assert.strictEqual(f041.lifecycle, '01');
	assert.ok(elfs.some(r => r.aid === 'A1130001180002FFF7100E8904000200'), 'A113 ELF missing');
	assert.ok(elfs.some(r => r.aid === 'A0000000090005FFFFFFFF8912000000'), 'uicc.toolkit ELF missing');

	const modulesPage = '10A1130001180001FFFFFFFF89A100390001000110A1130001180001FFFFFFFF89A100390810A1130001180002FFF7100E890400020001000210A1130001180002FFF7100E890400020810A1130001180002FFF7100E89494D450807F0414C4641610101000108F0414C4641610101';
	const mods = ramParseElfStatus(modulesPage, true);
	const f041Row = mods.find(r => r.aid === 'F0414C46416101');
	assert.ok(f041Row, JSON.stringify(mods.map(r => r.aid)));
	assert.deepStrictEqual(f041Row.moduleAids, ['F0414C4641610101']);
});

test('ramParseAppStatus keeps 16-byte AIDs (no rawLen-1 truncation)', () => {
	const page = '08D276000005AA3F010704'
		+ '0FD276000005AA060200000000B000000700'
		+ '0FD276000005AA060200000000B00001070010A1130001180001FFFFFFFF89A10039080700'
		+ '10A1130001180002FFF7100E8904000208070010A1130001180002FFF7100E89494D45080700';
	const apps = ramParseAppStatus(page);
	assert.deepStrictEqual(apps.map(r => r.aid), [
		'D276000005AA3F01', 'D276000005AA060200000000B00000',
		'D276000005AA060200000000B00001',
		'A1130001180001FFFFFFFF89A1003908', 'A1130001180002FFF7100E8904000208',
		'A1130001180002FFF7100E89494D4508',
	]);
	assert.strictEqual(apps[3].lifecycle, '07');
});

test('ramDeleteApdu builds the GP DELETE with the 4F AID TLV (F0414C46416101)', () => {
	// GP Card Spec v2.3.1 Table 11-20/23: P1=00, P2.b8 = object / object+related,
	// data = '4F' AID TLV, case 3 (no Le - a trailing Le byte becomes a
	// phantom command on the card's SCP80 layer, live 2026-09-28).  The old
	// handler called an undefined helper and no APDU was ever sent.
	assert.strictEqual(ramDeleteApdu('F0414C46416101', false),
		'80E40000094F07F0414C46416101');
	assert.strictEqual(ramDeleteApdu('F0414C46416101', true),
		'80E40080094F07F0414C46416101');
	assert.strictEqual(ramDeleteApdu('A1130001180002FFF7100E8904000200', true),
		'80E40080124F10A1130001180002FFF7100E8904000200');
	assert.strictEqual(ramDeleteApdu('', false), '');
	// the dead helper reference must not come back
	assert.ok(!html.includes('_ber_len('), 'undefined _ber_len() call is back');
});

test('ramRemoteSwOk mirrors the server success set', () => {
	for (const sw of ['9000', '6113', '62F1', '6310', 'CAFE']) {
		assert.ok(ramRemoteSwOk(sw), sw);
	}
	for (const sw of ['6700', '6F00', '6A88', '', null]) {
		assert.ok(!ramRemoteSwOk(sw), String(sw));
	}
});

test('ramHasInstance matches instances by AID prefix', () => {
	const apps = [{ aid: 'F0414C4641610101' },
		{ aid: 'A1130001180001FFFFFFFF89A1003908' }];
	assert.strictEqual(ramHasInstance(apps, 'f0414c46416101'), true);
	assert.strictEqual(ramHasInstance(apps, 'F0414C4641610101'), true);
	assert.strictEqual(ramHasInstance(apps, 'F0414C46416001'), false);
	assert.strictEqual(ramHasInstance([], 'F0414C46416101'), false);
});

test('the Explore result offers Install / Make selectable actions', () => {
	global.t = s => s;
	const out = ramRenderExploreHtml(
		null,
		[],
		[{ aid: 'A000000151000000', lifecycle: '03', privileges: '' }],
		[{ aid: 'F0414C46416101', lifecycle: '01', version: '0.0',
			moduleAids: ['F0414C4641610101'] }]
	);
	// the module has no instance yet -> Install; the INSTALLED instance -> Make selectable
	assert.ok(out.includes("ramInstallFromExplorer('F0414C46416101','F0414C4641610101')"), out);
	assert.ok(out.includes("ramMakeSelectableFromExplorer('A000000151000000')"), out);
	delete global.t;
});

test('the single-APDU ops are wired', () => {
	assert.ok(/value="install-app"/.test(html), 'the INSTALL [for install] op is missing');
	assert.ok(/value="make-selectable"/.test(html), 'the make selectable op is missing');
	assert.ok(/async function ramInstallApp\(sp, op\)/.test(html), 'ramInstallApp is missing');
	assert.ok(/pysimFetch\('\/api\/ram-install-app'/.test(html), 'the endpoint call is missing');
});

test('ramRemoveFromExplorer drops the object locally (cascade drops its applets)', () => {
	let renders = 0;
	globalThis.ramRenderExplorer = () => { renders++; };
	_ramExplorerData = {
		apps: [{ aid: 'F0414C4641610101' }, { aid: 'F0414C4641610199' },
			{ aid: 'A1130001180001FFFFFFFF89A1003908' }],
		elfs: [{ aid: 'F0414C46416101' }, { aid: 'A1130001180001FFFFFFFF89A1003900' }],
	};
	assert.strictEqual(ramRemoveFromExplorer('f0414c4641610101', false), true);
	assert.deepStrictEqual(_ramExplorerData.apps.map(a => a.aid),
		['F0414C4641610199', 'A1130001180001FFFFFFFF89A1003908']);
	assert.strictEqual(renders, 1);
	assert.strictEqual(ramRemoveFromExplorer('F0414C46416101', true), true);
	assert.deepStrictEqual(_ramExplorerData.elfs.map(e => e.aid),
		['A1130001180001FFFFFFFF89A1003900']);
	assert.deepStrictEqual(_ramExplorerData.apps.map(a => a.aid),
		['A1130001180001FFFFFFFF89A1003908'], 'an unrelated applet stays');
	assert.strictEqual(renders, 2);
	assert.strictEqual(ramRemoveFromExplorer('DEADBEEF', false), false);
	delete globalThis.ramRenderExplorer;
});

test('getRamSpParams reads the computed SPI2 byte, not the base select', () => {
	const fn = extractFunc(html, 'getRamSpParams');
	assert.ok(/spi2: document\.getElementById\('sp-spi2-hex'\)/.test(fn), fn);
});

test('getRamSpParams normalises the packet TAR to three bytes', () => {
	const fn = extractFunc(html, 'getRamSpParams');
	assert.ok(/tar:[\s\S]*?padEnd\(6, '0'\)\.slice\(0, 6\),/.test(fn), fn);
});

test('ramListingSpi2 requests the SMS-submit PoR for the listing queries', () => {
	// Apps (40) and ELF (20/10) listings can exceed the ENVELOPE response;
	// with SPI2=0x01 the card answers actual_response_sms_submit and the data
	// never arrives (the live "Partial - Apps" bug).
	for (const p1 of ['40', '20', '10']) assert.strictEqual(ramListingSpi2(p1), '21', p1);
	for (const p1 of ['80', '00', 'FF']) assert.strictEqual(ramListingSpi2(p1), '01', p1);
});

function stubDeleteEnv(sendResult) {
	// ramShowProgress/ramHideProgress are the real extracted helpers
	const els = fakeRamDocument(['ram-result', 'ram-steps', 'ram-progress', 'ram-progress-text']);
	const calls = { refresh: [], saved: [], sent: [], explored: null, removed: null };
	globalThis.t = s => s;
	globalThis.spRefreshFromPreset = sel => { calls.refresh.push(sel); };
	globalThis.getRamSpParams = () => ({ cntr: '0000000005', kicKey: 'AA', kidKey: 'BB' });
	globalThis.confirm = () => true;
	globalThis.alert = () => {};
	globalThis.ramShowProgress = () => {};   // not part of the extracted FNS
	globalThis.ramSendOta = async (apdu, sp) => {
		calls.sent.push({ apdu: apdu, cntr: sp.cntr });
		return sendResult;
	};
	globalThis.ramSaveCntr = c => { calls.saved.push(c); };
	globalThis.ramExplore = async sp => { calls.explored = sp.cntr; };
	return { els, calls };
}

function unstubDeleteEnv() {
	for (const k of ['getRamSpParams', 'confirm', 'alert', 'ramShowProgress',
		'ramSendOta', 'ramSaveCntr', 'ramExplore']) {
		delete globalThis[k];
	}
	globalThis.spRefreshFromPreset = () => '';   // the top-level stub
}

test('ramDeleteFromExplorer refreshes the preset and drops the record locally', async () => {
	const { els, calls } = stubDeleteEnv({ success: true,
		por: { response_status: 'por_ok', decoded: { last_status_word: '9000' } } });
	_ramExplorerData = { apps: [{ aid: 'F0414C4641610101' }], elfs: [{ aid: 'F0414C46416101' }] };
	globalThis.ramRenderExplorer = () => {};
	await ramDeleteFromExplorer('F0414C46416101', true);
	delete globalThis.ramRenderExplorer;
	assert.deepStrictEqual(calls.refresh, ['ram-card-sel'],
		'the preset must be re-read before the operation');
	assert.strictEqual(calls.sent.length, 1);
	assert.ok(calls.sent[0].apdu.startsWith('80E40080'), calls.sent[0].apdu);
	assert.strictEqual(calls.sent[0].cntr, '0000000005');
	assert.deepStrictEqual(calls.saved, ['0000000006'],
		'the accepted packet advances the saved counter');
	assert.deepStrictEqual(_ramExplorerData.elfs, [],
		'a successful cascade delete drops the package from the Explore result');
	assert.deepStrictEqual(_ramExplorerData.apps, [],
		'and the package applets (AID prefix) with it');
	assert.strictEqual(calls.explored, null, 'no re-explore is run');
	assert.strictEqual(els['ram-result'].textContent, 'OK');
	unstubDeleteEnv();
});

test('ramDeleteFromExplorer leaves the counter untouched on a rejected packet', async () => {
	const { calls } = stubDeleteEnv({ success: true, por: { response_status: 'cntr_low' } });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.deepStrictEqual(calls.saved, [], 'a rejected packet must not advance the counter');
	assert.strictEqual(calls.explored, null, 'no re-explore after a rejected delete');
	unstubDeleteEnv();
});

test('ramDeleteFromExplorer advances but does not re-explore on a failed remote command', async () => {
	const { calls } = stubDeleteEnv({ success: true,
		por: { response_status: 'por_ok', decoded: { last_status_word: '6A88' } } });
	await ramDeleteFromExplorer('F0414C46416101', false);
	assert.deepStrictEqual(calls.saved, ['0000000006'],
		'the card consumed the packet, so the counter still advances');
	assert.strictEqual(calls.explored, null, 'a failed DELETE must not re-explore');
	unstubDeleteEnv();
});
