# Test report

Run 2026-10-29 on PostgreSQL 16 and Chromium, with the scripted model: 143 tests, 0 failures, 0 errors, 218 s.

| Suite | Passed | Failed | Seconds |
|---|---|---|---|
| `tests/test_api.py` | 28 | 0 | 47 |
| `tests/test_browser.py` | 3 | 0 | 14 |
| `tests/test_data.py` | 7 | 0 | 17 |
| `tests/test_integration.py` | 17 | 0 | 34 |
| `tests/test_live.py` | 17 | 0 | 23 |
| `tests/test_mail.py` | 6 | 0 | 4 |
| `tests/test_routing_golden.py` | 25 | 0 | 52 |
| `tests/test_unit.py` | 40 | 0 | 27 |

Regenerate: `pytest -m 'not live' --junitxml=junit.xml` (the browser tests need the built front end).
CI runs the same suites on every push: see the Actions tab.
