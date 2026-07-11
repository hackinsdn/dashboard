/*
 * Regression tests for parseLabGuide() in
 * apps/templates/pages/labs_edit.html
 *
 * parseLabGuide() previews the lab guide Markdown and rejects the form when two
 * questions share the same `name` attribute. radio/checkbox inputs are allowed
 * to repeat a name (they form a single option group); every other repeat is a
 * duplicate question. Separately, every input `id` must be unique (a repeated id
 * breaks the <label for="..."> associations of radio/checkbox options).
 *
 * These tests extract the real function from the template (rather than copying
 * its body) and exercise it with the project's actual jQuery and marked builds,
 * so a regression in the shipped template is caught.
 *
 * Run with:  cd tests/js && npm install && npm test
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const TEMPLATE = path.join(REPO_ROOT, 'apps', 'templates', 'pages', 'labs_edit.html');
const JQUERY = path.join(REPO_ROOT, 'apps', 'static', 'assets', 'plugins', 'jquery', 'jquery.min.js');
const MARKED = path.join(REPO_ROOT, 'apps', 'static', 'assets', 'plugins', 'marked', 'marked.min.js');

// --- Extract the real parseLabGuide() source from the template -------------
function extractParseLabGuide(src) {
  const start = src.indexOf('function parseLabGuide');
  if (start === -1) throw new Error('parseLabGuide not found in template');
  let depth = 0;
  let seenBrace = false;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') { depth++; seenBrace = true; }
    else if (ch === '}') { depth--; }
    if (seenBrace && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error('Could not brace-match parseLabGuide body');
}

// --- Build a jsdom window with jQuery + marked and the real function -------
function makeRunner() {
  const jquerySrc = fs.readFileSync(JQUERY, 'utf8');
  const markedSrc = fs.readFileSync(MARKED, 'utf8');
  const fnSrc = extractParseLabGuide(fs.readFileSync(TEMPLATE, 'utf8'));

  const dom = new JSDOM(
    `<!DOCTYPE html><body><div id="custom-tabs-labguide-preview"></div>` +
    `<script>${jquerySrc}</script><script>${markedSrc}</script></body>`,
    { runScripts: 'dangerously' }
  );
  const { window } = dom;

  let lastAlert = null;
  window.alert = (msg) => { lastAlert = msg; };
  window.questionCounter = 0;
  window.editorLabGuide = { getValue: () => window.__md };
  window.eval(fnSrc + '\nwindow.__parseLabGuide = parseLabGuide;');

  return function run(md) {
    lastAlert = null;
    window.__md = md;
    const event = { preventDefault() { this.prevented = true; } };
    const ret = window.__parseLabGuide(event, true);
    return { ret, alert: lastAlert, prevented: !!event.prevented };
  };
}

// --- Markdown snippets mirroring what the editor buttons insert ------------
const SELECT = (n) => `<select name='${n}'>\n  <option value=''>--</option>\n  <option>a</option>\n</select>\n`;
const SELECT_MULTI = (n) => `<select multiple name='${n}'><option>a</option></select>\n`;
const TEXT = (n) => `<input type='text' name='${n}' placeholder='x'>\n`;
const TEXTAREA = (n) => `<textarea name='${n}' rows='6' cols='80'> </textarea>\n`;
const RADIO = (n) => `<input type='radio' name='${n}' value='1'/>\n`;
const CHECKBOX = (n) => `<input type='checkbox' name='${n}' value='1'/>\n`;
// radio/checkbox option with an explicit id + matching label, like the
// "Add checkbox question" button inserts (name repeats, ids must be unique).
const RADIO_ID = (n, id) => `<input type='radio' name='${n}' id='${id}' value='1'/> <label for='${id}'>x</label><br>\n`;
const CHECK_ID = (n, id) => `<input type='checkbox' name='${n}' id='${id}' value='1'/> <label for='${id}'>x</label><br>\n`;
const TEXT_ID = (n, id) => `<input type='text' name='${n}' id='${id}'>\n`;
// A full 3-option radio group (name `n`, ids `${p}-1..3`) — the button output.
const RADIO_GROUP = (n, p) => RADIO_ID(n, `${p}-1`) + RADIO_ID(n, `${p}-2`) + RADIO_ID(n, `${p}-3`);

// { label: [markdown, expect] } where expect is { name: bool, id: bool }.
// Omitted keys default to false; the form must abort iff name || id is true.
const cases = {
  // --- the reported bug: a select name reused by another field ------------
  'two identical selects':               [SELECT('q1') + SELECT('q1'), { name: true }],
  'multi-selects same name':             [SELECT_MULTI('q1') + SELECT_MULTI('q1'), { name: true }],
  'select then radio (regression)':      [SELECT('q1') + RADIO('q1'), { name: true }],
  'select then checkbox (regression)':   [SELECT('q1') + CHECKBOX('q1'), { name: true }],
  'radio then select':                   [RADIO('q1') + SELECT('q1'), { name: true }],
  'checkbox then select':                [CHECKBOX('q1') + SELECT('q1'), { name: true }],
  'text then select':                    [TEXT('q1') + SELECT('q1'), { name: true }],
  'select then text':                    [SELECT('q1') + TEXT('q1'), { name: true }],
  'textarea then select':                [TEXTAREA('q1') + SELECT('q1'), { name: true }],
  'selects split by markdown':           [`## Q1\ntext\n\n${SELECT('q1')}\n## Q2\n\n${SELECT('q1')}`, { name: true }],

  // --- non-select duplicates still caught --------------------------------
  'two text inputs same name':           [TEXT('q1') + TEXT('q1'), { name: true }],
  'two textareas same name':             [TEXTAREA('q1') + TEXTAREA('q1'), { name: true }],

  // --- duplicate ids on radio/checkbox groups (the new check) ------------
  'duplicate id within radio group':     [RADIO_ID('q1', 'id1-1') + RADIO_ID('q1', 'id1-1'), { id: true }],
  'two radio groups reuse ids':          [RADIO_GROUP('q1', 'id1') + RADIO_GROUP('q2', 'id1'), { id: true }],
  'duplicate id within checkbox group':  [CHECK_ID('q1', 'c1') + CHECK_ID('q1', 'c1'), { id: true }],
  'duplicate id on text inputs':         [TEXT_ID('q1', 'dup') + TEXT_ID('q2', 'dup'), { id: true }],
  'duplicate name and duplicate id':     [RADIO_ID('q1', 'x') + RADIO_ID('q1', 'x') + SELECT('q1'), { name: true, id: true }],

  // --- legitimate content that must NOT be flagged -----------------------
  'single select':                       [SELECT('q1'), {}],
  'distinct selects':                    [SELECT('q1') + SELECT('q2'), {}],
  'radio option group (no ids)':         [RADIO('q1') + RADIO('q1') + RADIO('q1'), {}],
  'checkbox option group (no ids)':      [CHECKBOX('q1') + CHECKBOX('q1'), {}],
  'radio group with unique ids':         [RADIO_GROUP('q1', 'id1'), {}],
  'two radio groups distinct ids':       [RADIO_GROUP('q1', 'id1') + RADIO_GROUP('q2', 'id2'), {}],
  'one of each distinct':                [TEXT('q1') + SELECT('q2') + TEXTAREA('q3') + RADIO('q4') + RADIO('q4'), {}],
  'empty guide':                         ['', {}],
};

// --- Run -------------------------------------------------------------------
const run = makeRunner();
let failures = 0;
for (const [label, [md, expect]] of Object.entries(cases)) {
  const expectName = !!expect.name;
  const expectId = !!expect.id;
  const expectAbort = expectName || expectId;

  const { ret, alert, prevented } = run(md);
  const gotAbort = ret === false;
  const hasNameMsg = !!alert && /Duplicated question name found/.test(alert);
  const hasIdMsg = !!alert && /Duplicated input id found/.test(alert);

  const ok =
    gotAbort === expectAbort &&
    prevented === expectAbort &&
    hasNameMsg === expectName &&
    hasIdMsg === expectId;

  if (ok) {
    console.log(`  ok    ${label}`);
  } else {
    failures++;
    console.error(`  FAIL  ${label}`);
    console.error(`        expected {name:${expectName}, id:${expectId}}, got abort=${gotAbort}, name=${hasNameMsg}, id=${hasIdMsg}, prevented=${prevented}, alert=${JSON.stringify(alert)}`);
  }
}

console.log(`\n${Object.keys(cases).length - failures}/${Object.keys(cases).length} passed`);
if (failures > 0) process.exit(1);
