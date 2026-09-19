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

let code = 'var _pysimServerAvailable = null;\nvar _pysimCardEquipped = false;\nvar _pysimEquipping = false;\nvar _pysimCardIccid = null;\nvar _pysimHeaderIccid = undefined;\nvar _pysimHeaderScp80 = undefined;\nvar _pysimHeaderScp81 = undefined;\n';
code += extractFunc(html, 'pysimAvailabilityState') + '\n';
code += extractFunc(html, 'pysimControlDisabled') + '\n';
code += extractFunc(html, 'pysimNeedsHint') + '\n';
code += extractFunc(html, 'pysimApplyAvailability') + '\n';
code += extractFunc(html, 'pysimUpdateCardActionLabels') + '\n';
code += extractFunc(html, 'pysimUpdateStateIndicator') + '\n';
code += extractFunc(html, 'pysimUpdateIccidIndicator') + '\n';
code += extractFunc(html, 'pysimUpdatePresetIndicator') + '\n';
code += extractFunc(html, 'pysimUpdatePresetIndicators') + '\n';
code += extractFunc(html, 'cardsScp80Complete') + '\n';
code += extractFunc(html, 'cardsScp81Complete') + '\n';
code += extractFunc(html, 'cardsMatchedPreset') + '\n';
code += 'globalThis.t = s => s;\n';
eval(code);

function fakeEl() {
	const classes = new Set();
	return {
		classes,
		attrs: {},
		classList: {
			add: (...cs) => cs.forEach(c => classes.add(c)),
			remove: (...cs) => cs.forEach(c => classes.delete(c)),
			contains: c => classes.has(c),
		},
		setAttribute(k, v) { this.attrs[k] = v; },
		getAttribute(k) { return this.attrs[k]; },
		removeAttribute(k) { delete this.attrs[k]; },
	};
}

function setup() {
	const els = {
		'state-indicator': fakeEl(),
		'state-indicator-dot': fakeEl(),
		'state-indicator-img': fakeEl(),
		'state-indicator-iccid': fakeEl(),
		'state-indicator-scp80': fakeEl(),
		'state-indicator-scp81': fakeEl(),
	};
	els['state-indicator-img'].src = '';
	globalThis.document = { getElementById: id => els[id] || null, querySelectorAll: () => [] };
	_pysimServerAvailable = null;
	_pysimCardEquipped = false;
	_pysimEquipping = false;
	_pysimCardIccid = null;
	_pysimHeaderIccid = undefined;
	_pysimHeaderScp80 = undefined;
	_pysimHeaderScp81 = undefined;
	globalThis.cards = [];
	globalThis.cardsFindByIccid = () => -1;
	return els;
}

test('unprobed server shows a gray dot and a Connecting title', () => {
	const els = setup();
	pysimUpdateStateIndicator();
	const { 'state-indicator': wrap, 'state-indicator-dot': dot, 'state-indicator-img': img } = els;
	assert.ok(dot.classes.has('text-gray-400'));
	assert.ok(!dot.classes.has('hidden'));
	assert.ok(img.classes.has('hidden'));
	assert.strictEqual(wrap.attrs.title, 'Connecting...');
	assert.strictEqual(dot.attrs.title, 'Connecting...');
});

test('unreachable server shows a red dot', () => {
	const els = setup();
	_pysimServerAvailable = false;
	pysimUpdateStateIndicator();
	const { 'state-indicator': wrap, 'state-indicator-dot': dot, 'state-indicator-img': img } = els;
	assert.ok(dot.classes.has('text-red-500'));
	assert.ok(!dot.classes.has('text-gray-400'));
	assert.ok(img.classes.has('hidden'));
	assert.strictEqual(wrap.attrs.title, 'No server connection');
	assert.strictEqual(dot.attrs.title, 'No server connection');
});

