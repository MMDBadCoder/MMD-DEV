# Interface design system

MMD-DEV is a Persian RTL application for developers. It has no build step;
consistency comes from shared primitives and behavioural tests.

## Rules

- Customer-visible prose belongs in `web/js/i18n.js`; API handlers return
  stable codes plus English developer messages.
- Use `fmtMoney` for Toman, `fmtFa` for prose, and `fmtNum` for copyable
  technical values. Escape untrusted values and isolate Latin identifiers.
- Never use colour alone. Icon-only controls need accessible names and
  decorative SVGs stay hidden from assistive technology.
- Phone controls are at least 44 pixels. Check 320, 360, 390, 768 and desktop.

## Primitives

- `statePill`: labelled state and tone.
- `note`: inline `ok`, `info`, `warn`, or `bad` feedback.
- `recoveryNote` plus `wireRecovery`: failure and one useful next action.
- `toast`: status semantics for success/info and alert semantics for failure.
- `empty`: explanation plus the next useful action when one exists.
- `confirmDialog`, `dangerDialog`, `destructiveDialog`: labelled modal, focus
  trap, Escape cancellation and focus restoration. Destructive dialogs name
  kept and deleted data.
- `secretRow` plus `wireSecrets`: consistently labelled reveal/copy controls.
- `usageChart`, `multiChart`, `spendChart`: absolute units plus a text legend
  or expandable data table.
- Page tabs are labelled navigation with `aria-current=page`. True in-page
  tabs implement tablist, selected state, controlled panels and arrow keys.
- The global operation strip exposes durable work in a polite live region.

## Navigation and forms

On phones, Overview, Connections, AI and Billing stay in the bottom rail;
secondary destinations live in the labelled More sheet. Every page has a main
landmark and the document begins with a skip link.

Every input has a label, correct autocomplete/input mode, and retains its
value after failure. Disable submission only while a request is in flight.
Recoverable prerequisites should link to their resolution. New validation
must use `aria-invalid`, connect field errors, and focus the first invalid
field.

## Motion and async work

Reduced-motion preference collapses decorative motion. Pollers stop while the
document is hidden. Route loaders use the generation boundary, and loading
failures render the shared recovery surface rather than a blank page.

## Review checklist

1. Run `bash tests/run.sh`.
2. Complete the journey with keyboard only, including error and cancellation.
3. Check overflow, touch size, long Persian names and Latin identifiers.
4. Confirm outcomes are announced and failures retain a next action.
5. Confirm no new prose bypasses the catalogue and no secret enters HTML/logs.
