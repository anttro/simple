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

function findNode(tree, label) {
	if (tree.label === label) return tree;
	if (tree.children) {
		for (const c of tree.children) {
			const r = findNode(c, label);
			if (r) return r;
		}
	}
	return null;
}

function findNodes(tree, label) {
	const out = [];
	if (tree.label === label) out.push(tree);
	if (tree.children) {
		for (const c of tree.children) out.push(...findNodes(c, label));
	}
	return out;
}

const consts = ['BER_QUAL', 'BER_DEVICES', 'BER_TONES', 'GSM7_ALPHABET', 'GSM7_EXT_MAP'];
let prefix = '';
for (const c of consts) {
	const m = html.match(new RegExp('const\\s+' + c + '\\s*=\\s*(?:\\[|\\{)[\\s\\S]*?(?:\\n[\\]\\}];|[\\]\\}];\\n)'));
	if (!m) throw new Error(c + ' not found');
	prefix += m[0].replace(/^const /, 'var ') + '\n';
}
const start = html.indexOf('// ===== C-APDU Parser =====');
const end = html.indexOf('// ===== Response Parser =====');
eval(prefix + html.slice(start, end)
	.replace(/^const PARSE_INS = /m, 'var PARSE_INS = ')
	.replace(/^const PRIVILEGE_NAMES = /m, 'var PRIVILEGE_NAMES = '));

test('Compact RAM INSTALL [for install] UICC', () => {
	const tree = parseHexTree('80E60C00214F08A000000151000000C70100EA128010000000020101020200011603B0000100');
	assert.strictEqual(tree.label, 'Compact C-APDU chain');
	assert.strictEqual(tree.children.length, 1);
	const apdu = tree.children[0];
	assert.strictEqual(apdu.label, 'APDU');
	assert.ok(findNode(apdu, 'INS').desc.includes('INSTALL'));
});

test('Compact RAM INSTALL [for install] SIM (CA) access domain 5A', () => {
	const tree = parseHexTree('80E60C00204F08A000000151000000C70100CA11015A000000020101020200011603B00001');
	const apdu = tree.children[0];
	assert.ok(findNode(apdu, 'INS').desc.includes('INSTALL'));
});

test('Expanded AA DISPLAY TEXT UCS2 "HI" + 5s', () => {
	const tree = parseHexTree('AA1681130103012101020281820D040048004904020105');
	assert.strictEqual(tree.label, 'Expanded Script (AA)');
	assert.strictEqual(tree.children.length, 1);
	assert.strictEqual(tree.children[0].label, 'Immediate Action');
});

test('Expanded AA DISPLAY TEXT GSM7 "HI"', () => {
	const tree = parseHexTree('AA0F810D0103012101020281820D02C824');
	assert.strictEqual(tree.children.length, 1);
	assert.strictEqual(tree.children[0].label, 'Immediate Action');
});

test('Expanded AA REFRESH + file list', () => {
	const tree = parseHexTree('AA10810E01030101000202818212036F3B2F');
	assert.strictEqual(tree.children.length, 1);
	assert.strictEqual(tree.children[0].label, 'Immediate Action');
});

test('Expanded AE80 PLAY TONE + Error Action no action', () => {
	const tree = parseHexTree('AE80810C0103200101020281820E010182000000');
	assert.strictEqual(tree.label, 'Expanded Script (AE80)');
	assert.strictEqual(tree.children.length, 2);
	assert.strictEqual(tree.children[0].label, 'Immediate Action');
	assert.strictEqual(tree.children[1].label, 'Error Action');
});

test('Immediate Action CR-set tags (real-world encoding)', () => {
	const tree = parseHexTree('AA0E8101818109810301010482028182');
	assert.strictEqual(tree.children.length, 2);
	assert.strictEqual(tree.children[0].label, 'Immediate Action');
	assert.strictEqual(tree.children[1].label, 'Immediate Action');
});

test('CR-set DISPLAY TEXT UCS2 "HI" + 5s', () => {
	const tree = parseHexTree('AA1581138103012101820281828D040048004984020105');
	assert.strictEqual(tree.children.length, 1);
});

