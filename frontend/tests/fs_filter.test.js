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

// module state the extracted match function closes over
let pysimFsFilter = new Set();
eval(extractFunc(html, 'pysimFsFilterMatch'));

test('the file tree has a multiselect filter next to the sort pills', () => {
	assert.match(html, /id="pysim-fs-filter"[^>]*multiple[^>]*onchange="pysimFsSetFilter\(\)"/);
	assert.match(html, /data-l10n="Filter:"/);
});

test('the filter options list is scaffolded and empty for now', () => {
	assert.match(html, /const FS_FILTER_OPTIONS = \[/);
});

test('the render consults the filter when walking children', () => {
	assert.match(html, /pysimFsFilterMatch\(child\)/);
});

test('the filter state is persisted under simple_fs_filter', () => {
	const fn = extractFunc(html, 'pysimFsSetFilter');
	assert.ok(fn.indexOf("localStorage.setItem('simple_fs_filter'") > 0, fn);
	assert.ok(fn.indexOf('pysimFsRenderTree()') > 0, fn);
});

test('the dropdown is re-rendered on language switch', () => {
	const fn = extractFunc(html, 'refreshDynamicI18n');
	assert.ok(fn.indexOf('pysimFsFilterRender()') > 0, fn);
});

test('an empty filter matches everything (scaffold no-op until criteria land)', () => {
	pysimFsFilter = new Set();
	assert.strictEqual(pysimFsFilterMatch({ name: 'EF.ADN', fid: '6F3A' }), true);
	assert.strictEqual(pysimFsFilterMatch({ name: 'DF.GSM', fid: '7F20', isDir: true }), true);
});
