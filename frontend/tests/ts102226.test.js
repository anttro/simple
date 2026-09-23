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

// Command builders and the expanded-script wiring they feed.
// Reference vectors are computed from the pinned specs:
//   TS 102 221 11.1.7/11.1.8 (SEARCH RECORD, INCREASE), 11.3 (SET/RETRIEVE DATA)
//   TS 102 222 tables 2/7/16/17 (CREATE/DELETE/RESIZE FILE, FCP templates)
//   TS 151 011 9.2.7/9.2.8 (SEEK, INCREASE for 2G SIM)
//   TS 102 226 9.2.1-9.2.4 (PUSH), 5.2.1 (Command TLVs)
//   GlobalPlatform Card Spec 11.8 (PUT KEY)
const FNS = ['berLenStr', 'buildApdu', 'buildSelect', 'escHtml', 'esc', 'chainInit',
	'chainKind', 'chainIsEmbedded', 'chainCommands', 'chainBuildRowHex',
	'chainSimBuildRowHex', 'chainRamBuildRowHex', 'chainPushData', '_hotaAsciiHex',
	'chainApduList', 'chainBuildFcp', 'pushSectionApdu'];
let code = '';
for (const f of FNS) code += extractFunc(html, f) + '\n';
for (const c of ['CHAIN_CMDS_SIM', 'CHAIN_CMDS_USIM', 'CHAIN_CMDS_RAM']) {
	const m = html.match(new RegExp('const ' + c + ' = \\[[\\s\\S]*?\\n\\];'));
	if (!m) throw new Error('catalog not found: ' + c);
	code += m[0].replace('const ', 'var ') + '\n';
}
code += html.match(/const _chains = \{\};/)[0].replace('const ', 'var ') + '\n';
code += html.match(/const CHAIN_LISTENERS = \{\};/)[0].replace('const ', 'var ') + '\n';
// The real chain renderers are DOM-bound; the pure builders under test only
// need chainRender as a no-op, and chainApduList's splitter is exercised with
// a stub (the splitter itself is covered by apdu_parse.test.js).
code += 'function chainRender() {}\n';
code += 'var __split = [];\nfunction parseCompactApdus() { return __split; }\n';
eval(code);

function row(cmd, fields) {
	return { cmd: cmd, fields: fields || {} };
}

test('the command catalogs cover the TS 102 226 table 7.1/8.1/9.1 gaps', () => {
	const sim = CHAIN_CMDS_SIM.map(c => c.value);
	const usim = CHAIN_CMDS_USIM.map(c => c.value);
	const ram = CHAIN_CMDS_RAM.map(c => c.value);
	['search-record', 'increase'].forEach(c => assert.ok(sim.includes(c), 'SIM ' + c));
	['search-record', 'increase', 'create-file', 'delete-file', 'resize-file', 'set-data', 'retrieve-data']
		.forEach(c => assert.ok(usim.includes(c), 'USIM ' + c));
	['put-key', 'push'].forEach(c => assert.ok(ram.includes(c), 'RAM ' + c));
});

test('chainKind maps the stock and embedded chains; embedded ones hide GET RESPONSE', () => {
	assert.strictEqual(chainKind('chain-sim'), 'sim');
	assert.strictEqual(chainKind('chain-usim'), 'usim');
	assert.strictEqual(chainKind('chain-ram'), 'ram');
	assert.strictEqual(chainKind('ber-sim-7'), 'sim');
	assert.strictEqual(chainKind('ber-usim-12'), 'usim');
	assert.strictEqual(chainKind('ber-ram-1'), 'ram');
	assert.ok(chainIsEmbedded('ber-sim-3'));
	assert.ok(!chainIsEmbedded('chain-sim'));
	// GET RESPONSE is not used in the expanded format (TS 102 226 5.2.1.1).
	assert.ok(chainCommands('chain-sim').some(c => c.value === 'get-response'));
	assert.ok(!chainCommands('ber-sim-3').some(c => c.value === 'get-response'));
	assert.ok(!chainCommands('ber-ram-3').some(c => c.value === 'get-response'));
});

test('USIM SEARCH RECORD: simple and enhanced (TS 102 221 11.1.7)', () => {
	_chains['chain-usim'] = { rows: [] };
	// Simple forward from record 2 (P2=10), pattern ABCD, Le=00.
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('search-record', { srMode: '10', srRec: '02', srPattern: 'ABCD', le: '00' })),
		'00A2021002ABCD00');
	// Enhanced (P2=18): 2-byte indication + search string.
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('search-record', { srMode: '18', srRec: '00', srInd: '1800', srPattern: 'AB', le: '00' })),
		'00A20018031800AB00');
	// Backward from record 1 (P2=12).
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('search-record', { srMode: '12', srRec: '01', srPattern: 'AA', le: '00' })),
		'00A2011201AA00');
	// Missing pattern: no APDU.
	assert.strictEqual(chainSimBuildRowHex('chain-usim', 0, row('search-record', { srPattern: '' })), '');
});