test('CR-set PLAY TONE (tag 8E)', () => {
	const tree = parseHexTree('AA0E810C8103012001820281828E0101');
	assert.strictEqual(tree.children.length, 1);
});

test('CR-set REFRESH file list (tag 92)', () => {
	const tree = parseHexTree('AA0F810D81030101048202818292023F00');
	assert.strictEqual(tree.children.length, 1);
});

test('Expanded AA Error Action = proactive DISPLAY TEXT', () => {
	const tree = parseHexTree('AA0F820D0103012101020281820D022852');
	assert.strictEqual(tree.children.length, 1);
	assert.strictEqual(tree.children[0].label, 'Error Action');
});

test('Expanded AA 22 C-APDU row (GET STATUS)', () => {
	const tree = parseHexTree('AA09220780F22000024F00');
	assert.strictEqual(tree.children.length, 1);
	assert.strictEqual(tree.children[0].label, 'C-APDU');
});

test('Chained SIM SELECT + UPDATE RECORD', () => {
	const tree = parseHexTree('A0A40000026F3BDC0102032B2F2D');
	assert.strictEqual(tree.label, 'Compact C-APDU chain');
	assert.strictEqual(tree.children.length, 2);
	assert.strictEqual(tree.children[0].label, 'APDU');
});

test('INTERNAL AUTHENTICATE', () => {
	const tree = parseHexTree('8088000008800F3495BA2355CD');
	assert.strictEqual(tree.children.length, 1);
	const apdu = tree.children[0];
	assert.strictEqual(apdu.label, 'APDU');
	assert.ok(findNode(apdu, 'INS').desc.includes('AUTHENTICATE'));
	assert.ok(findNode(apdu, 'Data'));
});

test('baseCompTag clears CR bit for 0x80-0x9F', () => {
	assert.strictEqual(baseCompTag('81'), '01');
	assert.strictEqual(baseCompTag('82'), '02');
	assert.strictEqual(baseCompTag('8D'), '0D');
	assert.strictEqual(baseCompTag('8E'), '0E');
	assert.strictEqual(baseCompTag('92'), '12');
	assert.strictEqual(baseCompTag('84'), '04');
	assert.strictEqual(baseCompTag('85'), '05');
	assert.strictEqual(baseCompTag('01'), '01');
	assert.strictEqual(baseCompTag('22'), '22');
});

test('parseTlvList handles BER-TLVs', () => {
	const tlvs = parseTlvList('810301210182028182');
	assert.strictEqual(tlvs.length, 2);
	assert.strictEqual(tlvs[0].tag, '81');
	assert.strictEqual(tlvs[0].length, 3);
	assert.strictEqual(tlvs[0].value, '012101');
	assert.strictEqual(tlvs[1].tag, '82');
});

test('parseBerLen handles short and long form lengths (ISO 7816-4 5.2)', () => {
	assert.deepStrictEqual(parseBerLen('1200', 0), { len: 18, consumed: 2 });
	assert.deepStrictEqual(parseBerLen('8112', 0), { len: 18, consumed: 4 });
	assert.deepStrictEqual(parseBerLen('820100', 0), { len: 256, consumed: 6 });
	assert.deepStrictEqual(parseBerLen('820182' + '0102030405060708', 0), { len: 386, consumed: 6 });
});

test('parseTlvList parses long-form lengths (81/82)', () => {
	const short = parseTlvList('6212' + '8202412183026F078A010580020009880110');
	assert.strictEqual(short.length, 1);
	assert.strictEqual(short[0].tag, '62');
	assert.strictEqual(short[0].length, 18);

	// same content with a long-form outer length
	const long81 = parseTlvList('628112' + '8202412183026F078A010580020009880110');
	assert.strictEqual(long81.length, 1);
	assert.strictEqual(long81[0].length, 18);
	assert.strictEqual(long81[0].value, short[0].value);

	// 2-byte length form
	const long82 = parseTlvList('62820004' + '80020009');
	assert.strictEqual(long82.length, 1);
	assert.strictEqual(long82[0].length, 4);
	assert.strictEqual(long82[0].value, '80020009');

	// inner TLV with a long-form length (A5 81 05 85 03 00 00 00)
	const inner = parseTlvList('A581058503000000');
	assert.strictEqual(inner.length, 1);
	assert.strictEqual(inner[0].tag, 'A5');
	assert.strictEqual(inner[0].length, 5);
	assert.strictEqual(inner[0].value, '8503000000');
});

