/*
 * Regression tests for saveAnswers() and applyAnswers() in
 * apps/templates/pages/lab_instance_view.html
 *
 * saveAnswers() collects the lab-guide form into a { name: value } object and
 * POSTs it; applyAnswers() takes such an object back and repopulates the form.
 * Together they must round-trip text, textarea, select, radio groups and
 * checkbox groups without losing or mangling a value.
 *
 * These tests extract the real functions from the template (rather than copying
 * their bodies) and exercise them with the project's actual jQuery build, so a
 * regression in the shipped template is caught. $.ajax is stubbed to capture
 * the POST body instead of hitting the network.
 *
 * Run with:  cd tests/js && npm install && npm test
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const TEMPLATE = path.join(REPO_ROOT, 'apps', 'templates', 'pages', 'lab_instance_view.html');
const JQUERY = path.join(REPO_ROOT, 'apps', 'static', 'assets', 'plugins', 'jquery', 'jquery.min.js');

// --- Extract a named function's real source from the template --------------
function extractFunction(src, marker) {
  const start = src.indexOf(marker);
  if (start === -1) throw new Error(`${marker} not found in template`);
  let depth = 0;
  let seenBrace = false;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') { depth++; seenBrace = true; }
    else if (ch === '}') { depth--; }
    if (seenBrace && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`Could not brace-match ${marker} body`);
}

// --- Build a jsdom window with jQuery and the real functions ---------------
function makeEnv() {
  const jquerySrc = fs.readFileSync(JQUERY, 'utf8');
  const src = fs.readFileSync(TEMPLATE, 'utf8');
  // Jinja placeholders ({{ ... }}) are not valid JS; stub them out.
  const stub = (s) => s.replace(/\{\{[^}]*\}\}/g, 'STUB');
  const saveSrc = stub(extractFunction(src, 'function saveAnswers'));
  const applySrc = stub(extractFunction(src, 'function applyAnswers'));

  const dom = new JSDOM(
    `<!DOCTYPE html><body><div id="lab-guide"></div>` +
    `<script>${jquerySrc}</script></body>`,
    { runScripts: 'dangerously' }
  );
  const { window } = dom;
  const $ = window.$;

  // toast plugin used on save success/failure paths
  $.fn.Toasts = function () { return this; };

  // capture the POST payload instead of hitting the network
  $.ajax = function (opts) {
    if (opts.type === 'POST') {
      window.__lastPost = JSON.parse(opts.data);
    }
    return { done() { return this; }, fail() { return this; } };
  };

  window.eval(
    saveSrc + '\n' + applySrc + '\n' +
    'window.__saveAnswers = saveAnswers; window.__applyAnswers = applyAnswers;'
  );

  return {
    $,
    setForm(html) { $('#lab-guide').html(html); },
    save() {
      window.__lastPost = undefined;
      window.__saveAnswers(false);
      return window.__lastPost;
    },
    apply(result) { window.__applyAnswers(result); },
    // read helpers
    val(sel) { return $(sel).val(); },
    checked(sel) { return $(sel).prop('checked'); },
  };
}

// --- A form covering every input kind the guide can contain ----------------
const FORM =
  `<input type='text' name='q_text'>` +
  `<textarea name='q_ta'></textarea>` +
  `<select name='q_sel'><option value=''>--</option><option value='a'>a</option><option value='b'>b</option></select>` +
  `<input type='radio' name='q_radio' value='A'>` +
  `<input type='radio' name='q_radio' value='B'>` +
  `<input type='radio' name='q_radio' value='C'>` +
  `<input type='checkbox' name='q_cb' value='X'>` +
  `<input type='checkbox' name='q_cb' value='Y'>` +
  `<input type='checkbox' name='q_cb' value='Z'>` +
  `<input type='checkbox' name='q_single'>`;

// --- Assertion helpers -----------------------------------------------------
let failures = 0;
let total = 0;
function check(label, cond, detail) {
  total++;
  if (cond) {
    console.log(`  ok    ${label}`);
  } else {
    failures++;
    console.error(`  FAIL  ${label}`);
    if (detail) console.error(`        ${detail}`);
  }
}
function eq(label, got, want) {
  check(label, JSON.stringify(got) === JSON.stringify(want),
        `expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
}

const env = makeEnv();
const { $ } = env;

// === saveAnswers: collects every field type ================================
{
  env.setForm(FORM);
  $("input[name=q_text]").val('hello world');
  $("textarea[name=q_ta]").val('line1\nline2');
  $("select[name=q_sel]").val('b');
  $("input[name=q_radio][value=B]").prop('checked', true);
  $("input[name=q_cb][value=X]").prop('checked', true);
  $("input[name=q_cb][value=Z]").prop('checked', true);
  $("input[name=q_single]").prop('checked', true);

  const post = env.save();
  eq('save: text value',              post.q_text, 'hello world');
  eq('save: textarea value',          post.q_ta, 'line1\nline2');
  eq('save: select value',            post.q_sel, 'b');
  eq('save: radio keeps selection',   post.q_radio, 'B');
  eq('save: checkbox group joined',   post.q_cb, 'X, Z');   // sorted, ", "-joined
  eq('save: single checkbox',         post.q_single, 'on');
}

// === saveAnswers: radio selection isn't lost when it's not the last option ==
{
  env.setForm(FORM);
  $("input[name=q_radio][value=A]").prop('checked', true);  // first option
  const post = env.save();
  eq('save: first radio not clobbered by later unchecked siblings', post.q_radio, 'A');
}

// === saveAnswers: a fully-blank form is NOT posted (never wipe answers) =====
// Firefox re-blanks the guide inputs after load and fires change events; the
// guard in saveAnswers must skip the POST entirely so an all-empty payload
// can't overwrite previously saved answers.
{
  env.setForm(FORM);
  const post = env.save();
  eq('save: blank form suppresses POST', post, undefined);
}

// === saveAnswers: blank fields still serialize as "" in a partial save ======
// When at least one field has a value the POST goes through, and the empty
// fields must still serialize as "" so they round-trip via applyAnswers.
{
  env.setForm(FORM);
  $("input[name=q_text]").val('answered');
  const post = env.save();
  eq('save: partial POST happens',     post && post.q_text, 'answered');
  eq('save: unchecked checkbox group', post.q_cb, '');
  eq('save: no radio selected',        post.q_radio, '');
}

// === applyAnswers: repopulates a blank form ================================
{
  env.setForm(FORM);
  env.apply({
    q_text: 'restored',
    q_ta: 'a\nb',
    q_sel: 'a',
    q_radio: 'C',
    q_cb: 'Y, Z',
    q_single: 'on',
  });
  eq('apply: text',        env.val('input[name=q_text]'), 'restored');
  eq('apply: textarea',    env.val('textarea[name=q_ta]'), 'a\nb');
  eq('apply: select',      env.val('select[name=q_sel]'), 'a');
  eq('apply: radio C checked',  env.checked('input[name=q_radio][value=C]'), true);
  eq('apply: radio A unchecked', env.checked('input[name=q_radio][value=A]'), false);
  eq('apply: checkbox Y checked', env.checked('input[name=q_cb][value=Y]'), true);
  eq('apply: checkbox Z checked', env.checked('input[name=q_cb][value=Z]'), true);
  eq('apply: checkbox X unchecked (value-aware)', env.checked('input[name=q_cb][value=X]'), false);
  eq('apply: single checkbox', env.checked('input[name=q_single]'), true);
}

// === applyAnswers: only a single value in a group checks exactly that box ==
{
  env.setForm(FORM);
  env.apply({ q_cb: 'Y' });
  eq('apply: only Y checked',  env.checked('input[name=q_cb][value=Y]'), true);
  eq('apply: X stays unchecked', env.checked('input[name=q_cb][value=X]'), false);
  eq('apply: Z stays unchecked', env.checked('input[name=q_cb][value=Z]'), false);
}

// === applyAnswers: leaves fields whose name is absent untouched ============
{
  env.setForm(FORM);
  $("input[name=q_text]").val('prefilled');
  env.apply({ q_radio: 'B' });   // no q_text key
  eq('apply: absent field left intact', env.val('input[name=q_text]'), 'prefilled');
}

// === Full round-trip: save then apply reproduces the original selection ====
{
  env.setForm(FORM);
  $("input[name=q_text]").val('round trip');
  $("select[name=q_sel]").val('b');
  $("input[name=q_radio][value=C]").prop('checked', true);
  $("input[name=q_cb][value=X]").prop('checked', true);
  $("input[name=q_cb][value=Y]").prop('checked', true);
  const post = env.save();

  env.setForm(FORM);              // fresh, blank form
  env.apply(post);
  eq('round-trip: text',   env.val('input[name=q_text]'), 'round trip');
  eq('round-trip: select', env.val('select[name=q_sel]'), 'b');
  eq('round-trip: radio C', env.checked('input[name=q_radio][value=C]'), true);
  eq('round-trip: checkbox X', env.checked('input[name=q_cb][value=X]'), true);
  eq('round-trip: checkbox Y', env.checked('input[name=q_cb][value=Y]'), true);
  eq('round-trip: checkbox Z unchecked', env.checked('input[name=q_cb][value=Z]'), false);
}

console.log(`\n${total - failures}/${total} passed`);
if (failures > 0) process.exit(1);
