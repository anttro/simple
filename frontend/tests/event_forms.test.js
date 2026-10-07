const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractBlock(startMarker, endMarker) {
	const start = html.indexOf(startMarker);
	const end = html.indexOf(endMarker, start);
	if (start < 0 || end < 0) throw new Error('block not found');
	return html.slice(start, end);
}

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

// Rewrite top-level const -> var so the maps leak out of sloppy-mode eval.
eval(extractBlock('const CMD_NAMES = {', 'function cmdQualifierShort').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'cmdQualifierShort'));
eval(extractBlock('const EVENT_ALIASES = {', 'function eventCode').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'eventCode'));
eval(extractBlock('const EVENT_NAMES = {', 'const REJECTION_CAUSES = [').replace(/^const /gm, 'var '));
eval(extractBlock('const REJECTION_CAUSES = [', 'const EVENT_FORMS = {').replace(/^const /gm, 'var '));
eval(extractBlock('const EVENT_FORMS = {', 'const PLI_QUALIFIERS = [').replace(/^const /gm, 'var '));
eval(extractBlock('const EVENT_INNER_MAX = ', 'function pysimEventSendForm').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'eventFieldsForRelease'));
eval(extractFunc(html, 'encPlmn'));
eval(extractFunc(html, 'bytesToHex'));
eval(extractFunc(html, 'esc'));
eval(extractFunc(html, 'eventFormFieldsHtml'));

test('CMD_NAMES decodes timer management and the BIP commands', () => {
	assert.strictEqual(CMD_NAMES['01'], 'REFRESH');
	assert.strictEqual(CMD_NAMES['10'], 'SET UP CALL');
	assert.strictEqual(CMD_NAMES['27'], 'TIMER MANAGEMENT');
	assert.strictEqual(CMD_NAMES['28'], 'SET UP IDLE MODE TEXT');
	assert.strictEqual(CMD_NAMES['40'], 'OPEN CHANNEL');
	assert.strictEqual(CMD_NAMES['41'], 'CLOSE CHANNEL');
	assert.strictEqual(CMD_NAMES['42'], 'RECEIVE DATA');
	assert.strictEqual(CMD_NAMES['43'], 'SEND DATA');
	assert.strictEqual(CMD_NAMES['44'], 'GET CHANNEL STATUS');
});

test('cmdQualifierShort decodes TIMER MANAGEMENT actions', () => {
	assert.strictEqual(cmdQualifierShort('27', 0x00), 'Start');
	assert.strictEqual(cmdQualifierShort('27', 0x01), 'Deactivate');
	assert.strictEqual(cmdQualifierShort('27', 0x02), 'Get');
});

test('cmdQualifierShort decodes OPEN CHANNEL qualifier flags', () => {
	assert.strictEqual(cmdQualifierShort('40', 0x00), 'OnDemand');
	assert.strictEqual(cmdQualifierShort('40', 0x01), 'Immediate');
	assert.strictEqual(cmdQualifierShort('40', 0x03), 'Immediate+AutoReconn');
	assert.strictEqual(cmdQualifierShort('40', 0x05), 'Background');
	assert.strictEqual(cmdQualifierShort('40', 0x0C), 'Background+DNS');
});

test('cmdQualifierShort returns empty for unknown types', () => {
	assert.strictEqual(cmdQualifierShort('99', 0x01), '');
});

test('EVENT_NAMES follows the pinned CAT spec and names the 3GPP events', () => {
	assert.strictEqual(EVENT_NAMES[0x0B], 'Access technology change (single access technology)');
	assert.strictEqual(EVENT_NAMES[0x14], 'Access technology change (multiple access technologies)');
	assert.strictEqual(EVENT_NAMES[0x19], 'Profile container');
	assert.strictEqual(EVENT_NAMES[0x1C], 'Poll interval negotiation');
	for (const [v, name] of [[0x11, '(I-)WLAN access status'], [0x12, 'Network rejection'],
		[0x15, 'CSG cell selection'], [0x17, 'IMS registration'], [0x18, 'Incoming IMS data'],
		[0x1D, 'Data connection status change'], [0x1E, 'CAG cell selection'],
		[0x1F, 'Slices status change'], [0x20, 'Reserved (future usage)']]) {
		assert.strictEqual(EVENT_NAMES[v], name, '0x' + v.toString(16));
	}
	// the "Reserved for 3GPP" spec cross-reference is never user-facing
	for (const [v, name] of Object.entries(EVENT_NAMES)) {
		assert.ok(!name.includes('Reserved for 3GPP'), v + ': ' + name);
	}
});