test('gsm7Decode unpacks "HI" from C824', () => {
	const bytes = new Uint8Array([0xC8, 0x24]);
	assert.strictEqual(gsm7Decode(bytes), 'HI');
});

test('decodeTextData legacy UCS2 without DCS (regression: lead 00 is not a DCS)', () => {
	assert.strictEqual(decodeTextData('00480049'), 'HI');
});

test('decodeTextData legacy Cyrillic UCS2 (uniform 04 high bytes)', () => {
	assert.strictEqual(decodeTextData('041f04400438043204350442'), 'Привет');
});

test('decodeTextData DCS-prefixed UCS2 (08)', () => {
	assert.strictEqual(decodeTextData('0800480049'), 'HI');
});

test('decodeTextData DCS-prefixed GSM7 packed (00)', () => {
	assert.strictEqual(decodeTextData('00C824'), 'HI');
});

test('decodeTextData DCS-prefixed unpacked 8-bit (04)', () => {
	assert.strictEqual(decodeTextData('04414243'), 'ABC');
});

test('decodeTextData explicit DCS 08 with malformed payload returns ?', () => {
	assert.strictEqual(decodeTextData('0841'), '?');
});

test('PRIVILEGE_NAMES matches GPC v2.3 Tables 11-7/11-8/11-9', () => {
	assert.strictEqual(PRIVILEGE_NAMES[0][7], 'Mandated DAP Verification');
	assert.strictEqual(PRIVILEGE_NAMES[1][2], 'Token Verification');
	assert.strictEqual(PRIVILEGE_NAMES[1][7], 'Global Service');
	assert.strictEqual(PRIVILEGE_NAMES[2][0], 'Receipt Generation');
	assert.strictEqual(PRIVILEGE_NAMES[2][3], 'Contactless Self-Activation');
});

test('decodePrivileges byte 2 b6 is Token Verification per GPC v2.3 Table 11-8', () => {
	assert.ok(decodePrivileges('0020').includes('Token Verification'));
});

test('decodePrivileges byte 1: DAP Verification vs Mandated DAP (GP Table 11-7)', () => {
	assert.strictEqual(decodePrivileges('80'), 'Security Domain');
	assert.strictEqual(decodePrivileges('C0'), 'Security Domain, DAP Verification');
	assert.strictEqual(decodePrivileges('C1'), 'Security Domain, Mandated DAP Verification');
	assert.strictEqual(decodePrivileges('40'), 'None');   // b7 alone is no privilege
	assert.strictEqual(decodePrivileges('01'), 'None');   // b1 alone is only the flag
});

test('decodePrivileges', () => {
	assert.ok(decodePrivileges('00').includes('None'));
	assert.ok(decodePrivileges('80').includes('Security Domain'));
});

test('LV INSTALL [for install] decode with labeled fields and trailing Le', () => {
	const tree = parseHexTree('80E60C0011000008A000000151000000010002C9000000');
	const apdu = tree.children[0];
	assert.ok(findNode(apdu, 'P1').desc.includes('for install + for make selectable'));
	const elf = findNode(apdu, 'ELF AID');
	assert.ok(elf);
	assert.strictEqual(elf.desc, '(empty)');
	assert.strictEqual(findNode(apdu, 'Application AID').desc, 'A000000151000000');
	assert.strictEqual(findNode(apdu, 'Privileges').desc, 'None');
	const params = findNode(apdu, 'Install parameters');
	assert.ok(params.children.some(c => c.label.includes('C9')));
	assert.strictEqual(findNode(apdu, 'Install token').desc, '(empty)');
	assert.strictEqual(tree.children[1].label, 'Le');
});

