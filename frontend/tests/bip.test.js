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

globalThis.esc = s => s;
globalThis.t = s => s;
for (const fn of ['bipReplacedNotice', 'bipChannelLine', 'bipStatusLine', 'bipStartBody', 'bipModeChanged']) {
	eval(extractFunc(html, fn));
}

function fakeDom(mode) {
	const els = {};
	for (const id of ['bip-mode', 'bip-host', 'bip-port', 'bip-sink-note',
		'bip-passthru-note', 'bip-redirect-note']) {
		const el = {
			disabled: false, placeholder: '', toggled: {},
			classList: { toggle(c, on) { el.toggled[c] = on; } },
		};
		els[id] = el;
	}
	els['bip-mode'].value = mode;
	globalThis.document = { getElementById: id => els[id] || null };
	return els;
}

test('bipStartBody builds the control body per mode', () => {
	// passthru: no host/port at all - the card chooses the destination
	assert.deepStrictEqual(bipStartBody('passthru', '127.0.0.1', '9'),
		{ body: { action: 'start', mode: 'passthru' } });
	// sink: host optional, empty port = server-side ephemeral
	assert.deepStrictEqual(bipStartBody('sink', '127.0.0.1', ''),
		{ body: { action: 'start', mode: 'sink', host: '127.0.0.1' } });
	assert.deepStrictEqual(bipStartBody('sink', '', '9101'),
		{ body: { action: 'start', mode: 'sink', port: 9101 } });
	// fixed target: host and a real port required
	assert.deepStrictEqual(bipStartBody('redirect', '10.0.0.5', '8080'),
		{ body: { action: 'start', mode: 'redirect', host: '10.0.0.5', port: 8080 } });
	assert.strictEqual(bipStartBody('redirect', '', '8080').error, 'Fixed target requires the host');
	assert.strictEqual(bipStartBody('redirect', '10.0.0.5', '').error, 'Port must be 1-65535');
	assert.strictEqual(bipStartBody('redirect', '10.0.0.5', '0').error, 'Port must be 1-65535');
	assert.strictEqual(bipStartBody('redirect', '10.0.0.5', 'x').error, 'Port must be 1-65535');
	assert.strictEqual(bipStartBody('sink', '127.0.0.1', 'x').error, 'Port must be 0-65535');
	assert.strictEqual(bipStartBody('sink', '127.0.0.1', '70000').error, 'Port must be 0-65535');
});

test('bipReplacedNotice names what the start replaced', () => {
	assert.strictEqual(bipReplacedNotice(null), '');
	assert.strictEqual(bipReplacedNotice({}), '');
	assert.strictEqual(bipReplacedNotice({ owner: 'scp81', mode: 'tls' }),
		'Stopped the SCP81 listener (tls)');
	assert.strictEqual(bipReplacedNotice({ owner: 'bip', mode: 'sink' }),
		'Stopped the BIP session (sink)');
});

test('bipChannelLine summarizes one channel', () => {
	assert.strictEqual(bipChannelLine({ id: 1, requested: '10.0.0.5:80',
		target: '10.0.0.5:80', bytes_in: 412, bytes_out: 0, pending: 3,
		peer_closed: false }),
		'ch1 10.0.0.5:80 -> 10.0.0.5:80 in:412 out:0 buffer:3 open');
	assert.strictEqual(bipChannelLine({ id: 2, peer_closed: true }),
		'ch2 ? -> ? in:0 out:0 buffer:0 peer closed');
});

test('bipStatusLine shows the mode, bound address, connections, owner and channels', () => {
	assert.strictEqual(bipStatusLine({ owner: 'bip', bip: { channels: [] },
		listener: { mode: 'sink', host: '127.0.0.1', port: 53121, connections: 1 } }),
		'sink 127.0.0.1:53121 | 1 connections \u00b7 owner: bip');
	assert.strictEqual(bipStatusLine({ owner: 'bip', bip: { channels: [] },
		listener: { mode: 'passthru' } }), 'passthru \u00b7 owner: bip');
	assert.strictEqual(bipStatusLine(null), '');
	const withChannel = bipStatusLine({
		owner: 'bip',
		bip: { channels: [
			{ id: 1, requested: 'a:1', target: 'b:2', bytes_in: 5, bytes_out: 7, pending: 0 }] },
		listener: { mode: 'sink', host: 'h', port: 1 },
	});
	assert.ok(withChannel.includes('ch1 a:1 -> b:2 in:5 out:7 buffer:0 open'), withChannel);
});

test('bipModeChanged toggles the fields and the mode notes', () => {
	let els = fakeDom('sink');
	bipModeChanged();
	assert.strictEqual(els['bip-host'].disabled, false);
	assert.strictEqual(els['bip-port'].disabled, false);
	assert.strictEqual(els['bip-port'].placeholder, 'ephemeral');
	assert.strictEqual(els['bip-sink-note'].toggled.hidden, false);
	assert.strictEqual(els['bip-passthru-note'].toggled.hidden, true);
	assert.strictEqual(els['bip-redirect-note'].toggled.hidden, true);

	els = fakeDom('passthru');
	bipModeChanged();
	assert.strictEqual(els['bip-host'].disabled, true);
	assert.strictEqual(els['bip-port'].disabled, true);
	assert.strictEqual(els['bip-host'].toggled['opacity-40'], true);
	assert.strictEqual(els['bip-passthru-note'].toggled.hidden, false);
	assert.strictEqual(els['bip-sink-note'].toggled.hidden, true);

	els = fakeDom('redirect');
	bipModeChanged();
	assert.strictEqual(els['bip-host'].disabled, false);
	assert.strictEqual(els['bip-port'].placeholder, '8443');
	assert.strictEqual(els['bip-redirect-note'].toggled.hidden, false);
});

test('the BIP pill, view and API endpoints are wired', () => {
	assert.match(html, /data-phone-sub="bip"[^>]*data-l10n="BIP"/);
	assert.match(html, /id="phone-sub-bip"/);
	assert.match(html, /\['phone', 'tr', 'bip', 'esim', 'test'\]/);
	assert.match(html, /else if \(name === 'bip'\) \{\s*bipEnter\(\);/);
	assert.match(html, /bip: 'bip'/);
	// the poll stops when the Simulator tab is left
	assert.match(html, /if \(_bipTimer && name !== 'phone'\)/);
	for (const id of ['bip-mode', 'bip-host', 'bip-port', 'bip-start-btn',
		'bip-stop-btn', 'bip-state', 'bip-channels', 'bip-log']) {
		assert.ok(html.includes('id="' + id + '"'), id);
	}
	assert.match(html, /\/api\/bip\/control/);
	assert.match(html, /\/api\/bip\/status/);
	assert.match(html, /\/api\/bip\/log-clear/);
	assert.match(html, /\/api\/bip\/log'/);
	// the shared one-session rule and the owner marker are both shown
	assert.match(html, /One BIP session at a time: starting here replaces a running SCP81 listener/);
	assert.match(html, /if \(st\.owner === 'bip'\) s = t\('BIP session:'\)/);
	assert.match(html, /if \(e\.peer\) parts\.push\(e\.peer\);/);
	assert.match(html, /if \(isViewVisible\('phone-sub-bip'\)\) \{ bipStatusRefresh\(\); bipLogRefresh\(\); \}/);
});
