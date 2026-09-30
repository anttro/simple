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

// Response parser maps + lookupSw live between SW_MAP and updateRespCmd.
// Rewrite top-level const -> var so the maps leak out of sloppy-mode eval.
eval(extractBlock('const SW_MAP = {', 'function updateRespCmd').replace(/^const /gm, 'var '));

test('lookupSw wildcard 91XX resolves proactive pending (any length)', () => {
	const desc = lookupSw('91', '14', 'uicc');
	assert.ok(desc.includes('proactive'), desc);
	assert.ok(!desc.includes('no response data'));
});

test('lookupSw 63CX extracts retry counter', () => {
	const desc = lookupSw('63', 'C3', 'uicc');
	assert.ok(desc.includes('3'), desc);
});

test('SW_MAP.generic covers ISO 6A85/6A89/6A8A', () => {
	assert.ok(SW_MAP.generic['6A85']);
	assert.ok(SW_MAP.generic['6A89']);
	assert.ok(SW_MAP.generic['6A8A']);
});

test('gp 6310 label does not reference removed P2=42 option', () => {
	assert.ok(!SW_MAP.gp['6310'].includes('42'));
});

test('LIFECYCLE_MAP per GPC v2.3 life cycles', () => {
	// GP Card Spec v2.3.1 11.1.1 Tables 11-3..11-6: the coding depends on the
	// object kind (the ISD inherits the card life cycle), so the shared map
	// names every interpretation.  The Explore decodes per kind instead
	// (ramFmtLifecycle).
	assert.strictEqual(LIFECYCLE_MAP['01'], 'OP_READY (card/ISD) / LOADED (ELF)');
	assert.strictEqual(LIFECYCLE_MAP['03'], 'INSTALLED (app/SD)');
	assert.strictEqual(LIFECYCLE_MAP['07'], 'SELECTABLE (app/SD) / INITIALIZED (card/ISD)');
	assert.strictEqual(LIFECYCLE_MAP['0F'], 'SECURED (card/ISD) / PERSONALIZED (SD)');
	assert.strictEqual(LIFECYCLE_MAP['1F'], 'App-specific (app)');
	assert.strictEqual(LIFECYCLE_MAP['7F'], 'CARD_LOCKED');
	assert.strictEqual(LIFECYCLE_MAP['FF'], 'TERMINATED');
	assert.strictEqual(LIFECYCLE_MAP['83'], 'LOCKED');
	assert.strictEqual(LIFECYCLE_MAP['8F'], 'LOCKED');
});

test('TS 51.011 SIM families resolve (94xx/98xx/92xx/9Exx/9Fxx)', () => {
	assert.ok(lookupSw('94', '04', 'uicc').includes('not found'));
	assert.ok(lookupSw('94', '00', 'uicc').includes('No EF selected'));
	assert.ok(lookupSw('94', '08', 'uicc').includes('inconsistent with the command'));
	assert.ok(lookupSw('98', '40', 'uicc').includes('blocked'));
	assert.ok(lookupSw('92', '40', 'uicc').includes('Memory problem'));
	assert.ok(lookupSw('98', '50', 'gp').includes('max value'));
	assert.ok(lookupSw('9E', '15', 'uicc').includes('download'));
	assert.ok(lookupSw('9F', '20', 'uicc').includes('GET RESPONSE'));
});