test('SIM SEEK: type/mode in P2, pattern in the data field (TS 151 011 9.2.7)', () => {
	_chains['chain-sim'] = { rows: [] };
	// Type 1, from next forward.
	assert.strictEqual(
		chainSimBuildRowHex('chain-sim', 0, row('search-record', { srMode: '02', srPattern: '112233', le: '' })),
		'A0A2000203112233');
	// Type 2, from the beginning: Le appended.
	assert.strictEqual(
		chainSimBuildRowHex('chain-sim', 0, row('search-record', { srMode: '10', srPattern: '11', le: '00' })),
		'A0A20010011100');
});

test('INCREASE: TS 102 221 (USIM, optional SFI) and TS 51 011 (SIM)', () => {
	_chains['chain-sim'] = { rows: [] };
	_chains['chain-usim'] = { rows: [] };
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('increase', { value: '000001', le: '00' })),
		'003200000300000100');
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('increase', { sfi: '05', value: '000001', le: '00' })),
		'003285000300000100');
	// TS 51 011 9.2.8: class A0, P1/P2 = 00, P3 = 03.
	assert.strictEqual(
		chainSimBuildRowHex('chain-sim', 0, row('increase', { value: '000001' })),
		'A032000003000001');
	assert.strictEqual(chainSimBuildRowHex('chain-usim', 0, row('increase', { value: '' })), '');
});

test('CREATE/DELETE/RESIZE FILE (TS 102 222 tables 2, 7, 16)', () => {
	_chains['chain-usim'] = { rows: [] };
	const fcp = '62138202002183026F078A01058C027F0080020010';
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('create-file', { fcp: fcp })),
		'00E0000015' + fcp);
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('create-file', { fcp: '83026F07' })),
		'');
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('delete-file', { fid: '6F07' })),
		'00E40000026F07');
	// RESIZE FILE uses CLA 80 (TS 102 222 table 1: '8X'/'CX').
	const rfcp = '620883026F0780020010';
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('resize-file', { mode: '00', fcp: rfcp })),
		'80D400000A' + rfcp);
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('resize-file', { mode: '01', fcp: rfcp })),
		'80D401000A' + rfcp);
});

test('SET DATA / RETRIEVE DATA block coding (TS 102 221 table 11.35)', () => {
	_chains['chain-usim'] = { rows: [] };
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('set-data', { sdMode: '80', sfi: '00', value: '80017F' })),
		'00DB00800380017F');
	// Next block with SFI 5.
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('set-data', { sdMode: '00', sfi: '05', value: '80017F' })),
		'00DB00050380017F');
	// First block, tag 5C, Le 00.
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('retrieve-data', { sdMode: '80', sfi: '00', tag: '5C', le: '00' })),
		'00CB0080015C00');
	// No tag: case 2.
	assert.strictEqual(
		chainSimBuildRowHex('chain-usim', 0, row('retrieve-data', { sdMode: '80', sfi: '00', tag: '', le: '00' })),
		'00CB008000');
});

test('PUT KEY (GlobalPlatform Card Spec 11.8)', () => {
	_chains['chain-ram'] = { rows: [] };
	assert.strictEqual(
		chainRamBuildRowHex(0, row('put-key', { keyVer: '01', keyId: '02', data: '1122334455667788' })),
		'80D00102081122334455667788');
	assert.strictEqual(chainRamBuildRowHex(0, row('put-key', { data: '' })), '');
});

test('PUSH: BIP channel opening and identification packet (TS 102 226 9.2.1/9.2.4)', () => {
	_chains['chain-ram'] = { rows: [] };
	// No TLVs: the application uses its defaults, so the APDU is case 1.
	assert.strictEqual(chainRamBuildRowHex(0, row('push', { pushType: '01', pushData: '' })), '80EC0101');
	assert.strictEqual(
		chainRamBuildRowHex(0, row('push', { pushType: '01', pushData: '350103' })),
		'80EC010103350103');
	assert.strictEqual(
		chainRamBuildRowHex(0, row('push', { pushType: '04', pushIdent: '0102' })),
		'80EC01040436020102');
	assert.strictEqual(chainRamBuildRowHex(0, row('push', { pushType: '04', pushIdent: '' })), '80EC0104');
});

