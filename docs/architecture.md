# Agentic architecture and delivery status

The goal is an autonomous Digital Performance Administrator Employee. The implemented autonomous cycle observes authorised school records, identifies actionable evidence gaps, creates in-app follow-ups and can initiate goal-driven investigations under a principal's policy.

## Control and data flow

```mermaid
flowchart TD
  Records["Class records and schedules"] --> Monitor["Policy-controlled monitor"]
  Staff["Teacher or principal goal"] --> Queue["Durable SQL job queue"]
  Monitor --> Follow["Assigned teacher follow-ups"]
  Monitor --> Queue
  Queue --> Planner["ADK coordinator: plan and select specialists"]
  Planner --> Analyst["Performance analyst: optional"]
  Planner --> Support["Intervention planner: optional"]
  Analyst --> Saved["Saved stage outputs"]
  Support --> Saved
  Planner --> Saved
  Saved --> Reporter["Reporting agent"]
  Reporter --> Reviewer["Independent quality reviewer"]
  Reviewer -->|Issues| Reporter
  Reviewer --> Gate["Code checks: metrics, review and source freshness"]
  Gate -->|Pass| Report["Saved staff report"]
  Queue --> Recovery["Retry, configured fallback or resume"]
  Recovery --> Saved
```

The ADK coordinator generates a typed plan from the goal and snapshot. Its decisions control whether the performance analyst and intervention planner run. The reporting agent and independent reviewer are mandatory. Specialist outputs and prior review feedback are passed explicitly to later agents. Host code executes the selected stages and constrains the available actions.

This preserves goal interpretation, selective delegation, evidence-based reasoning, evaluation and feedback. It is a bounded autonomous workflow, not a claim that the full school employee is complete. The observer and action permissions are code-enforced; an LLM does not decide its own database access.

## Durable execution

`AgentRun` preserves report history. New additive tables store jobs, immutable source snapshots, typed stage checkpoints, dispatched-attempt counts, leases, monitoring policies, follow-ups, worker heartbeat and daily automatic-initiation budgets.

The API queues work and returns 202 without waiting for the model. A separate worker claims one stage with an atomic compare-and-set lease. Checkpoint writes require the matching unexpired lease. Cancellation clears the lease; stale workers cannot publish over a newer owner. A unique nullable active-class slot prevents simultaneous active reports for a class.

Completed stages survive process restarts. Provider errors affect the unfinished stage. Bounded transient retries are delayed in the database; quota/authentication failures pause. An explicitly configured backup can handle a failed stage. Manual resume preserves successful predecessors and the total request-attempt budget. Source records are checked again before publication; a changed snapshot requires a new report.

This is application-level checkpointing around short ADK stage invocations. ADK conversational sessions are temporary inside one stage. ADK's native whole-invocation Resume feature is not enabled or required by this implementation. Inference is at least once across an abrupt crash; no exactly-once provider billing guarantee is made.

Default full-path cost is five successful stage dispatches, with additional attempts for provider failures and repairs. The shared budget defaults to ten dispatched attempts across the entire report, including manual resumes. At most two report/review repair cycles are allowed. Each stage permits one model inference and has its own deadline.

## Autonomous monitoring and action

Policies are principal-controlled and off by default. Enabled policies run on an interval and can wake after authorised record changes. They produce follow-ups for missing captured marks, source-defined learner support flags, supplied absence records and overdue recorded interventions. Follow-ups are assigned to the current teacher, close when the recorded issue disappears, and become visible as escalated to the principal when overdue. Staff resolutions require recorded evidence.

Optional AI investigations start when the evidence hash changes and the per-class and installation-wide daily caps permit. The installation budget is reserved atomically in SQL. These are initiation caps per UTC day, not provider usage ledgers. Cancelled or failed automatic work conservatively consumes its initiation slot. Disabling automatic AI prevents queued monitor jobs from dispatching new requests.

Follow-up assignment and escalation occur inside this application. No email, SMS or external message is sent. The worker must remain running; restarting a Codespace does not create an always-on hosted employee.

## Providers and review

ADK stages use Gemini directly or Groq through the pinned LiteLLM connector. No key is stored in job state. Fallback is explicit, provider errors use credential-safe metadata, and raw model thought parts are excluded. A compact snapshot discloses omitted individual/topic/intervention details while retaining full-class aggregate metrics.

Each worker has a per-model request gate; Gemini starts with a 60-second cooldown. SDK automatic retries are disabled. This does not coordinate external applications, provider daily/token quotas or multiple worker replicas. Distributed provider rate limiting is required before scaling workers.

The reporting agent's structured metrics and the independent reviewer's checked metrics must match the immutable source. Approval with unresolved issues is blocked. `verified_metrics` means these checks passed; it does not certify every narrative claim, official academic decisions or educational effectiveness. Intermediate model outputs remain labelled drafts. Official marks change only through the authenticated, audited staff correction route.

A deterministic statistical report is available without AI and is explicitly labelled as having no AI interpretation.

## Remaining full-employee work

- Curriculum/assessment calendars, official weighting and grade/subject rules.
- Script extraction, rubric-based assessment and teacher moderation.
- Teaching-continuity planning from approved resources and reviewed cover materials.
- Notification outbox, delivery tracking and approved communication channels.
- Staff account recovery, fuller administration audits and formal versioned migrations.
- Distributed rate/usage controls, backup/restore, hosting, retention, load tests and school pilot evaluation.

SQL `create_all` adds the new tables without altering old tables or recreating accounts. Formal production migration/version management remains necessary.

The current descriptive mean is earned / possible across marked topic records, not an official term/promotion mark. Expected coverage is roster size times assessment-topic pairs already represented in the database. Entirely absent assessments need the future assessment calendar. Attendance summaries cover supplied records, not the complete school calendar.

Sources:
- https://adk.dev/agents/models/litellm/
- https://adk.dev/runtime/resume/
- https://console.groq.com/docs/models
- https://docs.github.com/en/codespaces


## Offline provider

`pae/offline_engine.py` registers provider `offline` (model `deterministic-rules-v1`). `jobs.process_one` uses it instead of `agents.run_stage` when the chosen provider is `offline`, so durable jobs, stage checkpoints, `SCHEMAS` validation, `review_numbers`, the reviewer repair loop, snapshot fencing, cancellation and audit are identical to live runs. It needs no key and no network, and is not paced. Its reviewer also requires the coverage statement when marks are missing and rejects recommendations not produced by the intervention planner. `demonstrate_repair` (offline only, stored on the job and the audit entry) makes the first reporting draft omit the coverage statement so the repair loop is observable. Offline text is rule-based, not model reasoning; runs record `provider: offline`.