test('Legacy TLV INSTALL falls back to raw Data node', () => {
	const tree = parseHexTree('80E60C00214F08A000000151000000C70100EA13801100000002010102020002011603B0000100');
	const apdu = tree.children[0];
	const data = findNodes(apdu, 'Data');
	assert.strictEqual(data.length, 1);
	assert.ok(!data[0].children || !data[0].label.includes('ELF'));
	assert.ok(findNode(apdu, 'P1').desc.includes('for install + for make selectable'));
});

test('Install parameters EF nesting exposes inner CA TLV', () => {
	const tree = parseHexTree('80E60C0025000008A000000151000000010016C900EF12CA1000000000030101000003030002011600' + '00');
	const apdu = tree.children[0];
	const params = findNode(apdu, 'Install parameters');
	assert.ok(params.children.some(c => c.label.includes('EF')));
	const ef = params.children.find(c => c.label.includes('EF'));
	assert.ok(ef.children.some(c => c.label.includes('CA')));
});

test('GET DATA case 2 renders P3 as Le', () => {
	const tree = parseHexTree('80CA5F5000');
	const apdu = tree.children[0];
	assert.ok(findNode(apdu, 'Le'));
	assert.ok(!findNode(apdu, 'Lc'));
});

test('GET DATA case 4 decodes Lc + tag list + Le', () => {
	const tree = parseHexTree('80CA2F00025C0000');
	const apdu = tree.children[0];
	assert.ok(findNode(apdu, 'Lc'));
	assert.strictEqual(findNode(apdu, 'Data').desc, 'Tag list: 5C00');
	assert.ok(findNode(apdu, 'Le'));
});

test('SET STATUS raw AID data labeled', () => {
	const tree = parseHexTree('80F0408008A000000151000000');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'P1').desc, 'Application or SSD');
	assert.strictEqual(findNode(apdu, 'P2').desc, 'LOCKED');
	assert.ok(findNode(apdu, 'Data').desc.startsWith('AID (raw):'));
});

test('SET STATUS ISD card state label', () => {
	const tree = parseHexTree('80F0807F00');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'P1').desc, 'ISD');
	assert.strictEqual(findNode(apdu, 'P2').desc, 'CARD_LOCKED');
	assert.ok(!findNode(apdu, 'Data'));
});

test('Trailing single byte consumed as Le in compact chain', () => {
	const tree = parseHexTree('A0A40000026F3BDC0102032B2F2D00');
	const last = tree.children[tree.children.length - 1];
	assert.strictEqual(last.label, 'Le');
	assert.strictEqual(last.hex, '00');
});

test('ACTIVATE FILE case-1 (4 bytes)', () => {
	const tree = parseHexTree('00440000');
	const apdu = tree.children[0];
	assert.strictEqual(apdu.hex, '00440000');
	assert.strictEqual(apdu.desc, 'no data, no Le');
	assert.ok(!findNode(apdu, 'P3'));
	assert.ok(!findNode(apdu, 'Lc'));
});

test('ACTIVATE FILE legacy 5-byte empty-Lc form', () => {
	const tree = parseHexTree('0044000000');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'P3').desc, 'empty Lc (legacy form)');
});

test('ACTIVATE FILE with FID data', () => {
	const tree = parseHexTree('00440000026F3B');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'Data').desc, 'FID 6F3B');
});

test('READ RECORD next mode: P1 ignored note', () => {
	const tree = parseHexTree('00B2000200');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'P1').desc, 'ignored');
	assert.ok(findNode(apdu, 'P2').desc.includes('next record'));
});

test('UPDATE RECORD absolute mode with record number', () => {
	const tree = parseHexTree('00DC010404AABBCCDD');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'P1').desc, 'record #1');
	assert.strictEqual(findNode(apdu, 'P2').desc, 'absolute/current');
});

