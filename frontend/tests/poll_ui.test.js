const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extractFunc(src, name, asyncFn) {
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
	return (asyncFn ? 'async ' : '') + src.slice(m.index, i + 1);
}

eval(extractFunc(html, 'pysimUpdatePollUI'));
globalThis.t = s => s;

function setup() {
	const els = {
		'pli-pause-btn': { textContent: '', classList: { add: () => {}, remove: () => {} } },
		'pli-poll-stat': { textContent: '', className: 'text-xs text-gray-400' },
	};
	globalThis.document = { getElementById: id => els[id] || null };
	return els;
}

test('pysimUpdatePollUI shows the interval while polling is on', () => {
	const els = setup();
	pysimUpdatePollUI(true, 30);
	assert.strictEqual(els['pli-pause-btn'].textContent, 'ON');
	assert.strictEqual(els['pli-poll-stat'].textContent, '(30s)');
	assert.ok(!els['pli-poll-stat'].className.includes('amber'));
});

test('pysimUpdatePollUI shows the card-disabled state in amber', () => {
	const els = setup();
	pysimUpdatePollUI(true, 30, true);
	assert.ok(els['pli-poll-stat'].textContent.includes('POLLING OFF'), els['pli-poll-stat'].textContent);
	assert.ok(els['pli-poll-stat'].className.includes('text-amber-600'));
});

test('pysimUpdatePollUI shows a server warning', () => {
	const els = setup();
	pysimUpdatePollUI(true, 30, false, 'card disabled proactive polling');
	assert.strictEqual(els['pli-poll-stat'].textContent, 'card disabled proactive polling');
});

test('pysimUpdatePollUI clears the status line when off', () => {
	const els = setup();
	pysimUpdatePollUI(false, 30);
	assert.strictEqual(els['pli-pause-btn'].textContent, 'OFF');
	assert.strictEqual(els['pli-poll-stat'].textContent, '');
});