test('server up without a card shows nosim.svg', () => {
	const els = setup();
	_pysimServerAvailable = true;
	pysimUpdateStateIndicator();
	const { 'state-indicator': wrap, 'state-indicator-dot': dot, 'state-indicator-img': img } = els;
	assert.ok(dot.classes.has('hidden'));
	assert.ok(!img.classes.has('hidden'));
	assert.strictEqual(img.src, 'nosim.svg');
	assert.strictEqual(wrap.attrs.title, 'Server connected, no card equipped');
	assert.strictEqual(dot.attrs.title, undefined);
});

test('equipped card shows sim.svg', () => {
	const els = setup();
	_pysimServerAvailable = true;
	_pysimCardEquipped = true;
	pysimUpdateStateIndicator();
	const { 'state-indicator': wrap, 'state-indicator-img': img } = els;
	assert.strictEqual(img.src, 'sim.svg');
	assert.strictEqual(wrap.attrs.title, 'Card equipped');
});

test('equipping shows the animated sim_anim.svg', () => {
	const els = setup();
	_pysimServerAvailable = true;
	_pysimEquipping = true;
	pysimUpdateStateIndicator();
	const { 'state-indicator': wrap, 'state-indicator-img': img } = els;
	assert.strictEqual(img.src, 'sim_anim.svg');
	assert.strictEqual(wrap.attrs.title, 'Card inserted — initializing...');
});

test('dot color transitions do not accumulate', () => {
	const els = setup();
	_pysimServerAvailable = false;
	pysimUpdateStateIndicator();
	_pysimServerAvailable = null;
	pysimUpdateStateIndicator();
	const dot = els['state-indicator-dot'];
	assert.ok(dot.classes.has('text-gray-400'));
	assert.ok(!dot.classes.has('text-red-500'));
});

test('indicator markup carries the dot and image elements', () => {
	assert.match(html, /id="state-indicator-dot"/);
	assert.match(html, /id="state-indicator-img"[^>]*src="nosim\.svg"/);
});

test('header prints the equipped card ICCID next to the card image', () => {
	const els = setup();
	pysimUpdateIccidIndicator('89701450001700031958');
	const el = els['state-indicator-iccid'];
	assert.strictEqual(el.textContent, '89701450001700031958');
	assert.ok(!el.classes.has('hidden'));
	// no card session -> hidden again
	pysimUpdateIccidIndicator(null);
	assert.strictEqual(el.textContent, '');
	assert.ok(el.classes.has('hidden'));
	// markup order: image, ICCID, ADM badge, SCP80, SCP81
	assert.ok(html.indexOf('id="state-indicator-img"') < html.indexOf('id="state-indicator-iccid"'));
	assert.ok(html.indexOf('id="state-indicator-iccid"') < html.indexOf('id="state-indicator-adm"'));
	assert.ok(html.indexOf('id="state-indicator-adm"') < html.indexOf('id="state-indicator-scp80"'));
	assert.ok(html.indexOf('id="state-indicator-scp80"') < html.indexOf('id="state-indicator-scp81"'));
});

