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
//     [--src /path/to/report-news.js]  (mutation self-check only)
//
// Prints a single JSON object to stdout:
//   { childrenCount, children: [className,...], html, btnDataOpen, bodyOpen,
//     layers: { <layerName>: { className, open } } }
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

// Reconstructs an html-ish string by walking real child nodes. A node with
// no real children (report-news.js only ever sets .innerHTML as a leaf
// string, never both appendChild *and* .innerHTML on the same node) just
// contributes its raw .innerHTML string; a node with real children recurses.
// This deliberately does not emit the container's own tag — it mirrors what
// direct `.innerHTML =` string-building used to produce pre-Task-7, so every
// pre-existing html-substring assertion keeps working unchanged.
function flatten(el) {
  if (!el.children || !el.children.length) return el.innerHTML || '';
  return el.children.map(flatten).join('');
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
  }
  process.stdout.write(JSON.stringify(out));
}).catch(function (e) {
  process.stdout.write(JSON.stringify({ error: String((e && e.stack) || e) }));
  process.exitCode = 1;
});
