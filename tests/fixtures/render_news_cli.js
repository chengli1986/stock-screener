#!/usr/bin/env node
// render_news_cli.js — CLI driver for render_news.js.
//
// stock-screener/tests/test_news_page_wiring.py shells out to this via
// subprocess so the Python side can assert on genuine rendered DOM output
// and genuine click behavior, not just "does this string exist in the
// source file" (see render_news.js header for why that distinction matters
// here specifically).
//
// Usage:
//   node render_news_cli.js <jsonPath>
//     [--patch '<json object merged onto the parsed fixture>']
//     [--accord-items a,b,c]      (seeds .page-wrap children before render)
//     [--sect-nums s1,s2,...]     (seeds existing .sect-num badges)
//     [--click]                   (simulates a click on the accordion header)
//     [--click-layer <layer>]     (Task 7: simulates a click on that layer's
//                                  collapsible header, e.g. "procedural")
//     [--click-more-btn <n>]      (F1 三审: simulates a click on the n-th
//                                  (0-based, document order) .rn-more-btn,
//                                  dispatched through body's delegated
//                                  listener — same code path production uses)
//     [--src /path/to/report-news.js]  (mutation self-check only)
//
// Prints a single JSON object to stdout:
//   { childrenCount, children: [className,...], html, btnDataOpen, bodyOpen,
//     layers: { <layerName>: { className, open } },
//     moreBtnIdxs: [data-idx values of every .rn-more-btn, document order],
//     membersOpen: { <data-idx>: bool } }
// moreBtnIdxs/membersOpen are F1 三审 additions: moreBtnIdxs is what the
// cross-layer-uniqueness test asserts on (one entry per grouped item, by
// construction — a collision here is exactly the F1 bug); membersOpen is
// what the real-click test asserts on (only the clicked item's members
// should be open, everything else stays closed).
// childrenCount/children describe .page-wrap's direct children *after*
// rendering, in order — this is what proves M7 (insertion position) and the
// is_empty early-return (nothing appended).
//
// Task 7: report-news.js now builds each layer's <h3>/<button> + <ul> as real
// createElement nodes (appended directly to .accord-inner) instead of one big
// innerHTML string, specifically so procedural/sector's collapse toggle can
// be click-tested for real here — see the buildLayer() comment in
// report-news.js. `html` below is reconstructed by walking those real nodes
// (flatten()) rather than reading .innerHTML off .accord-inner directly,
// since .accord-inner itself is never assigned a single html string anymore.
// `layers` exposes each layer's <ul> collapse state directly via classList,
// which is what the Task 7 tests assert on instead of string-matching html.
const path = require('path');
const { run } = require(path.join(__dirname, 'render_news.js'));

function arg(flag) {
  var i = process.argv.indexOf(flag);
  return i === -1 ? null : process.argv[i + 1];
}

// Reconstructs an html-ish string by walking real child nodes.
//
// ★2026-08-11 二审 Finding 5：第一版这里只拼子节点/innerHTML，从不吐出节点
// 自己的标签——结果 procedural/sector 的 <h3>/<button> 和 <ul> 这两层容器
// 标签在 out.html 里彻底消失（只剩内容裸拼），层标题文案会跟下一层的 <li>
// 直接连在一起。当时的断言恰好没碰这两个标签所以全绿，但这会让以后任何
// 针对层容器标签的字符串断言静默失效。
//
// 区分规则：`div` 是本文件（intro 包装层、.accord-inner 本身）用来承载
// 字符串内容的纯组织性容器，不代表页面上真实存在的可见标签，flatten 时不
// 吐出它自己的 <div>——这与改动前「一整块 innerHTML 字符串」的输出形状一致
// （intro 段落从来没被包过 <div>）。非 div 的真实元素（buildLayer() 造的
// <h3>/<button>/<ul>）代表真实可见标签，flatten 时连标签本身一起吐出来，
// 跟 Task 7 之前「拼字符串」产出的 `<h3 class="rn-h">…</h3><ul class="rn-list">…</ul>`
// 逐字节一致。
function attrsString(el) {
  var parts = [];
  if (el.className) parts.push('class="' + el.className + '"');
  Object.keys(el._attrs || {}).forEach(function (k) {
    parts.push(k + '="' + el._attrs[k] + '"');
  });
  return parts.length ? ' ' + parts.join(' ') : '';
}

function flatten(el) {
  var body = (el.children && el.children.length)
    ? el.children.map(flatten).join('')
    : (el.innerHTML || '');
  if (el.tagName === 'div') return body;
  return '<' + el.tagName + attrsString(el) + '>' + body + '</' + el.tagName + '>';
}

