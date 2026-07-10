/*
 * Regression tests for parseLabGuide() in
 * apps/templates/pages/labs_edit.html
 *
 * parseLabGuide() previews the lab guide Markdown and rejects the form when two
 * questions share the same `name` attribute. radio/checkbox inputs are allowed
 * to repeat a name (they form a single option group); every other repeat is a
 * duplicate question.
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

// { name: [markdown, expectDuplicate] }
const cases = {
  // --- the reported bug: a select name reused by another field ------------
  'two identical selects':               [SELECT('q1') + SELECT('q1'), true],
  'multi-selects same name':             [SELECT_MULTI('q1') + SELECT_MULTI('q1'), true],
  'select then radio (regression)':      [SELECT('q1') + RADIO('q1'), true],
  'select then checkbox (regression)':   [SELECT('q1') + CHECKBOX('q1'), true],
  'radio then select':                   [RADIO('q1') + SELECT('q1'), true],
  'checkbox then select':                [CHECKBOX('q1') + SELECT('q1'), true],
  'text then select':                    [TEXT('q1') + SELECT('q1'), true],
  'select then text':                    [SELECT('q1') + TEXT('q1'), true],
  'textarea then select':                [TEXTAREA('q1') + SELECT('q1'), true],
  'selects split by markdown':           [`## Q1\ntext\n\n${SELECT('q1')}\n## Q2\n\n${SELECT('q1')}`, true],

  // --- non-select duplicates still caught --------------------------------
  'two text inputs same name':           [TEXT('q1') + TEXT('q1'), true],
  'two textareas same name':             [TEXTAREA('q1') + TEXTAREA('q1'), true],

  // --- legitimate content that must NOT be flagged -----------------------
  'single select':                       [SELECT('q1'), false],
  'distinct selects':                    [SELECT('q1') + SELECT('q2'), false],
  'radio option group':                  [RADIO('q1') + RADIO('q1') + RADIO('q1'), false],
  'checkbox option group':               [CHECKBOX('q1') + CHECKBOX('q1'), false],
  'one of each distinct':                [TEXT('q1') + SELECT('q2') + TEXTAREA('q3') + RADIO('q4') + RADIO('q4'), false],
  'empty guide':                         ['', false],
};

// --- Run -------------------------------------------------------------------
const run = makeRunner();
let failures = 0;
for (const [label, [md, expectDup]] of Object.entries(cases)) {
  const { ret, alert, prevented } = run(md);
  const gotDup = ret === false;
  const ok =
    gotDup === expectDup &&
    (expectDup ? alert && /Duplicated question name found/.test(alert) && prevented
               : alert === null && !prevented);
  if (ok) {
    console.log(`  ok    ${label}`);
  } else {
    failures++;
    console.error(`  FAIL  ${label}`);
    console.error(`        expected duplicate=${expectDup}, got=${gotDup}, alert=${JSON.stringify(alert)}, prevented=${prevented}`);
  }
}

console.log(`\n${Object.keys(cases).length - failures}/${Object.keys(cases).length} passed`);
if (failures > 0) process.exit(1);
