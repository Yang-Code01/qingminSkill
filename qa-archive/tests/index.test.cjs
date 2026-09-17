const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = readFileSync(path.join(__dirname, '..', 'index-template.html'), 'utf8');

function load(entries = []) {
  const ids = [...template.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  const elements = Object.fromEntries(ids.map(id => [id, {
    value: '', innerHTML: '', textContent: '', children: [], listeners: {}, hidden: false,
    appendChild(child) { this.children.push(child); },
    addEventListener(event, listener) { this.listeners[event] = listener; },
    focus() { this.focused = true; }
  }]));
  const context = vm.createContext({
    document: {
      getElementById(id) { return elements[id]; },
      createElement() { return {}; }
    }
  });
  const source = template.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace('{{ENTRIES_JSON}}', () => JSON.stringify(entries).replace(/</g, '\\u003c'));
  vm.runInContext(source, context);
  return { elements, context };
}

test('highlighting never searches generated markup or entities', () => {
  const { context } = load();
  assert.equal(vm.runInContext("snippet('foo mark & <b>', ['foo', 'mark', 'amp', '<b>'], 120)", context),
    '<mark>foo</mark> <mark>mark</mark> &amp; <mark>&lt;b&gt;</mark>');
});

test('overlapping and duplicate matches merge without nested markup', () => {
  const { context } = load();
  assert.equal(vm.runInContext("snippet('rebase', ['rebase', 'base', 'rebase'], 120)", context),
    '<mark>rebase</mark>');
  assert.equal(vm.runInContext("snippet('a+b [x]', ['a+b', '[x]'], 120)", context),
    '<mark>a+b</mark> <mark>[x]</mark>');
});

test('empty searches still escape body text', () => {
  const { context } = load();
  assert.equal(vm.runInContext("snippet('<script>&', [], 120)", context), '&lt;script&gt;&amp;');
  assert.equal(vm.runInContext("snippet('', ['x'], 120)", context), '');
});

test('sorts by update date and searches category plus multiple terms', () => {
  const entries = [
    { title: 'Old', category: 'Work', date: '2026-01-01', updated_at: '2026-09-15',
      keywords: ['git'], path: 'old.html', text: 'rebase details' },
    { title: 'New', category: 'Other', date: '2026-09-01',
      keywords: ['git'], path: 'new.html', text: 'rebase' }
  ];
  const { context, elements } = load(entries);
  assert.ok(elements.results.innerHTML.indexOf('old.html') < elements.results.innerHTML.indexOf('new.html'));
  elements.q.value = 'work git';
  vm.runInContext('render()', context);
  assert.ok(elements.results.innerHTML.includes('old.html'));
  assert.ok(!elements.results.innerHTML.includes('new.html'));
  elements.cat.value = 'Other';
  vm.runInContext('render()', context);
  assert.ok(elements.results.innerHTML.includes('class="empty"'));
});

test('cards escape user text and preserve encoded local URLs', () => {
  const { elements } = load([
    { title: '<b>Title</b>', category: 'A&B', date: '2026-09-15',
      keywords: ['<script>'], path: 'A%26B/file.html', text: '</script>' }
  ]);
  assert.ok(elements.results.innerHTML.includes('&lt;b&gt;Title&lt;/b&gt;'));
  assert.ok(elements.results.innerHTML.includes('href="A%26B/file.html"'));
  assert.ok(!elements.results.innerHTML.includes('<script>'));
});

test('overview counts and reset control follow the active filters', () => {
  const { context, elements } = load([
    { title: 'Git', category: 'Work', date: '2026-09-15', keywords: ['git'], path: 'git.html', text: 'rebase' },
    { title: 'SQL', category: 'Work', date: '2026-09-14', keywords: ['sql'], path: 'sql.html', text: 'query' }
  ]);
  assert.equal(elements['entry-count'].textContent, '2');
  assert.equal(elements['category-count'].textContent, '1');
  assert.equal(elements.reset.hidden, true);
  elements.q.value = 'git';
  elements.cat.value = 'Work';
  vm.runInContext('render()', context);
  assert.equal(elements.reset.hidden, false);
  assert.ok(!elements.results.innerHTML.includes('sql.html'));
  elements.reset.listeners.click();
  assert.equal(elements.q.value, '');
  assert.equal(elements.cat.value, '');
  assert.equal(elements.reset.hidden, true);
  assert.equal(elements.q.focused, true);
  assert.ok(elements.results.innerHTML.includes('sql.html'));
});

test('empty library and unmatched search have distinct guidance', () => {
  const empty = load().elements.results.innerHTML;
  const { context, elements } = load([
    { title: 'Git', category: 'Work', date: '2026-09-15', keywords: [], path: 'git.html', text: '' }
  ]);
  elements.q.value = 'nothing';
  vm.runInContext('render()', context);
  assert.notEqual(elements.results.innerHTML, empty);
  assert.ok(empty.includes('class="empty"'));
  assert.ok(elements.results.innerHTML.includes('class="empty"'));
});
