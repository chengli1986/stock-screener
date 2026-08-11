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
// querySelector(All) (descendant search, `.class` and `.class[attr="value"]`
// selectors), closest(sel), getAttribute/setAttribute, classList
// (contains/add/remove/toggle), addEventListener + a test-only
// simulateClick() helper.
//
// Deliberately NOT a full DOM: does not parse innerHTML strings into real
// child nodes (real browsers do this on assignment; this shim does not need
// to, because report-news.js only ever attaches listeners to elements it
// created itself via document.createElement — see the C1 fix comment at the
// top of report-news.js for why that matters).
//
// ★2026-08-11 三审 F1：row()/memberRow() 里带交互性的部分（.rn-more-btn /
// .rn-members）也改成了 createElement 现造真实节点（不再是 innerHTML 字符串
// 的一部分）——理由跟 buildLayer() 的 head/list 一样：body 级事件委托
// （e.target.closest('.rn-more-btn') + body.querySelector('.rn-members[data-idx=…]')）
// 要在这个 shim 里被真实点击测试跑到，前提是这些节点得是真实存在的对象，
// 不能只是字符串里的文本。为此这里补了两样东西：
//   - querySelector(All) 从「只查直接子节点、只认纯 class 名」升级成
//     「递归查所有后代、支持 `.class[attr="value"]` 属性选择器」
//   - closest(sel)：沿 parentNode 链向上找，同一套选择器匹配逻辑
// 上一轮已经因为 shim 的 classList/className 脱钩吃过一次亏——这次同理，
// shim 弱一分，上面所有点击行为断言就都弱一分。

const fs = require('fs');
const vm = require('vm');

// 支持 `.class` 或 `.class[attr="value"]` 两种形式；解析不出来就退回旧的
// 纯 class 名匹配（历史行为不变，比如 document 级的 '.kd-bar[data-snapshot]'
// 走的是 run() 里单独的 doc.querySelector 覆盖，不经过这个函数）。
function matchesSelector(el, sel) {
  var m = /^\.([\w-]+)(?:\[([\w-]+)="([^"]*)"\])?$/.exec(sel);
  if (!m) {
    var cls = sel.replace(/^\./, '');
    return String(el.className || '').split(/\s+/).indexOf(cls) !== -1;
  }
  var cls = m[1], attr = m[2], val = m[3];
  var hasClass = String(el.className || '').split(/\s+/).indexOf(cls) !== -1;
  if (!hasClass) return false;
  if (attr === undefined) return true;
  return !!el.getAttribute && el.getAttribute(attr) === val;
}

function queryAllDescendants(el, sel, out) {
  out = out || [];
  (el.children || []).forEach(function (c) {
    if (matchesSelector(c, sel)) out.push(c);
    queryAllDescendants(c, sel, out);
  });
  return out;
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
  // F1 三审：从「只查直接子节点」升级成递归查所有后代——production 的
  // `body.querySelector('.rn-members[data-idx="N"]')` 要找的节点现在挂在
  // body → inner → list → li 这条链的第 4 层，只查直接子节点永远找不到。
  el.querySelectorAll = function (sel) {
    return queryAllDescendants(el, sel);
  };
  el.querySelector = function (sel) {
    var found = el.querySelectorAll(sel);
    return found.length ? found[0] : null;
  };
  // F1 三审新增：真实浏览器里 `Element.closest()` 沿 parentNode 链向上找，
  // 自身也算——production 的委托监听 `e.target.closest('.rn-more-btn')`
  // 正是靠这个从「实际点到的节点」找回「真正绑了 data-idx 的按钮」。
  el.closest = function (sel) {
    var node = el;
    while (node) {
      if (matchesSelector(node, sel)) return node;
      node = node.parentNode;
    }
    return null;
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
