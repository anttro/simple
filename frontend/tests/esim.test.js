const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name) {
	const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
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

let code = '';
for (const fn of ['esimLabel', 'esimGroupEid', 'esimFieldRows', 'esimResultText',
	'esimStateLabel', 'esimOperationsText', 'esimProfileRows', 'esimIconDataUrl',
	'esimChipSectionRows', 'esimChipValueWide', 'esimChipBox', 'esimSwitchStatus',
	'esimProfileTitle']) {
	code += extractFunc(html, fn) + '\n';
}
for (const c of ['ESIM_CHIP_LABELS', 'ESIM_RESULT_KEYS']) {
	code += html.match(new RegExp('const ' + c + ' = \\{[\\s\\S]*?\\n\\};'))[0]
		.replace('const ', 'var ') + '\n';
}
code += 'function t(s){return s;}\n';
code += 'function esc(s){return String(s);}\n';
eval(code);

test('esimGroupEid groups the hex digits in fours', () => {
	assert.strictEqual(esimGroupEid('89049032000000000000000000000001'),
		'8904 9032 0000 0000 0000 0000 0000 0001');
	assert.strictEqual(esimGroupEid(''), '');
});

test('esimLabel maps known chip keys and prettifies the rest', () => {
	assert.strictEqual(esimLabel('svn'), 'SVN');
	assert.strictEqual(esimLabel('default_dp_address'), 'Default SM-DP+ address');
	assert.strictEqual(esimLabel('installed_application'), 'Installed applications');
	assert.strictEqual(esimLabel('forbidden_profile_policy_rules'), 'Forbidden PPRs');
	assert.strictEqual(esimLabel('allowed_operators'), 'Allowed operators');
	assert.strictEqual(esimLabel('some_unknown_key'), 'Some unknown key');
});

test('esimFieldRows flattens nested values and skips empties', () => {
	const rows = esimFieldRows({
		svn: '1.2.3', empty: null, blank: '', nested: { profile_version: '2.2' },
		list: ['a', 'b'], zero: 0,
	});
	assert.deepStrictEqual(rows, [
		['SVN', '1.2.3'],
		['Nested / Profile version', '2.2'],
		['List', 'a, b'],
		['Zero', '0'],
	]);
});

test('esimFieldRows labels nested keys per path component', () => {
	const rows = esimFieldRows({
		euicc_ci_pki_list_for_verification: { subject_key_identifier: '8137AB' },
		ext_card_resource: {
			installed_application: 0,
			free_non_volatile_memory: 439084,
		},
	}, '');
	assert.deepStrictEqual(rows, [
		['CI PKI (verification) / Subject key identifier', '8137AB'],
		['Card resource / Installed applications', '0'],
		['Card resource / Free non-volatile memory', '439084'],
	]);
});

test('esimFieldRows recurses into arrays of objects with index labels', () => {
	const rows = esimFieldRows({
		rat: [{
			ppr_ids: ['ppr1', 'ppr2'],
			allowed_operators: [{ plmn: 'EEEEEE', gid1: null, gid2: null }],
			ppr_flags: ['consentRequired'],
		}],
	}, '');
	assert.deepStrictEqual(rows, [
		['Rules authorisation table 1 / PPR IDs', 'ppr1, ppr2'],
		['Rules authorisation table 1 / Allowed operators 1 / PLMN', 'EEEEEE'],
		['Rules authorisation table 1 / PPR flags', 'consentRequired'],
	]);
});

test('esimResultText localizes result codes and passes errors through', () => {
	assert.strictEqual(esimResultText({ result: 'catBusy' }), 'card is busy with a CAT session');
	assert.strictEqual(esimResultText({ result: 'ok' }), 'ok');
	assert.strictEqual(esimResultText({ error: 'no card' }), 'no card');
	assert.strictEqual(esimResultText(null), '');
});

test('esimSwitchStatus reports the switch, re-init, REFRESH and verification', () => {
	assert.strictEqual(esimSwitchStatus({ ok: true }), 'Profile switched');
	assert.strictEqual(
		esimSwitchStatus({ ok: true, reinitialized: true, refresh_seen: true }),
		'Profile switched — card re-initialized (REFRESH received)');
	assert.strictEqual(
		esimSwitchStatus({ ok: true, reinitialized: true, verified: false, state_after: 'disabled' }),
		'Profile switched — card re-initialized — not confirmed (Disabled)');
	assert.strictEqual(esimSwitchStatus({ ok: false, result: 'catBusy' }),
		'Profile switch failed: card is busy with a CAT session');
	assert.strictEqual(esimSwitchStatus({ ok: false, error: 'SW 6985' }),
		'Profile switch failed: SW 6985');
});

test('esimStateLabel and esimOperationsText map the profile metadata', () => {
	assert.strictEqual(esimStateLabel('enabled'), 'Enabled');
	assert.strictEqual(esimStateLabel('disabled'), 'Disabled');
	assert.strictEqual(esimStateLabel('other'), 'other');
	assert.strictEqual(esimOperationsText(['enable', 'delete']), 'enable, delete');
	assert.strictEqual(esimOperationsText([]), '');
});

test('esimProfileRows renders the metadata in a stable order', () => {
	const rows = esimProfileRows({
		iccid: '8970119000004002667', isdp_aid: 'A0000005591010FFFFFFFF8900000100',
		provider: 'Miranda', name: 'Miranda LTE', class: 'operational',
		owner: '250-99', icon_type: 'png', icon_size: 1234,
	});
	assert.deepStrictEqual(rows.map(r => r[0]),
		['ICCID', 'ISD-P AID', 'Provider', 'Profile name', 'Class', 'Owner', 'Icon']);
	assert.strictEqual(rows[0][1], '8970119000004002667');
	assert.strictEqual(rows[rows.length - 1][1], 'png · 1234 B');
});

test('esimProfileRows shows the bare icon type when the card sent no image', () => {
	assert.deepStrictEqual(esimProfileRows({ icon_type: 'jpg' }), [['Icon', 'jpg']]);
});

test('esimIconDataUrl builds a data URL for png and jpg icons', () => {
	assert.strictEqual(esimIconDataUrl({ icon_type: 'png', icon: '89504E47' }),
		'data:image/png;base64,iVBORw==');
	assert.strictEqual(esimIconDataUrl({ icon_type: 'jpg', icon: 'FFD8' }),
		'data:image/jpeg;base64,/9g=');
	assert.strictEqual(esimIconDataUrl({ icon_type: 'png' }), '');
	assert.strictEqual(esimIconDataUrl({ icon: '89504E47' }), '');
	assert.strictEqual(esimIconDataUrl(null), '');
});

test('esimProfileTitle puts the provider in front of the profile label', () => {
	assert.strictEqual(esimProfileTitle({ provider: 'Alfa', name: '0002' }), 'Alfa 0002');
	assert.strictEqual(esimProfileTitle({ provider: 'Alfa', nickname: 'Work', name: '0002' }),
		'Alfa Work');
	assert.strictEqual(esimProfileTitle({ name: '0002' }), '0002');
	assert.strictEqual(esimProfileTitle({ provider: 'Alfa' }), 'Alfa');
	assert.strictEqual(esimProfileTitle({ iccid: '8970119000004002667' }),
		'8970119000004002667');
	assert.strictEqual(esimProfileTitle({}), '?');
});

test('the profile card renders the icon image next to the rows', () => {
	const fn = extractFunc(html, 'esimRenderProfiles');
	assert.match(fn, /esimIconDataUrl\(p\)/);
	assert.match(fn, /<img src=/);
});

test('esimChipSectionRows builds rows for objects and RAT rule lists', () => {
	assert.deepStrictEqual(esimChipSectionRows({ svn: '2.2.2' }), [['SVN', '2.2.2']]);
	assert.deepStrictEqual(esimChipSectionRows(null), []);
	assert.deepStrictEqual(esimChipSectionRows([{ ppr_ids: ['ppr1'] }]),
		[['Rule 1 / PPR IDs', 'ppr1']]);
});

test('esimChipBox renders a titled box and skips empty sections', () => {
	assert.strictEqual(esimChipBox('EUICCInfo1', []), '');
	assert.strictEqual(esimChipBox('EUICCInfo1', null), '');
	const box = esimChipBox('EUICCInfo1', [['SVN', '2.2.2'], ['', 'raw-value']]);
	assert.match(box, /EUICCInfo1/);
	assert.match(box, /SVN/);
	assert.match(box, /2\.2\.2/);
	assert.match(box, /raw-value/);
});

test('the chip boxes prefer wrapping at spaces over breaking words', () => {
	const box = esimChipBox('EUICCInfo1', [['SVN', '2.2.2']]);
	assert.match(box, /break-words/);
	assert.doesNotMatch(box, /break-all/);
	const render = extractFunc(html, 'esimRenderProfiles');
	assert.doesNotMatch(render, /break-all/);
});

test('esimChipValueWide keeps short scalars in one column', () => {
	assert.strictEqual(esimChipValueWide('Profile version', '2.3.1'), false);
	assert.strictEqual(
		esimChipValueWide('Card resource / Free non-volatile memory', '439084'), false);
	assert.strictEqual(esimChipValueWide('SS accreditation number', 'ED-ZI-UP-0826'), false);
	assert.strictEqual(
		esimChipValueWide('RSP capability', 'additionalProfile, testProfileSupport'), true);
	assert.strictEqual(
		esimChipValueWide('CI PKI (verification) / Subject key identifier',
			'81370F5125D0B1D408D4C3B232E6D25E795BEBFB'), true);
});

test('esimChipBox lays short fields out in two columns', () => {
	const box = esimChipBox('EUICCInfo2', [
		['Profile version', '2.3.1'], ['SVN', '2.2.2'],
		['UICC capability', 'usimSupport, isimSupport'],
	], true);
	assert.match(box, /sm:grid-cols-2/);
	const cells = box.match(/<div class="flex gap-2[^"]*"/g);
	assert.deepStrictEqual(cells, [
		'<div class="flex gap-2 min-w-0"',
		'<div class="flex gap-2 min-w-0"',
		'<div class="flex gap-2 min-w-0 sm:col-span-2"',
	]);
	const one = esimChipBox('EUICCInfo1', [['SVN', '2.2.2']]);
	assert.doesNotMatch(one, /sm:grid-cols-2/);
	assert.match(one, /flex gap-2 mb-0\.5/);
});

test('the chip view renders EUICCInfo2 in two columns', () => {
	const fn = extractFunc(html, 'esimRenderChip');
	assert.match(fn, /esimChipBox\('EUICCInfo2', esimChipSectionRows\(c\.info2\), true\)/);
});

test('the chip view groups the sections into a grid', () => {
	const fn = extractFunc(html, 'esimRenderChip');
	assert.match(fn, /lg:grid-cols-3/);
	assert.match(fn, /lg:col-span-2/);
	assert.match(fn, /esimChipBox\('EUICCInfo2'/);
	assert.match(fn, /esimChipSectionRows/);
});

test('the eSIM pill is wired into the Phone simulator tab', () => {
	assert.ok(html.includes('data-phone-sub="esim"'));
	assert.ok(html.includes('id="phone-sub-esim"'));
	assert.ok(html.includes("'phone', 'tr', 'esim'"));
	assert.ok(html.includes('id="esim-refresh-btn"'));
	assert.ok(html.includes('id="esim-profiles"'));
	assert.ok(html.includes('id="esim-notifications"'));
});
