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
eval(extractBlock('const REJECTION_CAUSES = [', 'const EVENT_FORMS = {').replace(/^const /gm, 'var '));
eval(extractBlock('const EVENT_FORMS = {', 'const PLI_QUALIFIERS = [').replace(/^const /gm, 'var '));

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

test('channel status event builds the B8 channel status TLV', () => {
	const build = EVENT_FORMS[0x0A].build;
	assert.strictEqual(EVENT_FORMS[0x0A].note, undefined);
	assert.strictEqual(build({ channel: '2', state: '128', info: '5' }), 'B8028205');
	assert.strictEqual(build({ channel: '1', state: '0', info: '0' }), 'B8020100');
	assert.strictEqual(build({ channel: '0', state: '64', info: '0' }), 'B8024000');
});