test('SELECT P1/P2 descriptors', () => {
	const tree = parseHexTree('00A40804047FFF6FC500');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'P1').desc, 'path from MF');
	assert.ok(findNode(apdu, 'P2').desc.includes('FCP'));
});

test('Immediate Action EFRMA reference (01-7F)', () => {
	const tree = parseHexTree('AA03810105');
	const row = tree.children[0].children[0];
	assert.strictEqual(row.label, 'Reference to EFRMA record');
	assert.strictEqual(row.desc, 'Record 0x05');
});

test('CLA 84-87 labeled GlobalPlatform secure messaging', () => {
	const tree = parseHexTree('8482030010' + '00112233445566778899AABBCCDDEEFF');
	const apdu = tree.children[0];
	assert.strictEqual(findNode(apdu, 'CLA').desc, 'GlobalPlatform (secure messaging)');
});
test('New PARSE_INS labels decode (FETCH le, RESIZE D4, RETRIEVE CB, AUTH 89)', () => {
	const fetchTree = parseHexTree('8012000020');
	assert.ok(findNode(fetchTree.children[0], 'INS').desc.includes('FETCH'));
	assert.ok(findNode(fetchTree.children[0], 'Le'));

	const resizeTree = parseHexTree('80D4000004' + '00000FA0');
	assert.ok(findNode(resizeTree.children[0], 'INS').desc.includes('RESIZE FILE'));
	assert.strictEqual(findNode(resizeTree.children[0], 'Data').desc, '00000FA0');

	const retrTree = parseHexTree('80CB000000');
	assert.ok(findNode(retrTree.children[0], 'INS').desc.includes('RETRIEVE DATA'));

	const authTree = parseHexTree('0089000008' + '0011223344556677');
	assert.ok(findNode(authTree.children[0], 'INS').desc.includes('odd INS'));
});

test('READ BINARY SFI mode and offset descriptors', () => {
	const sfi = parseHexTree('00B0830602');
	const p1sfi = findNode(sfi.children[0], 'P1').desc;
	assert.ok(p1sfi.includes('SFI') && p1sfi.includes('3'), p1sfi);
	assert.strictEqual(findNode(sfi.children[0], 'P2').desc, 'offset low');

	const off = parseHexTree('00B0010202');
	assert.strictEqual(findNode(off.children[0], 'P1').desc, 'offset high');
});

test('SELECT P2 response field per ETSI b5-b3', () => {
	const fcp = parseHexTree('00A40804047FFF6FC500');
	assert.ok(findNode(fcp.children[0], 'P2').desc.includes('FCP'));
	const none = parseHexTree('00A4000C026F3B');
	assert.ok(findNode(none.children[0], 'P2').desc.includes('no response data'));
});

test('live RFM captures decode (silent hop / implied CLA / SIM chain)', () => {
	// capture line 1 head: silent relative hop 09/0C
	const t1 = parseHexTree('00A4090C026F46');
	const a1 = t1.children[0];
	assert.ok(findNode(a1, 'P1').desc.includes('path from current DF'));
	assert.ok(findNode(a1, 'P2').desc.includes('no response data'));
	assert.strictEqual(findNode(a1, 'Data').desc, '6F46');

	// capture line 2: silent hop + READ RECORD as implied-CLA continuation
	const t2 = parseHexTree('00A4090C026FC5B2010414');
	assert.strictEqual(t2.children.length, 2);
	assert.strictEqual(t2.children[1].label, 'APDU (implied CLA)');
	assert.ok(findNode(t2.children[1], 'INS').desc.includes('READ RECORD'));

	// capture line 3: SIM chained FID selects + UPDATE RECORD (34 x FF)
	const t3 = parseHexTree('A0A40000023F00A0A40000027F20A0A40000026FC5A0DC010422' + 'FF'.repeat(34));
	assert.strictEqual(t3.children.length, 4);
	assert.strictEqual(t3.children[3].label, 'APDU');
	assert.ok(findNode(t3.children[3], 'INS').desc.includes('UPDATE RECORD'));
	assert.ok(findNode(t3.children[3], 'Lc').desc.includes('34'));
});
