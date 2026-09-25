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

eval(extractFunc(html, 'scp81LogLine'));

test('scp81LogLine renders a BIP open entry', () => {
	assert.strictEqual(
		scp81LogLine({ seq: 4, kind: 'open', channel: 1, requested: '77.221.153.19:10174', target: '127.0.0.1:8443' }),
		'4 open ch1 77.221.153.19:10174 -> 127.0.0.1:8443');
});

test('scp81LogLine renders a TLS request with the GP headers', () => {
	assert.strictEqual(
		scp81LogLine({ seq: 5, kind: 'tls-request', method: 'POST', uri: '/server/adminagent?cmd=1', agent: '0123456789', bytes: 0 }),
		'5 tls-request from=0123456789 POST /server/adminagent?cmd=1');
});

test('scp81LogLine renders handshake and errors', () => {
	assert.strictEqual(
		scp81LogLine({ seq: 6, kind: 'tls-handshake', cipher: 'PSK-AES128-CBC-SHA256', identity: 'id-1' }),
		'6 tls-handshake id=id-1 PSK-AES128-CBC-SHA256');
	assert.strictEqual(scp81LogLine({ seq: 7, kind: 'tls-error', error: 'boom' }), '7 tls-error boom');
});

test('scp81 log covers the dump mode kinds', () => {
	assert.strictEqual(scp81LogLine({ seq: 1, kind: 'dump-rx', bytes: 71 }), '1 dump-rx 71B');
});

test('scp81LogLine renders script entries', () => {
	assert.strictEqual(
		scp81LogLine({ seq: 9, kind: 'script-send', index: 1, apdu: '80CAFF2100' }),
		'9 script-send #1 80CAFF2100');
	assert.strictEqual(
		scp81LogLine({ seq: 12, kind: 'script-rapdu', index: 1, sw: '9000', bytes: 14 }),
		'12 script-rapdu #1 SW 9000 14B');
	assert.strictEqual(
		scp81LogLine({ seq: 13, kind: 'script-memory', applets: 4, free_nv: 61600, free_volatile: 2048 }),
		'13 script-memory applets=4 free NV=61600 free vol=2048');
});

eval(html.match(/const JC_AID_NAMES = \{[\s\S]*?\n\};/)[0].replace(/^const /, 'var '));
eval(html.match(/const JC_AID_RIDS = \{[\s\S]*?\n\};/)[0].replace(/^const /, 'var '));
eval(extractFunc(html, 'jcAidName'));
eval(extractFunc(html, 'jcAidSuffix'));
eval(extractFunc(html, 'scp81DecodeGetStatus'));
eval(extractFunc(html, 'scp81GroupResults'));
eval(extractFunc(html, 'scp81ResultLines'));

test('scp81DecodeGetStatus decodes complete entries', () => {
	const entries = scp81DecodeGetStatus('E32A4F08A0000000030000009F70010FC50380DE00C40BD276000005AAFFCAFE0010CC08A000000003000000');
	assert.strictEqual(entries.length, 1);
	assert.strictEqual(entries[0].aid, 'A000000003000000');
	assert.strictEqual(entries[0].lifecycle, '0F');
	assert.strictEqual(entries[0].privileges, '80DE00');
});

test('scp81DecodeGetStatus reads module AIDs and skips truncated tails', () => {
	const entries = scp81DecodeGetStatus('E31B4F07A00000015153509F700101CE0201008408A000000151535041' + 'E3204F08D27600');
	assert.strictEqual(entries.length, 1);
	assert.strictEqual(entries[0].aid, 'A0000001515350');
	assert.strictEqual(entries[0].modules[0], 'A000000151535041');
});

test('scp81GroupResults merges pages under one command', () => {
	const groups = scp81GroupResults({ results: [
		{ index: 4, apdu: '80F24002024F0000', sw: 'CAFE', rapdu: 'E3114F08A0000000030000009F70010FC50100' },
		{ index: 5, apdu: '80F24002114F0F', sw: '9000', rapdu: 'E3114F08A0000000030000009F70010FC50100' },
		{ index: 1, apdu: '80CAFF2100', sw: '9000', rapdu: 'FF210B81010D8202C5D683020962' },
	] });
	assert.strictEqual(groups.length, 2);
	assert.strictEqual(groups[0].results.length, 2);
	assert.strictEqual(groups[1].key, '80CAFF');
});

test('scp81ResultLines decodes the memory page', () => {
	const lines = scp81ResultLines({ apdu: '80CAFF2100', results: [
		{ rapdu: 'FF210B81010D8202C5D683020962', sw: '9000' } ] });
	assert.strictEqual(lines[0], 'applets=13  free NV=50646 B  free vol=2402 B');
});

test('scp81ResultLines decodes GET STATUS entries', () => {
	global.decodePrivileges = () => 'Security Domain';
	try {
		const lines = scp81ResultLines({ apdu: '80F24002024F0000', results: [
			{ rapdu: 'E3114F08A0000000030000009F70010FC50100', sw: '9000' } ] });
		assert.strictEqual(lines[0], 'A000000003000000  life=0F  [Security Domain]');
	} finally {
		delete global.decodePrivileges;
	}
});

test('scp81ResultLines annotates standard package AIDs and RID hints', () => {
	const lines = scp81ResultLines({ apdu: '80F24002024F0000', results: [
		{ rapdu: 'E30E4F08A0000001510000009F70010F', sw: '9000' },
		{ rapdu: 'E3174F07A00000015153509F7001018408A000000062010101', sw: '9000' },
	] });
	assert.strictEqual(lines[0], 'A000000151000000 (GlobalPlatform Issuer Security Domain)  life=0F');
	assert.strictEqual(lines[1], 'A0000001515350 (GlobalPlatform RID)  life=01  module=A000000062010101 (javacard.framework.service)');
});

eval(extractFunc(html, 'scp81Ascii'));
eval(extractFunc(html, 'scp81Bcd'));
eval(extractFunc(html, 'scp81DecodeAdminParams'));
eval(extractFunc(html, 'scp81CmdLabel'));

test('scp81DecodeAdminParams decodes the stored 0085 answer', () => {
	const hex = '856F84248103014003820281828500B50103B902058EC70403475042BC03020582BE05215BD50502851814383937303178787878787878787878787878787802400186070001250300100089248A096C6F63616C686F73748B1438393730317878787878787878787878787878788C012F';
	const lines = scp81DecodeAdminParams(hex);
	assert.ok(lines.includes('PSK id=89701xxxxxxxxxxxxxxx  KVN/KID=40/01'));
	assert.ok(lines.includes('retry counter=1  timer=00:10:00'));
	assert.ok(lines.includes('host=localhost'));
	assert.ok(lines.includes('agent=89701xxxxxxxxxxxxxxx'));
	assert.ok(lines.includes('uri=/'));
	assert.ok(lines.includes('  apn=GPB'));
	assert.ok(lines.includes('  dest=91.213.5.2'));
});

test('scp81CmdLabel names the explore commands', () => {
	assert.strictEqual(scp81CmdLabel('80CAFF2100'), 'GET DATA FF21 (extended card resources)');
	assert.strictEqual(scp81CmdLabel('80F24002024F0000'), 'GET STATUS P1=40 (applications and security domains)');
	assert.strictEqual(scp81CmdLabel('80F22002024F0000'), 'GET STATUS P1=20 (executable load files)');
	assert.strictEqual(scp81CmdLabel('80F21002024F0000'), 'GET STATUS P1=10 (executable load files and modules)');
	assert.strictEqual(scp81CmdLabel('80E8800000'), 'LOAD');
});