test('SCP80/SCP81 markers follow the matching preset completeness', () => {
	const els = setup();
	globalThis.cards = [{ name: 'C', kic: '15', kid: '15', spi1: '16', spi2: '01',
		cntr: '0000000001', kicKey: 'AA', kidKey: 'BB', pskIdentity: 'id', pskKey: 'KEY' }];
	globalThis.cardsFindByIccid = () => 0;
	_pysimCardIccid = '89701450001700031958';
	pysimUpdatePresetIndicators();
	const e80 = els['state-indicator-scp80'];
	const e81 = els['state-indicator-scp81'];
	assert.strictEqual(e80.textContent, 'SCP80');
	assert.strictEqual(e81.textContent, 'SCP81');
	assert.ok(!e80.classes.has('hidden'));
	assert.ok(!e81.classes.has('hidden'));
	assert.strictEqual(e80.attrs.title, 'SCP80 preset complete');
	assert.strictEqual(e81.attrs.title, 'SCP81 preset complete');
	// SCP80 incomplete (missing key) -> only the SCP81 marker stays
	globalThis.cards[0].kicKey = '';
	pysimUpdatePresetIndicators();
	assert.ok(e80.classes.has('hidden'));
	assert.strictEqual(e80.textContent, '');
	assert.strictEqual(e80.attrs.title, undefined);
	assert.ok(!e81.classes.has('hidden'));
	// no matching preset -> both hidden
	globalThis.cardsFindByIccid = () => -1;
	pysimUpdatePresetIndicators();
	assert.ok(e81.classes.has('hidden'));
	// explicit null ICCID (no card / server lost) hides them
	globalThis.cardsFindByIccid = () => 0;
	pysimUpdatePresetIndicators(null);
	assert.ok(e80.classes.has('hidden'));
	assert.ok(e81.classes.has('hidden'));
});

test('card-iccid controls need an equipped card with a readable ICCID', () => {
	const check = (state, iccid) => {
		const el = fakeEl();
		el.setAttribute('data-needs', 'card-iccid');
		globalThis.document = { querySelectorAll: () => [el], getElementById: () => null };
		_pysimServerAvailable = state !== 'server-down';
		_pysimCardEquipped = state === 'card';
		_pysimCardIccid = iccid;
		pysimApplyAvailability();
		return el;
	};
	// no card -> disabled
	let el = check('no-card', null);
	assert.strictEqual(el.disabled, true);
	assert.strictEqual(el.attrs.title, 'Insert and equip a card');
	// equipped but unreadable ICCID -> disabled with its own hint
	el = check('card', null);
	assert.strictEqual(el.disabled, true);
	assert.strictEqual(el.attrs.title, 'Card equipped but its ICCID is not readable');
	// equipped with an ICCID -> enabled
	el = check('card', '8970119000004600098');
	assert.strictEqual(el.disabled, false);
	assert.strictEqual(el.attrs.title, undefined);
	// server down -> disabled
	el = check('server-down', '8970119000004600098');
	assert.strictEqual(el.disabled, true);
});

test('indicator image stays within the 32px header row budget', () => {
	const m = /id="state-indicator-img"[^>]*style="width:(\d+)px;height:(\d+)px"/.exec(html);
	assert.ok(m, 'inline image size not found');
	assert.strictEqual(m[1], m[2]);
	const size = Number(m[1]);
	assert.ok(size >= 24 && size <= 32, 'size ' + size + 'px would change the header height');
});

test('header badges are bordered chips with even spacing', () => {
	for (const id of ['state-indicator-adm', 'state-indicator-scp80', 'state-indicator-scp81']) {
		const m = new RegExp('id="' + id + '"[^>]*class="([^"]*)"').exec(html);
		assert.ok(m, id + ' markup not found');
		assert.match(m[1], /\bborder\b/);
		assert.match(m[1], /\brounded\b/);
	}
	assert.match(html, /id="state-indicator-scp80"[^>]*class="[^"]*ml-1/);
	assert.match(html, /id="state-indicator-scp81"[^>]*class="[^"]*ml-1/);
});

test('card action labels carry the equipped ICCID', () => {
	setup();
	const plain = fakeEl();
	const colon = fakeEl();
	colon.attrs['data-iccid-sep'] = ': ';
	globalThis.document.querySelectorAll = sel => sel === '.profiler-iccid-label' ? [plain, colon] : [];
	_pysimCardIccid = '89701450001700031958';
	pysimUpdateCardActionLabels();
	assert.strictEqual(plain.textContent, ' 89701450001700031958');
	assert.strictEqual(colon.textContent, ': 89701450001700031958');
	_pysimCardIccid = null;
	pysimUpdateCardActionLabels();
	assert.strictEqual(plain.textContent, '');
	assert.strictEqual(colon.textContent, '');
});
