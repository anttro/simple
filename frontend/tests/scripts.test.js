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

// The extracted functions run in this module's scope; their free variables
// (`cards`, `t`, ...) resolve to globals we stub here.
global.t = (s) => s;
eval(extractFunc(html, 'cardsPskMap'));
eval(extractFunc(html, 'scriptsParseApdus'));
eval(extractFunc(html, 'scp81DeleteApdus'));
eval(extractFunc(html, 'scp81LogLine'));
eval(extractFunc(html, 'scp81LogEntryHtml'));
eval(extractFunc(html, 'scp81GroupResults'));
eval(extractFunc(html, 'scp81ScriptStateText'));
eval(extractFunc(html, 'scriptKindLabel'));
global.esc = (s) => String(s == null ? '' : s)
	.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

test('scriptKindLabel maps the stored kinds to the template labels', () => {
	assert.strictEqual(scriptKindLabel('explore'), 'Explore ISD');
	assert.strictEqual(scriptKindLabel('Explore'), 'Explore ISD');
	assert.strictEqual(scriptKindLabel('delete'), 'Delete AID');
	assert.strictEqual(scriptKindLabel('install'), 'Install from .cap');
	assert.strictEqual(scriptKindLabel('empty'), 'Empty');
	assert.strictEqual(scriptKindLabel(undefined), 'Empty');
});

test('explore template is the reference administration sequence', () => {	const m = /const SCP81_EXPLORE_APDUS = \[(.*?)\];/s.exec(html);
	assert.ok(m, 'SCP81_EXPLORE_APDUS not found');
	const apdus = [...m[1].matchAll(/'([0-9A-F]+)'/g)].map(x => x[1]);
	assert.deepStrictEqual(apdus, ['80CAFF2100', '80F28002024F0000', '80CA008500',
		'80F24002024F0000', '80F22002024F0000', '80F21002024F0000']);
	for (const a of apdus) {
		assert.ok(/^[0-9A-F]+$/.test(a) && a.length % 2 === 0, a);
	}
});

test('cardsPskMap keeps only cards with both identity and key', () => {
	global.cards = [
		{name: 'A', pskIdentity: 'id-1', pskKey: '00112233445566778899aabbccddeeff'},
		{name: 'B', pskIdentity: 'id-2'},                       // no key
		{name: 'C', pskKey: '00112233'},                        // no identity
		{name: 'D', pskIdentity: 'id-4', pskKey: 'AA BB CC'},   // spaces stripped
		{name: 'E', pskIdentity: 'id-5', pskKey: 'not-hex'},
	];
	assert.deepStrictEqual(cardsPskMap(), [
		{identity: 'id-1', psk_hex: '00112233445566778899aabbccddeeff'},
		{identity: 'id-4', psk_hex: 'AABBCC'},
	]);
	global.cards = [];
	assert.deepStrictEqual(cardsPskMap(), []);
});

test('scriptsParseApdus accepts comments and whitespace, rejects bad lines', () => {
	assert.deepStrictEqual(
		scriptsParseApdus('80CAFF2100\n\n80F2 4002 024F 0000  # listing\n; note\n80E60200AB00'),
		{apdus: ['80CAFF2100', '80F24002024F0000', '80E60200AB00']});
	assert.strictEqual(scriptsParseApdus('80CAFF2100').error, undefined);
	assert.deepStrictEqual(scriptsParseApdus('ZZ'), {error: 'ZZ'});
	assert.deepStrictEqual(scriptsParseApdus('80CAF'), {error: '80CAF'});
	assert.deepStrictEqual(scriptsParseApdus('80CAFF2'), {error: '80CAFF2'});
});

test('scp81DeleteApdus builds GP DELETE APDUs per AID', () => {
	assert.deepStrictEqual(scp81DeleteApdus(['A000000003000000'], '00'),
		['80E4000008A00000000300000000']);
	assert.deepStrictEqual(scp81DeleteApdus(['A000000003000000', 'A000000100'], '80'),
		['80E4800008A00000000300000000', '80E4800005A00000010000']);
	assert.deepStrictEqual(scp81DeleteApdus([], '00'), []);
});

