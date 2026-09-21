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
	'esimStateLabel', 'esimOperationsText', 'esimProfileRows', 'esimSwitchStatus']) {
	code += extractFunc(html, fn) + '\n';
}
for (const c of ['ESIM_CHIP_LABELS', 'ESIM_RESULT_KEYS']) {
	code += html.match(new RegExp('const ' + c + ' = \\{[\\s\\S]*?\\n\\};'))[0]
		.replace('const ', 'var ') + '\n';
}
code += 'function t(s){return s;}\n';
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
		owner: '250-99', icon_type: 'png',
	});
	assert.deepStrictEqual(rows.map(r => r[0]),
		['ICCID', 'ISD-P AID', 'Provider', 'Profile name', 'Class', 'Owner', 'Icon']);
	assert.strictEqual(rows[0][1], '8970119000004002667');
});

test('the eSIM pill is wired into the Phone simulator tab', () => {
	assert.ok(html.includes('data-phone-sub="esim"'));
	assert.ok(html.includes('id="phone-sub-esim"'));
	assert.ok(html.includes("'phone', 'tr', 'esim'"));
	assert.ok(html.includes('id="esim-refresh-btn"'));
	assert.ok(html.includes('id="esim-profiles"'));
	assert.ok(html.includes('id="esim-notifications"'));
});
