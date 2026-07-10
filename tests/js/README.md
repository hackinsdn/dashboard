# JavaScript regression tests

Tests for inline JavaScript that ships inside the Jinja templates under
`apps/templates/`.

These run in Node with [jsdom](https://github.com/jsdom/jsdom) and load the
project's actual jQuery and `marked` builds from `apps/static/assets/plugins/`,
so they exercise the same code paths as the browser.

## Running

```bash
cd tests/js
npm install    # once, pulls jsdom
npm test
```

## Tests

- `parseLabGuide.test.js` — verifies duplicate-question detection in
  `apps/templates/pages/labs_edit.html`. The function source is extracted from
  the template at test time (not copied), so a regression in the template is
  caught. Covers the fix where a `<select>` question name reused by another
  field (including a following radio/checkbox) must be reported as a duplicate,
  while legitimate radio/checkbox option groups are not.

- `labAnswers.test.js` — verifies `saveAnswers()` / `applyAnswers()` in
  `apps/templates/pages/lab_instance_view.html`, which serialize the lab-guide
  form and repopulate it when the instance is reopened. Both functions are
  extracted from the template at test time. Covers text/textarea/select values,
  radio selections that aren't the last option, checkbox groups (stored as a
  sorted `", "`-joined string), value-aware reloading of checkboxes, untouched
  fields whose name is absent from the saved data, and a full save→apply
  round-trip.