test('the poll interval negotiation event builds a Duration TLV', () => {
	const build = EVENT_FORMS[0x1C].build;
	assert.strictEqual(build({ unit: '1', interval: '45' }), '0402012D');
	assert.strictEqual(build({ unit: '0', interval: '2' }), '04020002');
	assert.strictEqual(build({ unit: '2', interval: '5' }), '04020205');
	assert.strictEqual(build({ unit: '1', interval: '' }), '0402011E');   // default 30 s
});

test('channel status event builds the B8 channel status TLV', () => {
	const build = EVENT_FORMS[0x0A].build;
	assert.strictEqual(EVENT_FORMS[0x0A].note, undefined);
	assert.strictEqual(build({ channel: '2', state: '128', info: '5' }), 'B8028205');
	assert.strictEqual(build({ channel: '1', state: '0', info: '0' }), 'B8020100');
	assert.strictEqual(build({ channel: '0', state: '64', info: '0' }), 'B8024000');
});

test('the data connection status change event builds the 7.5.25 object set', () => {
	const build = EVENT_FORMS[0x1D].build;
	assert.strictEqual(EVENT_FORMS[0x1D].note, undefined);
	// the four mandatory objects; every conditional object omitted
	assert.strictEqual(build({ status: '0', type: '0', ti: '00', loc_status: '0' }),
		'9D0100AA01001C01009B0100');
	// full form: cause, location information, access technology, APN, type
	assert.strictEqual(
		build({ status: '1', type: '2', cause: '26', ti: '85', mcc: '250', mnc: '01',
			lac: '00FF', cell: '0001', tech: '8', loc_status: '1', apn: 'internet', pdp_type: '3' }),
		'9D0101AA0102AE011A1C0185130752F01000FF0001BF01089B0101C708696E7465726E65740B0103');
	// an empty TI falls back to 00; the location defaults fill missing fields
	assert.strictEqual(build({ status: '2', type: '1', ti: '', mcc: '250', loc_status: '2' }),
		'9D0102AA01011C0100130752F010000000019B0102');
});

