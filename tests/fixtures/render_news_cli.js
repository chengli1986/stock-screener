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
//     [--src /path/to/report-news.js]  (mutation self-check only)
//
// Prints a single JSON object to stdout:
//   { childrenCount, children: [className,...], html, btnDataOpen, bodyOpen }
// childrenCount/children describe .page-wrap's direct children *after*
// rendering, in order — this is what proves M7 (insertion position) and the
// is_empty early-return (nothing appended).
const path = require('path');
const { run } = require(path.join(__dirname, 'render_news.js'));

function arg(flag) {
  var i = process.argv.indexOf(flag);
  return i === -1 ? null : process.argv[i + 1];
}

var jsonPath = process.argv[2];
var patchArg = arg('--patch');
var accordItemsArg = arg('--accord-items');
var sectNumsArg = arg('--sect-nums');
var srcArg = arg('--src');
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
    out.html = body && body.children[0] ? body.children[0].innerHTML : '';
    out.btnHtml = btnHtml;
    out.btnDataOpen = btn ? btn.getAttribute('data-open') : null;
    out.bodyOpen = body ? body.classList.contains('open') : null;
  }
  process.stdout.write(JSON.stringify(out));
}).catch(function (e) {
  process.stdout.write(JSON.stringify({ error: String((e && e.stack) || e) }));
  process.exitCode = 1;
});
