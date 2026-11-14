# Visible agent planning, actions and self-correction

## Run the complete demo

1. Open **Agent activity** in the application sidebar.
2. Choose the photo reader. Local OCR needs no API key; configured vision readers
   use their existing accounts. Local OCR must be installed for the keyless path.
3. Click **Run the complete colleague demo** once.
4. Watch the **Photo agent team** and **Class agent team** tabs. Both run in the
   worker and continue if the page is closed. The view refreshes once per second.

The photo demonstration is explicitly labelled: the system renders a fictional
marked page, performs actual image reading, then deliberately alters one value
in the first transcription. For the sample, Q3 becomes 8/10 while the printed
paper still says 3/10. The resulting question sum disagrees with the printed
25/40 total. The validator blocks publication. The coordinator delegates another
read of the original image, using contrast/sharpening and, if needed, a different
layout-segmentation strategy. Only a replacement reading that passes all checks
can reach the report and its independent review.

The UI preserves every attempted transcription, failed check and strategy. It
shows initial/latest marks and the recomputed final result, normally 25/40 (62.5%).
The error is seeded for a reproducible presentation; this is not evidence of a
naturally occurring OCR error or a universal handwriting accuracy claim.
The repair actually reads the pixels again; it never subtracts an unexplained
amount from a score to make the total balance.

The class team runs the existing coordinator, analyst, intervention planner,
report writer and quality reviewer with offline rules on the current class
evidence. The coordinator may skip specialists that are not needed; skipped
roles are labelled. Where marks are missing, the controlled report demo omits
the coverage statement in its initial draft. The reviewer rejects it and the
writer repairs it before re-review. Rejected and replacement summaries remain
visible. If a class report is already active, the demo reuses its saved work
rather than creating an overlapping report.

## What is visible

| Step | Persisted evidence |
| --- | --- |
| Plan | Coordinator work plan; each role's public task/verification summary; repair delegation after a failed check. |
| Act | Actual stage/tool invocation, selected photo reader or agent provider/model, and attempt number. |
| Observe | Structured stage outputs, candidate photo marks, topic results or draft summaries. |
| Check | Range/coverage/sum tests, grounding and schema checks, independent report metrics, source-row/topic checks and reviewer issues. |
| Correct | A rejected draft sent back with feedback, a newly read image transcription, or a rebuilt report followed by another verification. |
| Retry | A separate event for temporary provider/connection failures; network recovery is distinguished from content correction. |
| Complete | Reviewed publication and transactional teacher/principal inbox delivery. |

These are public work artifacts and host execution summaries. Model private
reasoning and thought tokens are not recorded. Photo coordination/reporting are
deterministic policies and tools; only a configured vision reader uses a model.
Class reports identify offline rules versus live ADK model execution. The UI
does not present the offline demonstration as live model reasoning.

The same activity view is available inside **Photo script demo** and **Agent
reports**. **All saved agent work** includes routine uploads, manual
investigations and capture/scheduled class reports within the selected class.
Historical runs keep their saved results; plans/actions that were not recorded
at execution time are not retroactively invented.

## Genuine runtime correction and limits

Ordinary uploaded photos use the same check/re-read/re-check loop without seeded
errors. At most three total photo-reading attempts are permitted, including
provider retries and crash recovery. The first repair changes contrast/sharpness;
the second changes layout segmentation. Vision re-reading receives failed-check
feedback and the source photo, with explicit instructions to copy visible marks
instead of solving for a balancing score. It stays with the selected provider.

A changed learner ID between reads blocks automatic identity matching pending
staff evidence. A replacement reading cannot drop a previously visible written
total to evade reconciliation; a changed total needs a second matching source
read or an audited staff correction. Percentages use decimal arithmetic rounded
to two places, half up, in both the builder and the independent reviewer.
If the paper itself has a wrong total, missing pages, an unclear
score, or persistent disagreement, the application stops for review. It does not
rewrite the source total, silently fill blanks or alter official class marks.

The report reviewer separately recomputes totals, percentage and topic breakdown;
compares source rows; and checks strengths/support classifications against the
stated thresholds. A rejected report can be rebuilt twice from the checked
transcription, then must pass review before delivery. The existing class agent
loop retains its total request budget and two-review-repair limit.

Plans, attempts, candidate readings, feedback and repair counts are stored in
existing SQL JSON fields; this upgrade adds no database tables or columns.
Owner-fenced writes protect both event checkpoints and publication. An expired
worker lease can resume unfinished work, and duplicate inbox delivery remains
prevented by the existing notification keys. No already completed source read is
repeated solely because the UI reloads.

## How the supplied resources informed this change

Reviewed on 14 November 2026. The list is a set of options, not a requirement to
replace every existing layer or claim integrations that were not implemented.

| Resource | Application to this repository |
| --- | --- |
| [Stitch](https://stitch.google.com/) | Design reference retained from the supplied list. No generated Stitch design or account integration is claimed. |
| [shadcn/ui](https://ui.shadcn.com/) | A component option for a future frontend migration. This change keeps the existing Streamlit interface. |
| [AI SDK](https://ai-sdk.dev/docs/introduction) | A future TypeScript streaming UI option. Current live updates poll durable public execution events once per second; no AI SDK/token-stream integration is claimed. |
| [FastAPI](https://fastapi.tiangolo.com/tutorial/) | Existing API retained; adds a scoped execution catalog and complete-demo endpoint. |
| [Supabase AI & Vectors](https://supabase.com/docs/guides/ai) | Relevant to future semantic memory. Current resumable work uses the existing PostgreSQL/SQLite records; this change does not create Supabase resources or embeddings. |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Persistence, feedback loops and inspection are relevant design concepts. Existing SQL checkpoints and ADK execution are retained; LangGraph is not added as a second orchestrator. |
| [Building effective agents](https://www.anthropic.com/research/building-effective-agents) | Evaluator/optimizer feedback, environment/tool evidence and bounded stopping inform the implemented check/repair/check loop. The photo implementation uses deterministic checks and re-reading tools. |
| [Vercel](https://vercel.com/docs/getting-started-with-vercel) | A future frontend-hosting option if the UI is migrated. No Vercel deployment is created. |
| [Render Background Workers](https://render.com/docs/background-workers) | Continuous background execution fits the existing `python -m pae.worker` process. Hosting still requires account-specific deployment and database setup. |
| [Cloud Run quickstarts](https://docs.cloud.google.com/run/docs/quickstarts) | Container and worker-pool deployment references. Request-driven web serving alone is not configured as an always-on worker here. No cloud infrastructure is provisioned. |

## Validation

Run the full suite with `.venv/bin/python -m pytest -q`, and the real-process demo
with `.venv/bin/python scripts/smoke.py`. Checks include the controlled sample's
actual OCR/re-reading, unseeded bad-reading recovery, persistent contradictions
stopping without guessed marks, bounded report repairs, class-scoped history,
each class agent's public steps, and both teams through the Streamlit interface.
UI checks use AppTest; live handwritten vision and hosted deployment still need
validation with the intended account and school data.
