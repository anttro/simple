const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractBlock(startMarker, endMarker) {
	const start = html.indexOf(startMarker);
	const end = html.indexOf(endMarker, start);
	if (start < 0 || end < 0) throw new Error('block not found: ' + startMarker);
	return html.slice(start, end);
}

// The codec map's select fields reference ACCESSTECH; only the date/time
// codec is exercised here, but the map literal still evaluates that list.
eval(extractBlock('const ACCESSTECH = [', 'const EVENT_FORMS = {').replace(/^const /gm, 'var '));
eval(extractBlock('// ===== PLI data dictionary =====', 'async function pysimPliRender')
	.replace(/^const /gm, 'var '));

test('the PLI date/time codec uses the 8.39 TP-SCTS coding', () => {
	const c = PLI_CODECS['03'];
	assert.strictEqual(c.tlv, '2607');
	// +03:00: swapped semi-octet BCD digits, the time zone in quarters of an
	// hour (0x21 = 12 quarters) - the eventDateTimeTlv / _encode_scts vectors
	assert.strictEqual(c.encode({ dt: '2026-09-30', tm: '21:55:00', tz: '+03:00' }),
		'260762900312550021');
	assert.deepStrictEqual(c.decode('260762900312550021'),
		{ dt: '2026-09-30', tm: '21:55:00', tz: '+03:00' });
	// negative zones carry the sign bit 0x08: -2:30 = 10 quarters -> 0x09
	assert.strictEqual(c.encode({ dt: '2026-01-05', tm: '08:07:09', tz: '-02:30' }),
		'260762105080709009');
	assert.deepStrictEqual(c.decode('260762105080709009'),
		{ dt: '2026-01-05', tm: '08:07:09', tz: '-02:30' });
	// 'FF' time zone = unknown (TS 123 040 9.2.3.11), never a bogus offset
	assert.strictEqual(c.decode('2607629003125500FF').tz, 'unknown');
	// a truncated value still returns null instead of throwing
	assert.strictEqual(c.decode('2607123'), null);
});

test('the PLI codec tags follow TS 101 220 Table 7.23', () => {
	// 05 Timing Advance: 2E/AE (C6 is a 3GPP2 tag)
	assert.strictEqual(PLI_CODECS['05'].tlv, 'AE02');
	assert.strictEqual(PLI_CODECS['05'].encode({ status: '0', ta: '0' }), 'AE020000');
	// 08 IMEISV: 62/E2 (94 is the IMEI tag)
	assert.strictEqual(PLI_CODECS['08'].tlv, 'E208');
	assert.strictEqual(PLI_CODECS['08'].encode({ imeisv: '4901542032375108' }),
		'E2089410450223731580');
	// 09 Network search mode: 65/E5, '00' Manual / '01' Automatic (8.75)
	assert.strictEqual(PLI_CODECS['09'].tlv, 'E501');
	assert.strictEqual(PLI_CODECS['09'].fields[0].opts[0].l, 'Manual');
	assert.strictEqual(PLI_CODECS['09'].fields[0].opts[1].l, 'Automatic');
	// 0A Battery state: 63/E3 is an enum, not a percentage (8.76)
	assert.strictEqual(PLI_CODECS['0A'].tlv, '6301');
	assert.strictEqual(PLI_CODECS['0A'].encode({ charge: '4' }), '630104');
	assert.strictEqual(PLI_CODECS['0A'].encode({ charge: '0' }), '630100');
});

test('the IMEI encoder pads the odd 15th digit with F (TS 124 008)', () => {
	// the encoder's digits are lowercase (the server accepts either case)
	assert.strictEqual(encImeiPli('490154203237518'), '94104502237315f8');
	assert.strictEqual(decImei('94104502237315F8'), '490154203237518');
});

test('the server PLI defaults decode through their codecs', () => {
	// mirrors PLI_DEFAULTS in server.py (v3.23.1; a drift guard for the codecs)
	const defaults = {
		'00': '930500F1100000', '01': '940894104502237315F8',
		'04': 'AD02656E', '05': 'AE020000', '06': 'BF0108',
		'08': 'E2089410450223731580', '09': 'E50101',
		'0A': '630104', '0E': 'BF020308',
	};
	for (const [code, hex] of Object.entries(defaults)) {
		assert.ok(PLI_CODECS[code].decode(hex), code + ' must decode');
	}
	assert.strictEqual(PLI_CODECS['01'].decode('940894104502237315F8').imei,
		'490154203237518');
	assert.deepStrictEqual(PLI_CODECS['09'].decode('E50101'), { mode: '1' });
	assert.deepStrictEqual(PLI_CODECS['0A'].decode('630104'), { charge: '4' });
});
