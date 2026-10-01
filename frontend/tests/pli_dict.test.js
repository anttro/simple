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
