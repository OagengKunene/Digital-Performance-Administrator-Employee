# Automatic capture, evaluation and delivery

After a class and its roster are configured, each accepted marks or attendance import and each actual mark correction automatically creates an immutable capture event. Reporting does not depend on the optional monitoring switch, a task prompt, or a Run button. An unchanged upload creates no new event.

## What happens automatically

1. The source processor validates the entire CSV against the authorised class roster, score limits and consistent topic maxima. It fills blank marks and inserts new records. Replacing existing nonblank values requires correction authority configured for that source; changes are audited. A blank source value never erases an existing mark.
2. The same database transaction saves the changed records, source snapshot, a factual performance evaluation, evidence-based teacher follow-ups and inbox deliveries to the assigned teacher and every principal in that school. A transaction failure leaves none of these partly saved. The evaluation includes the correct assessment/topic units, support-threshold counts, evidence limitations and source-supported next actions.
3. The worker automatically queues the ADK coordinator and specialists for that capture when the selected provider is configured. It saves each successful stage and independently reviews the resulting draft. An action whitelist, schema checks and source-metric checks reject known unsupported decisions. Invalid stage output is repaired within the original attempt budget.
4. An approved agent report is delivered to the same staff inboxes. Transient provider failures get bounded delayed retries. Captured evaluations that pause on quota, credentials or provider failures are reconsidered after `CAPTURE_RETRY_SECONDS`, keeping their original total request budget. Persistent failures produce an attention notice; the factual report has already arrived.
5. Configured SMTP sends queued email copies to opted-in real staff addresses, with delivery status and bounded retries. In-app delivery does not require SMTP.

Ingestion and factual delivery run in a separate control loop from model evaluation. A waiting model request does not block new source captures. AI interpretations are processed one report at a time, so heavy capture volume can create a queue. Every accepted batch receives its factual report immediately after processing; agent reports follow as capacity permits.

## Connect a school export once

Open **Assessment sources**, connect a marks or attendance source, and select its correction policy. A source is bound to one class and record type. Choose either integration:

- Configure the existing school system's scheduled CSV export to write completed files into the displayed server inbox folder. Write a temporary filename and atomically rename it to `.csv` after export completes. The worker watches it, persists an idempotent batch and archives it under `processed`. Export destinations must be accessible to the worker; a Codespace folder is not a folder on a teacher's laptop.
- Have the export system POST raw UTF-8 CSV to the displayed `/ingestion/<source-id>` API endpoint. Put its private source key in `X-Ingestion-Key`. This scoped machine credential does not expire with an interactive login. It grants only ingestion for that source, not report reading, roster changes or arbitrary class access. Save it privately when the source is connected; rotate it in the interface if needed. Use the deployed API HTTPS address when calling from another system.

The producer must still be configured to export/send data; registering a source cannot discover an unrelated school database. The supported input is already marked structured CSV. OCR of handwritten scripts, rubric-based marking and direct school-system connectors are not implemented. Missing evidence generates an exception for the teacher; the employee never invents a learner's score.

Marks columns:

```csv
learner_id,assessment,topic,earned,possible
G101-004,Sample Test 1,Algebra,15,20
```

Attendance columns:

```csv
learner_id,day,status
G101-004,2026-11-13,present
```

At most 2 MB and 5,000 records per batch. Export only learners already in this class's roster. Repeated identical CSV content is idempotent per source. Different exports with unchanged values also produce no extra capture/report. Unknown roster entries wait for supporting evidence and retry; conflicting marks are rejected atomically and routed as exceptions. Fix a rejected export and submit the corrected file. The processor does not silently resolve conflicting source scores.

## Read the results

**Reports inbox** shows the staff member's delivered reports, snapshot time and email status. **Assessment sources** shows ingestion batches and automatically queued evaluations. **Agent reports** retains detailed stage progress and optional manual investigations. No manual task entry is needed for routine mark captures.

Reports reflect the saved state at each capture. If another capture arrives during evaluation, both capture reports remain valid historical snapshots with timestamps. An older evaluation must not be read as the latest class state. Manual and scheduled-current-state reports still block publication if their source snapshot changed.

A report counts marked learner-assessment-topic records separately from distinct assessments. A learner below the class mean is not automatically below the configured support threshold. A missing captured mark is not proof that the learner missed the assessment. No recorded attendance data means attendance cannot yet be evaluated. `verified_metrics` means source checks passed, not that all educational judgments or real-world outcomes are proven. Staff can inspect and override recommendations; the agent does not award official promotion results.

## Email configuration

In-app reports are always delivered to authenticated staff inboxes. To add real email, configure these private server settings (or Codespaces secrets) and restart the application:

```dotenv
EMAIL_DELIVERY_ENABLED=true
SMTP_HOST=your-school-mail-host
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=your-service-account
SMTP_PASSWORD=your-private-password
SMTP_FROM=reports@your-school-domain
```

Use `SMTP_SECURITY=ssl` and the mail provider's appropriate port for implicit TLS. Both transports verify TLS certificates; there is no plaintext SMTP option. No credentials should be committed or pasted into chat.

Create staff accounts with their real email addresses and enable **Reports inbox → Email delivery settings → Also email future reports** once. Sample `.test` and reserved example-domain addresses cannot receive external mail. Principals can set a same-school staff preference through `PUT /staff/{id}/notification-preferences`; recipients always come from the class assignment and school roles, never from model-generated addresses. Preferences apply to future notifications. Pending sends recheck permissions and preferences.

The outbox records `pending`, `blocked_configuration`, `retrying`, `sent`, `failed` or `skipped`. `sent` means the SMTP server accepted the message; it does not prove the recipient opened it. A crash after SMTP acceptance but before checkpointing can cause a repeated email; stable Message-ID values assist downstream deduplication. Delivery is at least once, not exactly once. Tests mock SMTP and do not establish that any live mail account is configured.

## Operations and limits

Keep the API, worker, PostgreSQL and export producer running for unattended operation. Codespaces can stop when idle; deploy on an always-running host for continuous school service. The source inbox needs persistent storage if used. Source CSV archives, SQL capture snapshots and notification payloads contain school records and must be covered by retention, access control and backup policies.

Capture-triggered evaluations are mandatory and independent of the scheduled-monitor daily initiation cap. Every capture can consume up to `AI_MAX_MODEL_CALLS` dispatched attempts. Set provider limits and funding for expected capture volume; the factual report works without AI quota. Avoid unnecessary row-by-row exports when one assessment batch will do. Use one worker until a shared provider rate ledger is implemented.