test('PUSH: CAT_TP link and TCP connection data fields (TS 102 226 9.2.2/9.2.3)', () => {
	// CAT_TP: transport level 3C (protocol type 00) + optional 39/36.
	assert.strictEqual(chainPushData({ pushType: '02', pushPort: '1F90' }), '3C03001F90');
	assert.strictEqual(
		chainPushData({ pushType: '02', pushPort: '1F90', pushSdu: '0200', pushIdent: 'ABCD' }),
		'3C03001F90390202003602ABCD');
	assert.strictEqual(chainPushData({ pushType: '02', pushPort: '' }), '');
	// TCP: bearer 35, transport 3C (protocol type 02), destination 3E, NAA 47.
	assert.strictEqual(
		chainPushData({ pushType: '03', pushPort: '0050', pushAddr: '210A000001', pushApn: 'internet' }),
		'3501033C030200503E05210A0000014708696E7465726E6574');
	assert.strictEqual(
		chainPushData({ pushType: '03', pushPort: '0050', pushBearer: '350104' }),
		'3501043C03020050');
	_chains['chain-ram'] = { rows: [] };
	assert.strictEqual(
		chainRamBuildRowHex(0, row('push', { pushType: '02', pushPort: '1F90' })),
		'80EC0102053C03001F90');
});

test('CREATE FILE FCP template skeleton (TS 102 222 table 4)', () => {
	_chains['chain-usim'] = { rows: [] };
	const r1 = row('create-file', { fid: '6F07', structure: 'transparent', fileSize: '0010', lcsi: '05' });
	_chains['chain-usim'].rows.push(r1);
	chainBuildFcp('chain-usim', 0, 'create');
	assert.strictEqual(r1.fields.fcp, '62138202012183026F078A01058C027F0080020010');
	const r2 = row('create-file', { fid: '6F3A', structure: 'linear_fixed', recLen: '006E', fileSize: '0226', lcsi: '05' });
	_chains['chain-usim'].rows.push(r2);
	chainBuildFcp('chain-usim', 1, 'create');
	assert.strictEqual(r2.fields.fcp, '621582040221006E83026F3A8A01058C027F0080020226');
	// Record files default the file size to one record.
	const r3 = row('create-file', { fid: '6F3A', structure: 'cyclic', recLen: '0010', fileSize: '' });
	_chains['chain-usim'].rows.push(r3);
	chainBuildFcp('chain-usim', 2, 'create');
	assert.ok(r3.fields.fcp.endsWith('8002' + '0010'));
	// RESIZE template: FID + file size only (table 17).
	const r4 = row('resize-file', { fid: '6F07', fileSize: '0010' });
	_chains['chain-usim'].rows.push(r4);
	chainBuildFcp('chain-usim', 3, 'resize');
	assert.strictEqual(r4.fields.fcp, '620883026F0780020010');
});

test('pushSectionApdu builds the guided §9 push commands (Remote APDU pill)', () => {
	// BIP channel opening: OPEN CHANNEL TLVs are optional.
	assert.strictEqual(pushSectionApdu('bip', { request: '01', pushData: '350103' }), '80EC010103350103');
	assert.strictEqual(pushSectionApdu('bip', { request: '01', pushData: '' }), '80EC0101');
	// CAT_TP: the destination port is mandatory (9.2.2).
	assert.strictEqual(pushSectionApdu('bip', { request: '02', pushPort: '1F90' }), '80EC0102053C03001F90');
	assert.strictEqual(pushSectionApdu('bip', { request: '02', pushPort: '' }), '');
	// TCP: port and destination address are mandatory (9.2.3).
	assert.strictEqual(
		pushSectionApdu('tcp', { request: '03', pushPort: '0050', pushAddr: '210A000001', pushApn: 'internet' }),
		'80EC010319' + '3501033C030200503E05210A0000014708696E7465726E6574');
	assert.strictEqual(pushSectionApdu('tcp', { request: '03', pushPort: '0050' }), '');
	assert.strictEqual(pushSectionApdu('tcp', { request: '03', pushAddr: '210A000001' }), '');
	// Identification packet: optional data, ICCID is used when absent (9.2.4).
	assert.strictEqual(pushSectionApdu('tcp', { request: '04', pushIdent: '0102' }), '80EC01040436020102');
	assert.strictEqual(pushSectionApdu('tcp', { request: '04', pushIdent: '' }), '80EC0104');
});

test('chainApduList drops GET RESPONSE and splits multi-APDU rows', () => {
	_chains['ber-usim-1'] = { rows: [
		row('read-binary', { offset: '0000', le: '00' }),
		row('get-response', {}),
		row('select', { method: 'chain', chain: '3F00,2FE2' }),
	] };
	// Single APDUs pass through; the SELECT chain row is split by the parser.
	__split = [{ label: 'APDU', hex: '00A40000023F00' }, { label: 'APDU', hex: '00A40000022FE2' }];
	const list = chainApduList('ber-usim-1');
	assert.deepStrictEqual(list, ['00B0000000', '00A40000023F00', '00A40000022FE2']);
});

test('an embedded chain contributes a C-APDU TLV to the script', () => {
	// genBerRowValue is covered in ber.test.js; here the chain-side contract:
	// one row per C-APDU, built with the same builders as the stock chains.
	_chains['ber-ram-9'] = { rows: [row('put-key', { keyVer: '01', keyId: '01', data: 'AA' })] };
	assert.strictEqual(chainBuildRowHex('ber-ram-9', 0), '80D0010101AA');
});