function findAll(el, pred, out) {
  out = out || [];
  if (pred(el)) out.push(el);
  (el.children || []).forEach(function (c) { findAll(c, pred, out); });
  return out;
}

var jsonPath = process.argv[2];
var patchArg = arg('--patch');
var accordItemsArg = arg('--accord-items');
var sectNumsArg = arg('--sect-nums');
var srcArg = arg('--src');
var clickLayerArg = arg('--click-layer');
var clickMoreBtnArg = arg('--click-more-btn');
var doClick = process.argv.indexOf('--click') !== -1;

var opts = {};
if (patchArg) {
  var patch = JSON.parse(patchArg);
  opts.patch = function (data) { Object.assign(data, patch); };
}
if (accordItemsArg) opts.accordItems = accordItemsArg.split(',');
if (sectNumsArg) opts.sectNums = sectNumsArg.split(',');
if (srcArg) opts.srcPath = srcArg;

run(jsonPath, opts).then(function (host) {
  var out = { childrenCount: host.children.length, children: host.children.map(function (c) { return c.className; }) };

  var sec = host.children.filter(function (c) { return String(c.className || '').indexOf('rn-sec') !== -1; })[0];
  if (sec) {
    var btn = sec.children.filter(function (c) { return c.tagName === 'button'; })[0];
    var body = sec.children.filter(function (c) { return String(c.className || '').indexOf('accord-body') !== -1; })[0];
    var btnHtml = btn ? btn.innerHTML : '';
    if (doClick && btn) btn.simulateClick();

    var inner = body && body.children[0];
    if (clickLayerArg && inner) {
      var layerBtn = findAll(inner, function (e) {
        return e.tagName === 'button' && e.getAttribute && e.getAttribute('data-layer') === clickLayerArg;
      })[0];
      if (layerBtn) layerBtn.simulateClick();
    }

    out.html = inner ? flatten(inner) : '';
    out.btnHtml = btnHtml;
    out.btnDataOpen = btn ? btn.getAttribute('data-open') : null;
    out.bodyOpen = body ? body.classList.contains('open') : null;

    out.layers = {};
    if (inner) {
      findAll(inner, function (e) { return e.tagName === 'ul' && e.getAttribute && e.getAttribute('data-layer'); })
        .forEach(function (ul) {
          out.layers[ul.getAttribute('data-layer')] = {
            className: ul.className,
            open: ul.classList.contains('open'),
          };
        });
    }

    // F1 三审：跨层聚合项的 .rn-more-btn / .rn-members 现在是真实节点
    // （见 report-news.js 的 row() 注释），可以真的在文档树里找、真的点。
    var moreBtns = inner
      ? findAll(inner, function (e) { return e.tagName === 'button' && String(e.className || '').indexOf('rn-more-btn') !== -1; })
      : [];
    out.moreBtnIdxs = moreBtns.map(function (b) { return b.getAttribute('data-idx'); });

    if (clickMoreBtnArg !== null && body) {
      var n = parseInt(clickMoreBtnArg, 10);
      var target = moreBtns[n];
      // 走 body 的委托监听（跟 production 一样：body.addEventListener('click', ...)
      // 里靠 e.target.closest('.rn-more-btn') 找回被点的按钮），不是直接点按钮
      // 自己——按钮自己没有绑监听，监听绑在 body 上，委托这条路径才是真正要
      // 验证的代码。
      if (target) body.simulateClick(target);
    }

    // F1 三审：membersOpen 用**数组**而不是按 data-idx 做 key 的对象——如果
    // idx 撞车（正是 F1 那个 bug），按 idx 做 key 会让后面的条目静默覆盖
    // 前面的，反证时反而看不出撞车现场是哪个层被误开了。数组把每条
    // .rn-members 及其所属层都原样列出，撞车与否、开的是谁一目了然。
    function findLayerAncestor(el) {
      var node = el.parentNode;
      while (node) {
        if (node.tagName === 'ul' && node.getAttribute && node.getAttribute('data-layer')) {
          return node.getAttribute('data-layer');
        }
        node = node.parentNode;
      }
      return null;
    }
    out.membersOpen = inner
      ? findAll(inner, function (e) { return e.tagName === 'ul' && String(e.className || '').indexOf('rn-members') !== -1; })
        .map(function (ul) {
          return { idx: ul.getAttribute('data-idx'), layer: findLayerAncestor(ul), open: ul.classList.contains('open') };
        })
      : [];
  }
  process.stdout.write(JSON.stringify(out));
}).catch(function (e) {
  process.stdout.write(JSON.stringify({ error: String((e && e.stack) || e) }));
  process.exitCode = 1;
});
