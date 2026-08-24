## What and why

<!-- What changed, and what problem it solves. If it fixes a bug, what was the
     actual cause? "Fixed the layout" is less useful than "the fit addon measured
     the border-box height". -->

## How it was checked

- [ ] `bash tests/run.sh` passes
- [ ] `bash verify/p0-foundation.sh` passes (if the host, storage or network changed)
- [ ] Exercised against a real or disposable workspace (if the lifecycle changed)
- [ ] New behaviour has a test

<!-- If something was measurable, give the number. -->

## Not verified

<!-- Say plainly what you did not check. An honest gap is more useful than a
     confident claim that turns out to be untested. -->

## Notes

- [ ] Customer-visible text goes through `web/js/i18n.js`; the API returns a code
- [ ] Anything privileged goes through a provisioner verb, with its own validation
- [ ] `docs/DECISIONS.md` updated if the cause was not obvious
