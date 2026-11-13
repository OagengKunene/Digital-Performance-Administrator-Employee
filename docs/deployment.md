# Deployment plan

Validate in Codespaces first. No cloud resources or provider accounts are created by this repository.

## Processes and storage

The application has three processes: FastAPI (8000), Streamlit (8501), and the durable capture, delivery and evaluation worker. `scripts/start.py` starts all three for Codespaces and local Compose, stopping the others if any process exits. PostgreSQL stores records, checkpoints, leases, follow-ups and policies. Container files and Codespaces volumes are not hosted backups.

Run `python -m pae.manage init` as a controlled deployment step before starting the new version. This adds the new tables and preserves old users/marks/report history. Take a backup before real-school upgrades. Formal versioned production migrations are still a delivery requirement.

Use provider secrets for `GOOGLE_API_KEY` or `GROQ_API_KEY`. Select `AI_PROVIDER` explicitly. `AI_FALLBACK_PROVIDER` is empty by default; setting it authorises the configured second provider for a failed stage and requires that provider's secret.

## Google Cloud hosting shape

Use separate services from the image:

- API command: `python -m uvicorn pae.api:app --host 0.0.0.0 --port 8080`.
- UI command: `python -m streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8080 --browser.gatherUsageStats false`.
- Worker command: `python -m pae.worker`.

The worker needs an always-running execution environment with CPU available between requests, or a separately designed scheduled job runner. A request-scaled web service that becomes idle will not run continuous monitoring reliably. A Cloud Run configuration requires deliberate worker CPU/instance settings; these are not provisioned or verified here. Do not present a Codespace as 24/7 deployment.

Set the UI's `PAE_API_URL` to the API HTTPS URL. School-data routes enforce application authentication and class scope. An IAM-private API additionally needs authenticated service-to-service requests, which the current UI client does not implement. Configure ingress/IAM and TLS before exposing school records.

Supply an externally persistent PostgreSQL database and managed credentials. Local development Compose database credentials are not suitable for hosting. Initialise the first real principal with the management command and use a separate database from fictional sample records.

## Worker and provider limits

SQL jobs use atomic claims, owner-fenced writes and a unique active-class slot. Nevertheless, the provider request pacer is local to one worker. Keep one worker until a shared provider RPM/TPM/daily ledger is implemented. Multiple worker replicas can collectively exceed provider limits even when SQL claims are safe.

Tune `AI_REQUESTS_PER_MINUTE`, `AI_TIMEOUT_SECONDS` (per stage), `AI_MAX_MODEL_CALLS` (whole-report dispatch budget), and the automatic-initiation caps for actual account limits. The API returns queued jobs immediately; the UI polls short requests, so an API request no longer waits for a multi-minute model workflow.

Graceful worker shutdown requeues unfinished work. After an abrupt kill, another worker recovers when the lease expires. A request may be repeated if its provider reply was lost before checkpointing. Backups must include job and policy tables as well as school records.

## Before a school rollout

Complete account recovery, formal migrations, distributed usage controls, live mail-server validation, data retention, backup/restore, load tests and pilot evaluation. Paid/free provider allowances and managed database costs depend on the account and usage. Provider access and live end-to-end Groq quality require the user's account; mocked tests validate integration and recovery, not provider availability.

## Automatic sources and SMTP

See [automatic capture](automatic-capture.md). Persist the worker's `INGESTION_INBOX_ROOT` for watched exports, or configure the school export system to use the authenticated source-feed endpoint. The source key is private and scoped to one class and record type. Ingestion and factual inbox delivery continue while model work is waiting. Supply SMTP credentials through private deployment secrets, verify real recipient addresses, and validate delivery with the school before using email operationally. In-app notifications need no SMTP setup. Source archives, capture snapshots and report deliveries require retention and backups.
