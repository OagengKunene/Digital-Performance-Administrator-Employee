# Operational reliability validation

Validation date: 14 November 2026. Starting revision: `f5614e4`.
All records and addresses used by these checks are fictional.

## Failures reproduced and fixed

1. A temporary database exception while saving the worker heartbeat escaped the
   control task and stopped the worker's entire task group. The control loop now
   logs only the exception type and retries after its normal poll interval.
   Cancellation still propagates so shutdown remains possible.
2. Automatic capture recovery inspected only the first 20 failed captures before
   checking whether they could be retried. Twenty old failures could permanently
   hide later recoverable work. The database query now filters for due jobs with
   remaining attempts, a recoverable error and a configured provider before
   applying its limit; classes with an active job are excluded as well.

The new regression checks reproduce the backlog failure against the original
recovery function for cooldown, exhausted budget, nonrecoverable output and
unconfigured provider backlogs, and pass with the fix. A separate check confirms
control-loop survival after a simulated heartbeat database exception and ensures
private exception details do not enter its logs.

## Repeatable checks

Install the pinned dependencies and project, then run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/reliability_check.py
```

The reliability script requires Linux process signals and takes about two minutes.
It creates its own temporary SQLite database, generated credentials, watched
directory, API process and worker processes. It overrides database/provider/mail
settings, uses dynamically selected loopback ports, deletes the test data on exit,
and terminates its child processes even on failure. No school database is opened.
Optional `--output results.json` writes a compact results record.

The existing smoke check also starts and checks the Streamlit interface. The new
process check uses HTTP and separate worker processes to verify:

| Scenario | Required result |
| --- | --- |
| Valid row followed by an impossible score | Entire import rolls back; no partial capture; staff receive an exception notice. |
| Export accepted while the worker is stopped | Restart consumes the saved batch without another prompt. |
| No live inference credentials | Factual reports reach the assigned teacher and school principal; unrelated teacher sees no reports. Source mean is exactly 75%, four marked records, zero missing. |
| Restart with the offline provider configured | The pending capture's reviewed report completes automatically through the normal job pipeline. |
| SIGKILL during an unfinished provider stage | The completed coordinator checkpoint and both dispatched attempts remain persisted. |
| Restart after SIGKILL | The actual 61-second test lease expires naturally; only unfinished work repeats. Historical report means remain 75% and 68%; a repeated export creates no extra capture or inbox reports. |
| SMTP unavailable, then restored across worker restart | Pending deliveries retry after the real 60-second delay and reach a certificate-verified local TLS SMTP sink. The sink never relays mail. |

The SMTP test generates a short-lived localhost certificate and supplies its trust
anchor only to the test subprocesses. Certificate verification stays enabled. The
test records stable, distinct Message-IDs and checks the persisted retry count.

GitHub Actions runs the full suite against both SQLite and PostgreSQL, repeats
the process reliability check, and builds/smoke-tests the Docker image.

## Scope of the evidence

The inference stages in the process check use deterministic offline rules. The
unit/integration suite separately injects provider 503 errors, quota errors,
invalid metrics, rejected recommendations and review repairs, including the actual
ADK/LiteLLM connector with inference replaced. These checks establish orchestration
and recovery behavior; they do not measure live Gemini/Groq quality or availability.

The local SMTP check proves application transport and retry behavior. It does not
prove delivery to a real school's server, inbox placement or that a person read
the message. SMTP acceptance followed by a worker crash before the database
acknowledgement can still result in a repeated send; Message-ID is stable, but
external exactly-once email delivery is not guaranteed.

These are operational regression checks, not a load test or a school pilot.
Always-on hosting, managed backups/restore, real source integration, live provider
evaluation and real-school mail validation remain deployment work. Keep a single
worker until provider pacing is shared between processes, as described in the
deployment guide.
