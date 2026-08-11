// render_news.js — minimal hand-rolled DOM shim to *really execute*
// docs-site/js/report-news.js against real news JSON fixtures, without
// pulling in jsdom.
//
// Origin: 2026-08-11 review of Task 5 found the component's tests were all
// source-text assertions (`"announcements_error" in src`), which two mutation
// tests proved were blind — deleting the real behavior left every assertion
// green. This shim exists so the pytest side can assert on genuine rendered
// output and genuine click behavior instead.
//
// Scope: supports exactly the DOM surface report-news.js actually calls —
// createElement/appendChild/insertBefore/parentNode/nextSibling,
// querySelector(All) for '.kd-bar[data-snapshot]' / '.page-wrap' /
// '.accord-item' / '.sect-num', getAttribute/setAttribute, classList
// (contains/add/remove/toggle), addEventListener + a test-only
// simulateClick() helper.
//
// Deliberately NOT a full DOM: does not parse innerHTML strings into real
// child nodes (real browsers do this on assignment; this shim does not need
// to, because report-news.js only ever attaches listeners to elements it
// created itself via document.createElement — see the C1 fix comment at the
// top of report-news.js for why that matters).

const fs = require('fs');
const vm = require('vm');

function matchesClass(el, sel) {
  var cls = sel.replace(/^\./, '');
  return String(el.className || '').split(/\s+/).indexOf(cls) !== -1;
}

function makeEl(tag) {
  var el = {
    tagName: tag || 'div',
    innerHTML: '',
    textContent: '',
    children: [],
    parentNode: null,
    _attrs: {},
    _listeners: {},
    _classes: new Set(),
  };
  // ★2026-08-11 二审 Finding 4：`className` 原来是跟 `_classes`（classList
  // 读写的那个 Set）完全无关的独立字符串字段——审查者的变异测试证明了这
  // 有多危险：把某个真实节点的初始 `className` 硬编码成带 'open' 的字符串，
  // `classList.contains('open')` 依然读的是空 Set，测试全绿，看不出任何
  // 破绽。改成 accessor：读写 className 字符串本质上是在读写同一个
  // `_classes` Set，两者不可能再分叉。
  Object.defineProperty(el, 'className', {
    get: function () { return Array.from(el._classes).join(' '); },
    set: function (v) {
      el._classes = new Set(String(v == null ? '' : v).split(/\s+/).filter(Boolean));
    },
  });
  el.className = '';
  el.getAttribute = function (n) {
    return Object.prototype.hasOwnProperty.call(el._attrs, n) ? el._attrs[n] : null;
  };
  el.setAttribute = function (n, v) { el._attrs[n] = String(v); };
  el.appendChild = function (child) {
    child.parentNode = el;
    el.children.push(child);
    return child;
  };
  el.insertBefore = function (newNode, refNode) {
    newNode.parentNode = el;
    if (refNode == null) { el.children.push(newNode); return newNode; }
    var idx = el.children.indexOf(refNode);
    if (idx === -1) { el.children.push(newNode); } else { el.children.splice(idx, 0, newNode); }
    return newNode;
  };
  el.querySelectorAll = function (sel) {
    return el.children.filter(function (c) { return matchesClass(c, sel); });
  };
  el.querySelector = function (sel) {
    var found = el.querySelectorAll(sel);
    return found.length ? found[0] : null;
  };
  el.addEventListener = function (type, fn) {
    (el._listeners[type] = el._listeners[type] || []).push(fn);
  };
  el.simulateClick = function (target) {
    (el._listeners.click || []).forEach(function (fn) { fn({ target: target || el }); });
  };
  Object.defineProperty(el, 'nextSibling', {
    get: function () {
      if (!el.parentNode) return null;
      var idx = el.parentNode.children.indexOf(el);
      return idx === -1 ? null : (el.parentNode.children[idx + 1] || null);
    },
  });
  el.classList = {
    contains: function (c) { return el._classes.has(c); },
    add: function (c) { el._classes.add(c); },
    remove: function (c) { el._classes.delete(c); },
    toggle: function (c, force) {
      var on = force === undefined ? !el._classes.has(c) : !!force;
      if (on) el._classes.add(c); else el._classes.delete(c);
      return on;
    },
  };
  return el;
}

// jsonPath: path to a real `{code}-news.json` fixture.
// opts.patch(data): mutate the parsed JSON before it's "fetched".
// opts.accordItems: array of className strings to pre-seed as direct
//   children of .page-wrap (simulates existing report sections, and — with
//   a trailing 'disclaimer' entry — the innolight-300308 M7 regression).
// opts.sectNums: array of textContent strings to pre-seed as existing
//   .sect-num badges (drives the nextSectNum() continuation logic).
// opts.srcPath: override which report-news.js source file to execute —
//   used only for the manual mutation self-check, never by real tests.
function run(jsonPath, opts) {
  opts = opts || {};
  const data = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  if (opts.patch) opts.patch(data);

  const host = makeEl('div'); host.className = 'page-wrap';
  const bar = makeEl('div'); bar.className = 'kd-bar';
  bar._attrs['data-snapshot'] = 'X';

  (opts.accordItems || []).forEach(function (cls) {
    var item = makeEl('div');
    item.className = cls;
    host.appendChild(item);
  });

  const sectNums = (opts.sectNums || []).map(function (t) {
    var e = makeEl('span'); e.className = 'sect-num'; e.textContent = t; return e;
  });

  const doc = {
    querySelector: function (sel) {
      if (sel.indexOf('kd-bar') >= 0) return opts.noBar ? null : bar;
      if (sel.indexOf('accord-wrap') >= 0) return null; // 全仓不存在，见 M7
      if (sel.indexOf('page-wrap') >= 0) return host;
      return null;
    },
    querySelectorAll: function (sel) {
      if (sel.indexOf('sect-num') >= 0) return sectNums;
      return [];
    },
    createElement: function (tag) { return makeEl(tag); },
  };

  const ctx = {
    document: doc,
    Date: Date,
    console: console,
    fetch: function () {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve(data); } });
    },
  };
  vm.createContext(ctx);
  var srcPath = opts.srcPath || '/home/ubuntu/docs-site/js/report-news.js';
  vm.runInContext(fs.readFileSync(srcPath, 'utf8'), ctx, { filename: srcPath });

  // fetch/.then chain resolves on microtasks; two setImmediate hops is enough
  // slack for the promise chain used in report-news.js to fully settle.
  return new Promise(function (resolve) {
    setImmediate(function () { setImmediate(function () { resolve(host); }); });
  });
}

module.exports = { run: run, makeEl: makeEl };