test('scp81LogEntryHtml marks matched and unknown PSK identities', () => {
	global.cards = [{name: 'Foobar SIM', pskIdentity: 'id-1'}];
	global.cardsPskName = (identity) =>
		(global.cards.find(c => c.pskIdentity === identity) || {}).name || '';
	const matched = scp81LogEntryHtml(
		{seq: 1, kind: 'tls-handshake', cipher: 'PSK-AES128-CBC-SHA256',
		 identity: 'id-1', psk_match: true});
	assert.match(matched, /id=id-1/);
	assert.match(matched, /\[matched: Foobar SIM\]/);
	const unknown = scp81LogEntryHtml(
		{seq: 2, kind: 'tls-handshake', identity: 'who', psk_match: false});
	assert.match(unknown, /\[unknown identity\]/);
	const rejected = scp81LogEntryHtml({seq: 3, kind: 'tls-psk-unknown', identity: 'who'});
	assert.match(rejected, /\[unknown identity\]/);
	// non-handshake lines get no badge
	const plain = scp81LogEntryHtml({seq: 4, kind: 'script-send', index: 1, apdu: '80CAFF2100'});
	assert.doesNotMatch(plain, /\[\]/);
});

test('scp81ScriptStateText separates script progress from listing pages', () => {
	// untouched / empty scripts show no state line
	assert.strictEqual(scp81ScriptStateText({kind: 'none', total: 0}), '');
	assert.strictEqual(scp81ScriptStateText({kind: 'Explore', total: 0}), '');
	// still executing the configured APDUs
	assert.strictEqual(
		scp81ScriptStateText({kind: 'Explore', total: 6, done: [0, 1, 2, 3], script: []}),
		'Explore: 4/6 executed');
	// a configured APDU is awaiting the card's report
	assert.strictEqual(
		scp81ScriptStateText({kind: 'Explore', total: 6, done: [0, 1, 2, 3, 4],
			pending: {index: 9, pos: 5, page: false, apdu: '80F21002024F0000'}}),
		'Explore: 5/6 executed · waiting for card');
	// all script APDUs executed; only a listing page is in flight
	assert.strictEqual(
		scp81ScriptStateText({kind: 'Explore', total: 6, done: [0, 1, 2, 3, 4, 5],
			pending: {index: 17, pos: null, page: true, apdu: '80F21003024F0000'},
			pages: 11, complete: false}),
		'Explore: 6/6 executed · listing pages (11)…');
	// queued page, nothing sent yet
	assert.strictEqual(
		scp81ScriptStateText({kind: 'Explore', total: 6, done: [0, 1, 2, 3, 4, 5],
			pending: null, pages: 11, pages_queued: 1, complete: false}),
		'Explore: 6/6 executed · listing pages (11)…');
	// everything drained
	assert.strictEqual(
		scp81ScriptStateText({kind: 'Explore', total: 6, done: [0, 1, 2, 3, 4, 5],
			pending: null, pages: 11, pages_queued: 0, complete: true}),
		'Explore: 6/6 executed · completed');
});

test('scp81GroupResults groups pages by originating command', () => {
	const groups = scp81GroupResults({results: [
		{index: 1, pos: 0, page: false, apdu: '80F24002024F0000', rapdu: 'E3', sw: 'CAFE'},
		{index: 2, pos: null, page: true, apdu: '80F24003024F0000', rapdu: 'E3', sw: '9000'},
		{index: 3, pos: 1, page: false, apdu: '80CAFF2100', rapdu: 'FF21', sw: '9000'},
	]});
	assert.strictEqual(groups.length, 2);
	assert.strictEqual(groups[0].key, '80F240');
	assert.strictEqual(groups[0].results.length, 2);   // origin + continuation page
	assert.strictEqual(groups[1].key, '80CAFF');
});
