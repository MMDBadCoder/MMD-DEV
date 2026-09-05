# Critical invariant test map

Test counts are not a quality target by themselves. This map names the product
promises that must remain covered when modules or test files move. The fast
suite is SQLite/mock based; production-only gaps remain explicitly listed in
`PROJECT-REVIEW-1.7.md`.

| Invariant | Primary tests |
|---|---|
| Money is integer micro-Toman and hourly settlement is idempotent | `test_billing.py`, `test_ai_billing.py`, `test_hermes_credit_gate.py` |
| A credit top-up durably schedules and visibly confirms the supplier cap | `test_services_api.py`, `test_hermes_credit_gate.py` |
| Workspace create/reset/delete converge without retaining managed-service state | `test_reset.py`, `test_cleanup.py`, `test_optional_workspace.py` |
| Account deletion removes owned runtime resources and invalidates access | `test_cleanup.py`, `test_profiles.py` |
| Account status and credential changes invalidate sessions | `test_profiles.py`, `test_sms_auth.py` |
| Root-only actions cross the provisioner allowlist and validate arguments | `test_provisioner.py`, `test_file_security.py`, `test_services_api.py` |
| Port publication preserves HTTP internal-port and TCP/UDP external-port semantics | `test_port_protocols.py`, `test_vhosts.py` |
| Managed dashboards are not ready before their exact route is published | `test_vhosts.py`, `test_services_api.py` |
| Customer-visible API errors have stable codes and Persian UI translations | `test_no_english_prose.py`, `web/i18n.test.mjs`, `web/operations-ux.test.mjs` |
| Secrets do not enter API prose, metrics labels, or destructive confirmations | `test_no_english_prose.py`, `test_metrics_exporter.py`, `web/ux-7-10.test.mjs` |
| Keyboard and mobile users retain navigation, focus, status, and recovery paths | `web/accessibility-ux.test.mjs`, `web/reliability-ux.test.mjs`, `web/final-low-risk-ux.test.mjs` |

When a new product promise does not fit one row, add a row rather than hiding it
under a nearby category. When a test is removed, update this map in the same
change so the missing invariant is visible in review.