test('the access technology options follow the pinned 8.61 coding', () => {
	const opts = EVENT_FORMS[0x0B].fields[0].opts;
	assert.deepStrictEqual(opts.map(o => o.v), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
	assert.strictEqual(opts[1].l, 'TIA/EIA-553-A');
	assert.strictEqual(opts[2].l, 'TIA/EIA-136-270');
	assert.strictEqual(opts[10].l, 'NG-RAN');
	assert.strictEqual(opts[12].l, 'Satellite E-UTRAN');
});

test('eventDateTimeTlv builds the 8.39 date-time object', () => {
	const d = o => ({
		getFullYear: () => o.year, getMonth: () => o.month - 1, getDate: () => o.day,
		getHours: () => o.hour, getMinutes: () => o.min, getSeconds: () => o.sec,
		getTimezoneOffset: () => o.tzMin,
	});
	// UTC+3, 2026-09-30 21:55:00 -> swapped BCD + 12 quarters
	assert.strictEqual(
		eventDateTimeTlv(d({ year: 2026, month: 9, day: 30, hour: 21, min: 55, sec: 0, tzMin: -180 })),
		'260762900312550021');
	// UTC-2:30 -> 10 quarters with the sign bit (bit 3 of the first semi-octet)
	assert.strictEqual(
		eventDateTimeTlv(d({ year: 2026, month: 1, day: 5, hour: 8, min: 7, sec: 9, tzMin: 150 })),
		'260762105080709009');
});

test('the data connection event can carry the current date-time', () => {
	const build = EVENT_FORMS[0x1D].build;
	assert.match(build({ status: '0', type: '0', ti: '00', loc_status: '0', datetime: 'now' }),
		/^9D0100AA01001C01002607[0-9A-F]{14}9B0100$/);
	// left out by default
	assert.strictEqual(build({ status: '0', type: '0', ti: '00', loc_status: '0' }),
		'9D0100AA01001C01009B0100');
});

test('encBcdDigits and eventAddressTlv build the EFADN number coding', () => {
	assert.strictEqual(encBcdDigits('12345'), '2143F5');
	assert.strictEqual(encBcdDigits('79001234567'), '9700214365F7');
	// international number -> TON/NPI 0x91, length = 1 + 6 bytes
	assert.strictEqual(eventAddressTlv('79001234567', '1'), '8607919700214365F7');
	assert.strictEqual(eventAddressTlv('', '1'), '');
});

test('the call events build their 31.111 object sets', () => {
	// MT call: source network, TI + Address + Subaddress + IMS URI + Media Type
	assert.strictEqual(EVENT_FORMS[0x00].src, '83');
	assert.strictEqual(EVENT_FORMS[0x00].build({ ti: '85', number: '79001234567', ton: '1',
		sub: '12', ims_uri: 'sip:a@b', media: '1' }),
		'1C01858607919700214365F7880112B1077369703A614062FE0101');
	// call connected: source is the form's field, TI + optional Media Type
	assert.strictEqual(EVENT_FORMS[0x01].srcField, 'src');
	assert.strictEqual(EVENT_FORMS[0x01].build({ ti: '00', media: '0' }), '1C0100FE0100');
	assert.strictEqual(EVENT_FORMS[0x01].build({ ti: '00', media: '' }), '1C0100');
	// call disconnected: radio link timeout = zero-length cause, custom = bytes
	assert.strictEqual(EVENT_FORMS[0x02].build({ ti: '00', cause_mode: 'rlt', media: '' }), '1C01009A00');
	assert.strictEqual(EVENT_FORMS[0x02].build({ ti: '00', cause_mode: 'custom', cause: '10', media: '' }), '1C01009A0110');
	// a URI longer than the 100-byte limit is cut and tagged (8.135)
	const long = EVENT_FORMS[0x00].build({ ti: '00', ims_uri: 'x'.repeat(120) });
	assert.ok(long.includes('B164' + '78'.repeat(100)), 'the URI is cut to 100 bytes');
	assert.ok(long.endsWith('F300'), long.slice(-8));
	assert.ok(!EVENT_FORMS[0x00].build({ ti: '00', ims_uri: 'sip:a@b' }).includes('F300'));
});

test('the browser, WLAN and CSG events build their object sets', () => {
	assert.strictEqual(EVENT_FORMS[0x08].build({ cause: '1' }), 'B40101');
	assert.strictEqual(EVENT_FORMS[0x11].build({ status: '2' }), 'CB0102');
	// CSG: source network, access tech + status/mechanism + ID + name + PLMN
	assert.strictEqual(EVENT_FORMS[0x15].src, '83');
	assert.strictEqual(EVENT_FORMS[0x15].build({ status: '1', mech: 'manual', tech: '8',
		csg: '00000001', hnb: 'MyCSG', mcc: '250', mnc: '01' }),
		'BF0108D5020141D60400000001D7054D79435347890352F010');
});

test('every event type has a parameter form now', () => {
	assert.ok(!html.includes("note: 'not_yet'"), 'no event may stay unimplemented');
});

test('the data/display/search/frames events build their object sets', () => {
	// Data available: Channel status (B8) + Channel data length (B7)
	assert.strictEqual(EVENT_FORMS[0x09].build({ channel: '2', state: '128', info: '0', len: 'FF' }),
		'B8028200B701FF');
	// Display parameters: rows/cols + sizing/fonts/effects bytes
	assert.strictEqual(EVENT_FORMS[0x0C].build({ rows: '4', cols: '16', sizing: '1', fonts: '0', effects: '00' }),
		'C003841000');
	assert.strictEqual(EVENT_FORMS[0x0E].build({ mode: '1' }), 'E50101');
	assert.strictEqual(EVENT_FORMS[0x0F].build({ status: '12' }), 'E40112');
	assert.strictEqual(EVENT_FORMS[0x0F].build({ status: '' }), 'E40100');
	assert.strictEqual(EVENT_FORMS[0x10].build({ frame: '01', list: '0410' }), 'E703010410');
	// Card reader status (8.33) and Language (8.45) carry their object tags
	assert.strictEqual(EVENT_FORMS[0x06].build({ status: '1' }), 'A00101');
	assert.strictEqual(EVENT_FORMS[0x07].build({ lang: 'en' }), 'AD02656E');
});

test('the IMS, CAG and slices events build their object sets', () => {
	// IMS registration: source network, IMPU list (80-tagged URIs) + status code
	assert.strictEqual(EVENT_FORMS[0x17].src, '83');
	assert.strictEqual(EVENT_FORMS[0x17].build({ impu: 'sip:a@b', code: '200' }),
		'F70980077369703A614062F803323030');
	assert.strictEqual(EVENT_FORMS[0x18].src, '83');
	assert.strictEqual(EVENT_FORMS[0x18].build({ iari: 'a' }), 'F60161');
	// CAG cell selection: tech + status/mechanism + information list
	assert.strictEqual(EVENT_FORMS[0x1E].src, '83');
	assert.strictEqual(EVENT_FORMS[0x1E].build({ status: '1', mech: 'manual', tech: '8', list: '00000001' }),
		'BF0108D5020141D60400000001');
	// Slices status change: tech + status + served S-NSSAIs (count + 4 bytes each)
	assert.strictEqual(EVENT_FORMS[0x1F].src, '83');
	assert.strictEqual(EVENT_FORMS[0x1F].build({ status: '1', tech: '8', served: '01020304' }),
		'BF0108D50101D6050101020304');
	// partial NSSAI: the F9 object + the automatic Last Envelope marker
	assert.strictEqual(EVENT_FORMS[0x1F].build({ status: '1', partial: '0102' }), 'D50101F9020102F000');
	assert.ok(!EVENT_FORMS[0x1F].build({ status: '1', served: '01020304' }).includes('F000'));
});

test('the location-status event can carry the location information', () => {
	const build = EVENT_FORMS[0x03].build;
	// Normal service: the 8.19 object follows the status
	assert.strictEqual(build({ status: '0', mcc: '250', mnc: '01', lac: '00FF', cell: '0001' }),
		'9B0100930752F01000FF0001');
	// Limited / No service: no location information
	assert.strictEqual(build({ status: '1', mcc: '250' }), '9B0101');
	assert.strictEqual(build({ status: '0' }), '9B0100');
});

test('the channel-status event can carry the bearer and the local address', () => {
	const build = EVENT_FORMS[0x0A].build;
	assert.strictEqual(build({ channel: '1', state: '128', info: '0' }), 'B8028100');
	assert.strictEqual(build({ channel: '1', state: '128', info: '0', bearer: '5', addr_type: '21', addr: '192.168.1.2' }),
		'B8028100B50105BE0521C0A80102');
	assert.strictEqual(build({ channel: '1', state: '128', info: '0', addr_type: '00' }), 'B8028100BE00');
});

test('the disconnected/rejection/CAG/slices events carry their extra objects', () => {
	// IMS call disconnection cause: protocol + 2-byte cause (603 -> 025B)
	assert.strictEqual(EVENT_FORMS[0x02].build({ ti: '00', cause_mode: '', media: '', ims_proto: '1', ims_cause: '603' }),
		'1C0100D50301025B');
	// Extended information (CAG ID, 4 bytes)
	assert.ok(EVENT_FORMS[0x12].build({ reg_type: '9', access_tech: '8', cause: '2', ext_info_type: '1', ext_info: '00000001' })
		.endsWith('F2050100000001'));
	// CAG HRNN list (80-tagged names)
	assert.ok(EVENT_FORMS[0x1E].build({ status: '1', list: '', hrnn: 'A,B' }).endsWith('D706800141800142'));
	// allowed/served/rejected slice lists, with and without mapping
	assert.ok(EVENT_FORMS[0x1F].build({ status: '1', served: '', allowed_map: '01020304', allowed: '01020304',
		rejected_map: '01020304', rejected: '01020304' })
		.endsWith('F70401020304F80401020304D70401020304B10401020304'));
});

test('event fields introduced after the effective release are hidden', () => {
	// MT call: the IMS URI came with the Rel-12 URI support (12.4.0)
	let r = eventFieldsForRelease(EVENT_FORMS[0x00], 11);
	assert.deepStrictEqual(r.hidden.map(f => f.id), ['ims_uri']);
	assert.ok(r.fields.some(f => f.id === 'ti'));
	assert.strictEqual(eventFieldsForRelease(EVENT_FORMS[0x00], 12).hidden.length, 0);
	// CSG cell selection: the PLMN ID came in 12.1.0
	r = eventFieldsForRelease(EVENT_FORMS[0x15], 11);
	assert.deepStrictEqual(r.hidden.map(f => f.id).sort(), ['mcc', 'mnc']);
	// IMS registration: the IMPU list and the status code came in Rel-11
	r = eventFieldsForRelease(EVENT_FORMS[0x17], 10);
	assert.deepStrictEqual(r.hidden.map(f => f.id).sort(), ['code', 'impu']);
	// slices: the mapping/rejected objects came in Rel-18
	r = eventFieldsForRelease(EVENT_FORMS[0x1F], 16);
	assert.deepStrictEqual(r.hidden.map(f => f.id).sort(), ['allowed_map', 'partial', 'rejected', 'rejected_map']);
	assert.strictEqual(eventFieldsForRelease(EVENT_FORMS[0x1F], 18).hidden.length, 0);
	// a field without a known release is never hidden
	r = eventFieldsForRelease(EVENT_FORMS[0x12], 4);
	assert.strictEqual(r.hidden.length, 0);
	assert.strictEqual(r.fields.length, EVENT_FORMS[0x12].fields.length);
});

test('the single-envelope budget is enforced by the size helper', () => {
	// inner = 8 header bytes + the event data; D6 81 <len> + inner must stay
	// within the 1-byte APDU Lc, so the inner data is capped at 252 bytes
	assert.strictEqual(EVENT_INNER_MAX, 252);
	assert.strictEqual(eventEnvelopeSize(''), 8);
	assert.strictEqual(eventEnvelopeSize('AABB'), 10);
	assert.strictEqual(eventEnvelopeSize('AB'.repeat(244)), 252);
	assert.ok(eventFitsOneEnvelope('AB'.repeat(244)));
	assert.ok(!eventFitsOneEnvelope('AB'.repeat(245)));
	assert.ok(eventFitsOneEnvelope(null));
});

test('the event form previews the bytes the send would deliver', () => {
	// the preview builds from the visible fields - the same bytes the send
	// uses, so a raw-envelope test step can be authored by copying it
	const els = {
		'ev-f-status': { value: '0' },
		'ev-f-mcc': { value: '250' },
		'ev-f-mnc': { value: '01' },
		'ev-f-lac': { value: '00FF' },
		'ev-f-cell': { value: '0001' },
		'event-send-preview': { value: '' },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	globalThis._eventFormType = 0x03;
	pysimEventPreview();
	assert.strictEqual(els['event-send-preview'].value, '9B0100930752F01000FF0001');
	// a hidden field is left out of the build
	els['ev-f-mcc'].closest = () => ({ style: { display: 'none' } });
	pysimEventPreview();
	assert.strictEqual(els['event-send-preview'].value, '9B0100');
	// an event without a form leaves the preview empty
	globalThis._eventFormType = 0x99;
	pysimEventPreview();
	assert.strictEqual(els['event-send-preview'].value, '');
});

test('a name-keyword event points at the right form', () => {
	// the server's name keywords (used by the applet test scripts) resolve to
	// their code, so the editor shows the right event and prefills its fields
	// instead of falling back to event 03
	const cfg = EVENT_FORMS[eventCode('access_tech')];
	assert.ok(cfg, 'the access-tech form');
	assert.deepStrictEqual(cfg.fields.map(f => f.id), ['tech']);
	assert.strictEqual(cfg.build({ tech: 8 }), 'BF0108');
	const dc = EVENT_FORMS[eventCode('data_connection')];
	assert.ok(dc, 'the data-connection form');
	assert.ok(dc.fields.some(f => f.id === 'status') && dc.fields.some(f => f.id === 'type'));
});

test('the shared event vectors match the PWA builders', () => {
	// the same fixture is asserted against the Python builders by
	// tests/test_events.py - a change on either side fails the other suite
	// until the fixture and both implementations agree
	const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'event_vectors.json'), 'utf8'));
	assert.ok(vectors.events.length && vectors.scts.length, 'the fixture must not be empty');
	// the event name aliases (the server's keywords) resolve to the codes
	assert.ok(vectors.aliases, 'the fixture must carry the aliases');
	const wantAliases = {};
	for (const [name, hex] of Object.entries(vectors.aliases)) {
		wantAliases[name] = parseInt(hex, 16);
		assert.strictEqual(eventCode(name), parseInt(hex, 16), name);
	}
	assert.deepStrictEqual(EVENT_ALIASES, wantAliases);
	assert.deepStrictEqual(eventCode(0x0B), 0x0B);
	assert.deepStrictEqual(eventCode('0B'), 0x0B);
	assert.deepStrictEqual(eventCode('0x1d'), 0x1D);
	assert.ok(Number.isNaN(eventCode('nonsense')));
	assert.ok(Number.isNaN(eventCode('')));
	for (const v of vectors.events) {
		const build = EVENT_FORMS[parseInt(v.event, 16)].build;
		assert.strictEqual(build(v.fields).toUpperCase(), v.hex,
			'event 0x' + v.event + ' ' + JSON.stringify(v.fields));
	}
	for (const v of vectors.scts) {
		const d = {
			getFullYear: () => v.year, getMonth: () => v.month - 1,
			getDate: () => v.day, getHours: () => v.hour,
			getMinutes: () => v.min, getSeconds: () => v.sec,
			getTimezoneOffset: () => -v.tz_east_min,
		};
		assert.strictEqual(eventDateTimeTlv(d).toUpperCase(), '2607' + v.hex,
			'scts ' + JSON.stringify(v));
	}
});

