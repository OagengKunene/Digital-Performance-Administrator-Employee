# Digital Performance Administrator Employee

A deployable school application under active development. The first working foundation provides staff login, class-scoped records, validated assessment and attendance imports, descriptive performance evidence, intervention tracking and a live Google ADK reporting workflow.

The full employee also includes curriculum progress, rubric assessment, teacher-absence materials, scheduled reporting and institutional oversight. See [architecture and delivery status](docs/architecture.md) for remaining work. This version is not yet ready for an unattended production school rollout.

## Start in GitHub Codespaces

No local installations are needed for this browser workflow.

1. Open **Code → Codespaces → Create codespace on main**.
2. If your Codespace already exists, save/push your work, pull `main`, then open the command palette and choose **Codespaces: Rebuild Container** (or **Dev Containers: Rebuild Container**). Rebuilding installs the pinned Python environment and starts PostgreSQL.
3. Wait for the post-create setup command to finish. It initialises an empty schema and does not start the application or create accounts.
4. For fictional evaluation records, run:

   ```bash
   .venv/bin/python -m pae.manage bootstrap --sample
   ```

5. Open `.local/bootstrap-login.txt` in your private editor to see the randomly generated principal and teacher passwords. This file is gitignored.
6. Start the application:

   ```bash
   .venv/bin/python scripts/start.py
   ```

7. Open **Ports → 8501 → Open in Browser** and sign in. Port **8000** hosts the authenticated API and `/docs`. Keep forwarded ports private while setting up.

The Grade 10A sample has four expected marks, three marked scripts and one missing mark. Its marked-record average is **75%**, with coverage reported separately as **3 / 4**. A true zero contributes to the average; a missing mark does not.

Try `examples/marks.csv` and `examples/attendance.csv` in **Import records**. Imports are atomic. Duplicates raise an error instead of overwriting existing records. Corrections require a reason and preserve the previous value in the audit trail.

Sample usernames: `principal@example.test`, `teacher@example.test`, `other.teacher@example.test`. Passwords are generated locally. The principal sees both sample classes, while each teacher sees only their assigned class.

### Enable live agents

Core administration works without an AI key. Live agent reports require one.

1. Open your GitHub account **Settings → Codespaces → Secrets → New secret**.
2. Add `GOOGLE_API_KEY` and grant this repository access. Obtain a key from [Google AI Studio](https://aistudio.google.com/app/apikey).
3. Stop and restart the Codespace, then start the application again.
4. Open **Agent reports**, enter an objective and choose **Run agent team**.

Set `GEMINI_MODEL` to a model your account can access. The default is `gemini-3.6-flash`. A successful short generation test does not guarantee that every multi-agent run will finish within its deadline. Provider quotas still apply. Missing credentials never trigger fabricated agent output. Failed, unresolved and metric-verified runs are distinct statuses.

Report runs have a default 300-second wall-clock limit and up to 24 model calls
per invocation, with at most two repair invocations. The dashboard waits an extra
30 seconds for the server's result; ordinary API requests retain shorter limits.
Set `AI_TIMEOUT_SECONDS` equally for API and UI when they run separately.
The allowed range is 1–600 seconds. Timeouts cancel the in-process workflow and
retain completed tool events, elapsed times and the last completed tool name.
They do not publish a partial report. Slow or repeated failures still need
investigation.

### Troubleshoot live agents

In a second terminal, run:

```bash
.venv/bin/python scripts/check_ai.py
```

This checks whether the key is loaded, constructs the ADK team and makes one short
Gemini request without school data. It prints only safe diagnostic metadata, not
your key or raw exception messages. It does not prove the full multi-agent report
will succeed. Failed report runs now include an error category, exception type
and provider HTTP code when available. A quota error does not prove that a new
billing plan is required; check your project's actual limits first.

### Working with your teammate

Invite them through **Settings → Collaborators**. Each teammate can open a Codespace, use feature branches and share changes through commits and pull requests. Each Codespace has its own database; pushing code does not synchronise school records. For a shared live view, use Live Share or the eventual hosted application. Save and push work before deleting a Codespace. Database volumes persist across restarts but are not backups against Codespace deletion.

## Real school onboarding

Use a separate database from fictional evaluation records. Create the first principal:

```bash
.venv/bin/python -m pae.manage bootstrap --school "Your school" --email principal@your-school.example
```

The terminal securely prompts for a password of at least 12 characters. In the principal account, open **School setup** to create teachers and classes. Import a learner roster with columns `learner_id,name`. Learner IDs must be globally unique in this first schema. Complete the hosted-readiness work in [architecture.md](docs/architecture.md) before a school rollout.

## Other ways to run

### Docker Compose

```bash
docker compose build
docker compose run --rm app python -m pae.manage bootstrap --sample
docker compose up
```

Open http://localhost:8501. Read generated passwords with `docker compose exec app cat .local/bootstrap-login.txt`. PostgreSQL and the local credential file have separate persistent volumes. The database port is not published. The example database password is for local development only.

### Python without containers

With Python 3.12, run `python scripts/setup.py`, then the bootstrap and start commands above. Without `DATABASE_URL`, the application uses a SQLite file in `.local/` for development. Codespaces and Compose explicitly use PostgreSQL. A hosted application needs external durable storage.

## Verification

```bash
.venv/bin/python -m pytest -q
```

Tests cover authentication, school/class isolation, onboarding, atomic imports, duplicates, missing versus zero scores, attendance states, audited corrections, support follow-up, actual ADK team construction and bounded repair logic. Retry tests use an explicitly labelled SDK test double. They do not certify live Gemini responses. GitHub Actions repeats the suite against a disposable PostgreSQL database named `pae_test`.

The dependency lock records versions resolved for Python 3.12. Review and retest dependency upgrades.

## Project map

- `.devcontainer/`: browser workspace and PostgreSQL.
- `pae/api.py`: authenticated school endpoints.
- `pae/services.py`: imports and deterministic analytics.
- `pae/agents.py`: coordinator and specialist ADK tools.
- `pae/db.py`: records, interventions, audit history and saved runs.
- `ui/app.py`: staff interface.
- `scripts/`: setup and service startup.
- `tests/`: access, integrity and workflow tests.
- `docs/`: architecture, deployment plan and delivery backlog.

## Current boundaries

The descriptive average is not an official term/promotion mark. Assessment calendars, official weighting rules, curriculum evidence, autonomous teaching cover, notification delivery and validated predictive models remain future deliveries. Agent activity appears after a run finishes. Durable background jobs, in-flight resume, account recovery and versioned production migrations are not implemented yet.

No cloud deployment is created automatically. See [deployment.md](docs/deployment.md) for the hosting architecture and readiness requirements. The repository provides an operational foundation and continues toward the complete agreed employee role.
