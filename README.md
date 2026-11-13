# Digital Performance Administrator Employee

An autonomous, multi-agent school employee under active development. It proactively monitors authorised class records, creates in-app teacher follow-ups, plans investigations with an ADK coordinator, delegates to specialist agents, evaluates their reports and preserves progress after failures.

Every accepted marks/attendance capture now automatically evaluates the class and delivers a factual report to its assigned teacher and school principals. The ADK team adds a reviewed agent interpretation in the background. Connect a CSV export source once; routine captures need no task prompt or Run button. In-app inboxes work by default; optional TLS SMTP adds email.

See [automatic capture and delivery](docs/automatic-capture.md) for source setup, routing, retries and email configuration. Curriculum planning, handwritten script/rubric assessment and teacher-absence cover materials remain development work.

## Upgrade your existing Codespace

Stop the running application with Ctrl+C, then run:

```bash
git pull --ff-only origin main
.venv/bin/python scripts/setup.py
```

Setup installs the pinned environment and adds the new capture/source/delivery tables.
Existing users, marks, credentials and historical reports are preserved.
Do not recreate the sample school or delete the database.

To test Groq, add `GROQ_API_KEY` under your GitHub account **Settings → Codespaces → Secrets**, grant this repository access, then stop and restart the whole Codespace. Obtain the key from [Groq Console](https://console.groq.com/keys). Keep keys out of chat, commits and screenshots.

Start all three services:

```bash
AI_PROVIDER=groq .venv/bin/python scripts/start.py
```

This starts the authenticated API, durable worker and staff interface. Open **Ports → 8501 → Open in Browser**. Sign in with your existing credentials from `.local/bootstrap-login.txt`.

The default provider remains Gemini for compatibility. Use `AI_PROVIDER=gemini` with `GOOGLE_API_KEY` and `GEMINI_MODEL`, or set `AI_PROVIDER=groq` with `GROQ_API_KEY` and `GROQ_MODEL`. The Groq model default is `openai/gpt-oss-20b`. Model access and provider quotas still need validation in your account; local tests replace inference.

## First-time setup

1. Open **Code → Codespaces → Create codespace on main**.
2. Wait for setup to complete. PostgreSQL runs inside the Codespace configuration.
3. For fictional evaluation records, run:

   ```bash
   .venv/bin/python -m pae.manage bootstrap --sample
   ```

4. Open `.local/bootstrap-login.txt` privately for generated passwords.
5. Start `scripts/start.py` as above and open port 8501.

Sample accounts: `principal@example.test`, `teacher@example.test`, `other.teacher@example.test`. Teachers see assigned classes; the principal sees school classes. Grade 10A has three recorded marks averaging **75%** and one missing mark out of four expected. Missing results remain distinct from zero.

## Automatic reports after mark capture

Open **Assessment sources**, register the school's marks or attendance export and configure its displayed watched directory or authenticated upload endpoint. The worker validates and captures new exports automatically. The assigned teacher and school principals receive a data report in **Reports inbox**, followed by a reviewed agent report when the provider is available. Ordinary imports and audited mark corrections trigger the same workflow automatically.

No task prompt is required. Conflicting or unsupported source values are flagged for evidence; marks are never guessed. Read the [setup guide](docs/automatic-capture.md) before connecting an upstream source or SMTP. **Email requires a configured mail server and real registered staff addresses; fictional sample accounts receive in-app reports.**

## Optional manual agent investigations

Select a class, open **Agent reports**, enter a goal and choose **Run agent team**.
The API returns immediately with a queued job. A worker performs these stages:

1. **Coordinator:** interprets the goal and evidence, decides which specialists are needed.
2. **Performance analyst:** investigates results and evidence gaps when delegated.
3. **Intervention planner:** proposes support and follow-up when delegated.
4. **Reporting agent:** drafts the teacher/principal report.
5. **Quality reviewer:** independently checks the full draft and source metrics.

Successful stages are saved to SQL before the next one starts. The normal full path has five model requests; the coordinator can omit the two optional specialist stages. Each stage permits one inference attempt per dispatch. Repairs and provider retries use additional attempts within the whole-report budget, which defaults to ten.

The reviewer can send a draft back for up to two repairs. Host code checks the report's structured metrics and the reviewer's checked metrics against the original evidence. Changed school records block publication for manual and scheduled-current-state investigations. Capture evaluations remain explicitly timestamped historical snapshots when newer captures arrive.

If a stage fails, its completed predecessors remain visible in **Saved intermediate work**. Selected transient errors receive bounded, delayed retries of that stage. Quota/authentication errors pause. Capture-triggered work is reconsidered after a configurable cooldown within the original total attempt budget. **Resume from saved progress** can use a configured provider for unfinished work. Historical reports created before this version have no stage checkpoints and cannot be resumed.

The interface polls progress while open. You may leave and return; the worker runs independently. **Cancel further processing** fences the job against later publication. An inference request already sent may still finish. A statistical report without AI interpretation is always available separately.

`verified_metrics` is not certification of all narrative claims or educational outcomes. Intermediate outputs are AI drafts for staff review. The system does not autonomously alter official marks or claim recommendations were carried out.

## Enable autonomous monitoring

As principal, select a class and open **Autonomous monitoring**:

- Enable record monitoring and choose its check interval.
- Optionally check again after marks, attendance, roster or intervention changes.
- Optionally enable automatic AI investigations when evidence changes.

The monitor needs no prompt for each cycle. It detects missing captured topic marks, learners below the configured support-review threshold, supplied absence records and overdue recorded interventions. It assigns in-app follow-ups to the class teacher, closes issues when evidence resolves them, and flags overdue follow-ups to the principal. Teachers can record acknowledgements and resolution evidence.

Scheduled AI investigations are opt-in. Capture-triggered reports run automatically and are independent of this scheduled-monitor policy. They use the coordinator and specialists, not fabricated reports. Limits apply per class and installation: `AUTONOMOUS_DAILY_REPORT_LIMIT` defaults to **one initiated scheduled-monitor report per UTC day** across the installation. Capture-triggered and manual reports consume provider quota separately and do not use this scheduled cap. This application cap does not replace the provider's quota system. Failed/cancelled automatic jobs conservatively consume the initiation cap.

Monitoring and reports run only while the worker is running. Codespaces is a development environment, not an always-on school host. Teacher/principal report inboxes are implemented, with optional durable TLS SMTP delivery. SMS is not implemented.

## Provider and worker settings

See `.env.example` for configuration names. Preserve Codespaces' existing `DATABASE_URL`; do not replace it with an unrelated example URL.

- `AI_PROVIDER`: `gemini` or `groq`.
- `AI_FALLBACK_PROVIDER`: empty by default. Explicitly configure a second provider and its secret to permit fallback for a failed stage.
- `AI_MAX_MODEL_CALLS`: whole-report dispatched-attempt budget, including retries and resumes; default 10.
- `AI_TIMEOUT_SECONDS`: per-stage deadline, including pacing; default 300.
- `AI_REQUESTS_PER_MINUTE`: per-model gate inside one worker; default 4.
- `AI_RETRY_ATTEMPTS`: maximum attempts at a failing stage before pause; default 3.
- `WORKER_POLL_SECONDS`: idle polling interval; default 2.
- `AUTONOMOUS_DAILY_REPORT_LIMIT`: global automatic-initiation cap; default 1.

Gemini's first model request in a worker has a 60-second cooldown. Groq starts without that cooldown. Requests are paced and SDK automatic retries are disabled so extra calls cannot bypass this gate. External application traffic and provider token/day limits can still cause failures.

Run a single worker until a distributed provider rate limiter is implemented. SQL leases and a unique active-class slot prevent duplicate claims and overlapping class reports, but provider pacing is process-local. A crashed worker's expired lease is recoverable; graceful shutdown requeues the unfinished stage immediately. Inference dispatch is at least once across a crash: a reply lost before its checkpoint may require another provider request.

## Diagnose provider access

```bash
.venv/bin/python scripts/check_ai.py --provider groq
```

This makes one short provider request without school data. It prints credential-safe error categories, not keys or raw exception messages. It does not certify the full workflow and consumes provider allowance. Live Groq access has to be tested with your account.

## Real school onboarding

Use a separate database from fictional records:

```bash
.venv/bin/python -m pae.manage bootstrap --school "Your school" --email principal@your-school.example
```

Enter a password of at least 12 characters when prompted. In **School setup**, create teachers/classes, then import rosters with columns `learner_id,name`. Learner IDs are globally unique in the first schema. Marks and attendance import templates are in `examples/`. Imports are atomic; corrections preserve an audit trail.

## Other ways to run

**Docker Compose**

```bash
docker compose build
docker compose run --rm app python -m pae.manage bootstrap --sample
docker compose up
```

For an existing database, run `docker compose run --rm app python -m pae.manage init` after upgrading. Supply provider settings in your environment. Open http://localhost:8501. Read generated credentials privately using `docker compose exec app cat .local/bootstrap-login.txt`. PostgreSQL and credentials use persistent volumes.

**Python 3.12**

Run `python scripts/setup.py`. Without `DATABASE_URL`, SQLite in `.local/` is a development fallback. PostgreSQL is the Codespaces/Compose configuration and the intended hosted database.

**Separate processes**

```bash
.venv/bin/python -m uvicorn pae.api:app --host 0.0.0.0 --port 8000
.venv/bin/python -m pae.worker
.venv/bin/python -m streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8501
```

Use separate terminals. `python -m pae.worker --once` performs one source/delivery/monitoring control cycle and at most one job stage for operational checks.

## Collaboration and verification

Invite collaborators through repository **Settings → Collaborators**. Separate Codespaces have separate databases. Share code through branches/pull requests; use Live Share for live development. Preserve records before deleting a Codespace.

```bash
.venv/bin/python -m pytest -q
```

Tests exercise access isolation, record integrity, ADK stage execution, the actual Groq/LiteLLM connector with inference mocked, checkpoints, recovery, repair, fencing, monitoring and the interface. They do not certify live provider availability or a school rollout. GitHub Actions repeats the suite against PostgreSQL and builds/smoke-tests the Docker image.

See [architecture.md](docs/architecture.md) and [deployment.md](docs/deployment.md) for implemented boundaries and hosting requirements.
