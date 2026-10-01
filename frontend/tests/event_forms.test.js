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
eval(extractBlock('const EVENT_NAMES = {', 'const REJECTION_CAUSES = [').replace(/^const /gm, 'var '));
eval(extractBlock('const REJECTION_CAUSES = [', 'const EVENT_FORMS = {').replace(/^const /gm, 'var '));
eval(extractBlock('const EVENT_FORMS = {', 'const PLI_QUALIFIERS = [').replace(/^const /gm, 'var '));
eval(extractFunc(html, 'encPlmn'));
eval(extractFunc(html, 'bytesToHex'));

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