test('every event form renders - numeric option values are stringified (v3.23.4)', () => {
	// esc() is string-only and most EVENT_FORMS option values are numbers
	// (e.g. {v:0,l:'Normal service'} and the ACCESSTECH list): rendering the
	// form with esc(o.v) crashed with 'str.replace is not a function' for the
	// Phone tab and the test-script step editor alike (v3.22.0 regression).
	globalThis.t = s => s;
	const bad = [];
	for (const [ev, cfg] of Object.entries(EVENT_FORMS)) {
		if (!cfg) continue;   // the unimplemented events carry a null form
		for (const withOnchange of [true, false]) {
			try {
				eventFormFieldsHtml(cfg, cfg.fields, {}, 'x-', withOnchange);
			} catch (e) {
				bad.push(ev + (withOnchange ? '/phone' : '/step') + ': ' + e.message);
			}
		}
	}
	assert.deepStrictEqual(bad, [], 'every event form must render');
	// a numeric option value lands as its string form and prefills selected
	const cfg = EVENT_FORMS[0x00];
	const sel = cfg.fields.find(f => f.type === 'select' && f.opts.some(o => typeof o.v === 'number'));
	assert.ok(sel, 'a numeric-option select must exist');
	const num = sel.opts.find(o => typeof o.v === 'number');
	const out = eventFormFieldsHtml(cfg, cfg.fields, {[sel.id]: num.v}, 'x-', true);
	assert.ok(out.includes('value="' + String(num.v) + '"'), out);
	assert.ok(out.includes('value="' + String(num.v) + '" selected'), out);
	delete globalThis.t;
});
