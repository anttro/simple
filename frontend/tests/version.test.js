// The release version moves together: the PWA header, the server and the
// project metadata (AGENTS.md).  The Python suite pins the same three values
// (tests/test_version.py).
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const pyproject = fs.readFileSync(path.join(__dirname, '..', '..', 'pyproject.toml'), 'utf8');
const server = fs.readFileSync(path.join(__dirname, '..', '..', 'simple_server', 'server.py'), 'utf8');

function pwaVersion() {
	const m = /const SIMPLE_VERSION = '([^']+)';/.exec(html);
	assert.ok(m, 'SIMPLE_VERSION not found in index.html');
	return m[1];
}

test('the PWA, pyproject and server versions agree', () => {
	const project = /^version = "([^"]+)"/m.exec(pyproject);
	assert.ok(project, 'pyproject.toml has no [project] version');
	const srv = /^VERSION = '([^']+)'$/m.exec(server);
	assert.ok(srv, 'simple_server/server.py has no VERSION');
	assert.strictEqual(pwaVersion(), project[1]);
	assert.strictEqual(pwaVersion(), srv[1]);
});

test('the release is the pysim-free major', () => {
	assert.strictEqual(pwaVersion().split('.')[0], '4');
});

test('the service worker cache is the versioned form', () => {
	const sw = fs.readFileSync(path.join(__dirname, '..', 'sw.js'), 'utf8');
	const m = /const CACHE = 'simple-v(\d+)';/.exec(sw);
	assert.ok(m, 'sw.js CACHE is not simple-v<N>');
	assert.ok(Number(m[1]) >= 499, 'the index.html change needs a CACHE bump');
});
