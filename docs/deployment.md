# Deployment plan

Start and validate the repository in Codespaces first. No cloud resources are created automatically.

The Docker image contains both the API (8000) and Streamlit (8501). Local Compose binds ports only to localhost and supplies PostgreSQL on an internal network. The database password in Compose is exclusively a local-development setting; a hosted database needs its own managed credentials.

For a hosted system, use separate Cloud Run services from this image:

- API: override command with `python -m uvicorn pae.api:app --host 0.0.0.0 --port 8080`.
- UI: override command with `python -m streamlit run ui/app.py --server.address 0.0.0.0 --server.port 8080 --browser.gatherUsageStats false`.

Set the same `AI_TIMEOUT_SECONDS` on both services (default 300 seconds) and configure the hosting platform and any intermediary request deadlines to exceed the workflow limit plus the UI's 30-second response allowance. The synchronous workflow remains temporary until durable jobs and polling are implemented.

Set the UI's `PAE_API_URL` to the backend HTTPS URL. The backend enforces application session authentication and class permissions on all school-data routes. Configure platform ingress/IAM deliberately: an IAM-private backend additionally requires authenticated service-to-service requests, which the current UI client does not yet implement. Do not assume deploying the image establishes that connection.

Supply `DATABASE_URL` and `GOOGLE_API_KEY` through managed secrets. Use persistent PostgreSQL instead of the SQLite fallback. Initialise the database in a controlled one-off job (`python -m pae.manage init`), then provision the first principal via the documented management command. Do not seed fictional accounts in the real school environment. Do not use local container files as durable hosted storage.

Before a school rollout, complete the remaining migrations, durable jobs, backup/restore, account recovery, ingress/TLS review, retention and load tests listed in architecture.md. The cloud deployment and live Gemini calls require the user's accounts and have not been verified by the offline test suite. Free allowances depend on usage and account eligibility; managed PostgreSQL is a separate cost/provider decision.

The current AI pacing gate is local to one API process. Keep a single API worker
and replica for evaluation. Before scaling, introduce a shared limiter and daily
usage controls that account for every worker using the provider project. Lower
`AI_REQUESTS_PER_MINUTE` to match the project's actual allowance; it does not
increase provider quotas or coordinate other applications using the project.
