# Automatic marked-script photo demo

Open **Photo script demo**, then upload a JPG, PNG or WebP image. The upload
automatically queues reading, demo mark capture, validation, a student performance
report, numerical review and delivery to the assigned teacher and school
principals' existing **Reports inbox**. There is no task prompt or Run agent step.

The page polls once per second and displays the source image, extracted question
marks, processing activity, report checks and final student report. Work continues
in the background worker when the page is closed. Photo history survives restarts.

The **Agent activity** workspace adds a shared plan/action/check/correction view
for both teams and a controlled error-catching demonstration. Ordinary photos
now automatically re-read disputed scores with a different processing strategy,
within the three-read budget, before stopping for unresolved evidence. See the
[visible agent demo guide](visible-agent-demo.md) for the complete loop.

## Update an existing Codespace

Stop the app with Ctrl+C, then run each command separately:

```bash
git switch main
git pull --ff-only origin main
.venv/bin/python scripts/setup.py
.venv/bin/python scripts/start.py
```

Run these after the feature is merged. Setup installs pinned dependencies and
adds the `script_photos` table without recreating accounts or existing records.
In Codespaces, setup also tries to install Tesseract automatically using
non-interactive sudo. If the environment does not permit it, setup prints the
remaining local OCR packages; a configured vision reader remains available.
Docker and newly built devcontainers include Tesseract directly.

Open port 8501, sign in, select the class and choose **Photo script demo**.
**Try a sample marked script** renders a fictional marked page and reads its actual
pixels through Tesseract. It does not inject precomputed extraction or results.
The expected result is 25/40 (62.5%), with question-level marks 8/10, 5/10, 3/10
and 9/10. Sample question topics are printed explicitly on the page.

## Photo readers

| Reader | Input and configuration |
| --- | --- |
| Automatic | Prefers the installation's configured provider, then another configured vision provider, then local OCR. `AI_PROVIDER=offline` keeps automatic reading local. |
| Local OCR | No key or external image transfer. Intended for clear printed score summaries; compares two Tesseract segmentation passes. Unstructured handwriting is not a reliable local input. |
| Groq vision | Uses `GROQ_API_KEY` and the separate `GROQ_VISION_MODEL` (default `qwen/qwen3.8-27b`). The existing text report model is not used to read images. |
| Gemini vision | Uses `GOOGLE_API_KEY`; `GEMINI_VISION_MODEL` optionally overrides the existing Gemini model. |

The reader is selected before processing and shown on the capture. Provider
failure does not silently send the photo to another external service. Network
errors, timeouts and selected server/quota errors permit at most three reading
attempts, with delayed retries. Other failures pause for attention. Provider
responses are schema-validated; raw provider exception messages are not saved.
Requests use the worker's existing per-model pacer and a separate per-photo
attempt budget. Photo work consumes provider allowance separately from class
agent reports and scheduled-monitor caps. Continue using a single worker.

Vision integrations follow [Groq image input documentation](https://console.groq.com/docs/vision)
and the [Google Gen AI SDK](https://googleapis.github.io/python-genai/).
Model availability and quality still require validation with the account's key.

## Capture and report behavior

- This feature **transcribes already awarded marks**; it does not newly mark
  unmarked answers, infer a rubric or award scores from ticks.
- Put the learner ID, awarded scores, maxima and total in a clear upright image.
  Input limits are 6 MB, 16 megapixels and one image per submission. Cropped or
  incomplete scripts need attention if visible question marks do not explain the total.
- Demo marks stay with the uploaded photo. Official `marks` records are unchanged.
  Clear printed learner IDs can match the class roster for display; unmatched IDs
  receive an explicit unlinked-identity limitation, without joining other classes'
  records or histories.
- Question ranges, duplicate labels, overlapping parent/subquestion rows, missing
  scores, uncertainty flags and written-total reconciliation are checked before a
  report is built. Unreadable marks remain unknown, never silently zero.
- The report contains the script percentage, per-question and per-topic results,
  strengths at or above a stated 70% descriptive threshold, support topics below
  the class's configured support-review threshold, follow-up suggestions and
  limitations. It makes no diagnosis or official promotion decision.
- A second report-review phase recomputes totals and percentage from the saved
  transcription and compares question rows before delivery. Passing this review
  means arithmetic/report consistency; it does not certify every OCR digit against
  the original paper. Two agreeing OCR passes are not an accuracy guarantee.
- A page containing only a readable awarded total can produce an overall report,
  with an explicit absence of question-level findings. No topic is invented.
- Conflicting totals or unreadable readings trigger bounded re-reading before an
  unresolved case pauses. Staff may upload
  a clearer photo, select another available reader, or correct the transcription
  with a reason. The original extraction and before/after audit remain available;
  validation and reporting then resume automatically.
- Re-uploading the same normalized image with the same reader returns its saved
  submission; polling, refreshes and worker replays do not create duplicate inbox
  reports. Email uses the existing opt-in SMTP outbox, with the same delivery
  semantics described in the reliability guide.

Every phase uses an owner-fenced lease. After a crash, an expired lease can be
reclaimed. Completed reading is reused; an interrupted, uncheckpointed provider
request may repeat within the persisted three-attempt budget. Source images,
transcriptions and reports require the same database access, retention and backup
controls as the other school data; they are only served through authenticated,
class-scoped endpoints. Image metadata is stripped during normalization.

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke.py
```

The suite includes actual rendered-page OCR through both sample and upload
endpoints, numerical report review, bad totals, unreadable/duplicate scores,
access isolation, deduplication, lease fencing/recovery, provider errors,
audited correction and Streamlit UI tests. The UI upload test injects the file
chooser's result because Streamlit AppTest has no file-chooser driver; all HTTP,
OCR, validation and reporting logic thereafter runs normally.

The process smoke check starts the actual API, worker and UI, reads a rendered
photo without an API key, and verifies 62.5% and teacher/principal delivery. Live
handwritten-image inference uses account-specific external providers and is not
certified by these offline tests. Reports describe the one visible script;
longitudinal learner progress requires additional verified assessments.
