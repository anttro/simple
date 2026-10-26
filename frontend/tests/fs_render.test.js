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

let code = 'var pysimFsSort = "fid";\n';
code += 'var pysimFsFilter = new Set();\n';
code += extractFunc(html, 'pysimFsSortChildren') + '\n';
code += extractFunc(html, 'pysimFsNodePath') + '\n';
code += extractFunc(html, 'pysimFsFilterMatch') + '\n';
code += extractFunc(html, 'pysimFsRenderNode') + '\n';
code += 'globalThis.esc = s => s;\nglobalThis.t = s => s;\n';
eval(code);

const ef = (name, fid) => ({ name, fid, isDir: false, exists: true, children: null, expanded: false });
const df = (name, fid, extra) => Object.assign({ name, fid, isDir: true, exists: true, children: null, expanded: false }, extra || {});

test('absent directory renders a cross and no expand toggle', () => {
	const node = df('DF.WLAN', '5f40', { exists: false, children: [] });
	const out = pysimFsRenderNode(node, 0);
	assert.ok(out.includes('✗'), out);
	assert.ok(!out.includes('pysimFsToggleDir'), out);
	assert.ok(!out.includes('▶'), out);
});

test('expanded empty directory shows the (empty) placeholder', () => {
	const node = df('DF.EMPTY', '5f00', { children: [], expanded: true });
	const out = pysimFsRenderNode(node, 0);
	assert.ok(out.includes('(empty)'), out);
});

test('expanded directory with children renders no placeholder', () => {
	const node = df('DF.GSM', '7f20', { children: [ef('EF.IMSI', '6f07')], expanded: true });
	const out = pysimFsRenderNode(node, 0);
	assert.ok(out.includes('EF.IMSI'), out);
	assert.ok(!out.includes('(empty)'), out);
});

test('collapsed directory hides its children', () => {
	const node = df('DF.GSM', '7f20', { children: [ef('EF.IMSI', '6f07')], expanded: false });
	const out = pysimFsRenderNode(node, 0);
	assert.ok(!out.includes('EF.IMSI'), out);
});

test('non-existing directory hides previously loaded children', () => {
	const node = df('DF.WLAN', '5f40', { exists: false, expanded: true, children: [ef('EF.UPLMNWLAN', '4f42')] });
	const out = pysimFsRenderNode(node, 0);
	assert.ok(out.includes('✗'), out);
	assert.ok(!out.includes('EF.UPLMNWLAN'), out);
	assert.ok(!out.includes('(empty)'), out);
	assert.ok(!out.includes('pysimFsToggleDir'), out);
});

test('the row handlers carry the canonical path, so same-named files differ', () => {
	// EF.ECC lives in DF.GSM and ADF.USIM with the same name AND FID: the
	// onclick argument must be the path, never the ambiguous display name
	const root = { name: 'MF', fid: '3F00', isDir: true, exists: true, children: [], expanded: true, parent: null };
	const gsm = { name: 'DF.GSM', fid: '7F20', isDir: true, exists: true, children: [], expanded: true, parent: root };
	const usim = { name: 'ADF.USIM', fid: '7FFF', isDir: true, exists: true, children: [], expanded: true, parent: root };
	const gsmEcc = { name: 'EF.ECC', fid: '6FB7', isDir: false, exists: true, children: null, expanded: false, parent: gsm };
	const usimEcc = { name: 'EF.ECC', fid: '6FB7', isDir: false, exists: true, children: null, expanded: false, parent: usim };
	root.children = [gsm, usim];
	gsm.children = [gsmEcc];
	usim.children = [usimEcc];
	assert.ok(pysimFsRenderNode(gsmEcc, 0).includes("pysimFsClickFile('MF/7F20/6FB7')"));
	assert.ok(pysimFsRenderNode(usimEcc, 0).includes("pysimFsClickFile('ADF.USIM/6FB7')"));
	assert.ok(pysimFsRenderNode(gsm, 0).includes("pysimFsToggleDir('MF/7F20')"));
	assert.ok(pysimFsRenderNode(usim, 0).includes("pysimFsToggleDir('ADF.USIM')"));
});

test('pysimFsRenderNode passes pysimFsNodePath(node) to the click/toggle handlers', () => {
	const body = extractFunc(html, 'pysimFsRenderNode');
	assert.ok(body.includes('const nodePath = pysimFsNodePath(node);'));
	assert.match(body, /pysimFsToggleDir\(\\'' \+ esc\(nodePath\)/);
	assert.match(body, /pysimFsClickFile\(\\'' \+ esc\(nodePath\)/);
});
