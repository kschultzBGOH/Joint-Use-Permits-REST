# Joint Use Permits REST

REST API behind the Joint Use Permits Experience Builder widget's "Create
New Permit" flow. This service is fully self-contained: it runs its own
PDF intake and pole-discovery pipeline and creates features via the
ArcGIS API for Python.

## Status: first pass, untested against a real model/database/Portal

Nothing in this repo has been run yet. See "Open items" below for what
still needs verifying.

## What it does

`POST /jobs` (multipart, field `file`) accepts a plan set PDF and starts
background processing; `GET /jobs/{jobId}` polls for status. While a job
is `"queued"` or `"processing"`, the response also carries an optional
`detail` string -- a human-readable "what's happening right now" (e.g.
"Reading page 3 of 12...", "Loading the vision model..."), pushed by the
discovery pipeline's `on_progress` callback as it runs, for the widget to
show instead of a plain spinner. It's best-effort: if a pipeline stage
doesn't report progress, `detail` is just absent from that response, and
the widget falls back to its own generic status text.

Finding zero poles in the PDF doesn't fail the job. The permit is still
created -- just with no geometry yet and no poles -- so the widget's
"Define Project Scope" screen has a real permit to add poles to by hand
(search the catalog, or click poles on the linked map); the widget's own
work-area recompute already rebuilds the shape from whatever poles
actually end up on the permit, on every add/approve/reject, so there's
nothing useful to fail over at zero.

`POST /permits` (no body) creates that same kind of empty permit
directly and synchronously -- for someone with no plan set PDF to upload
at all, so they aren't forced through the upload step just to reach the
screen where poles get added by hand anyway.

Discovery (`app/discovery/`) runs in two tiers:

1. **Native-text pass** (`native_text.py`): extracts embedded PDF text
   (PyMuPDF), checks single words and adjacent word-pairs against the pole
   catalog for exact matches, and flags reference-shaped-but-unmatched
   tokens for the visual pass.
2. **Visual pass**, for pages the native-text pass didn't resolve
   (`renderer.py` + `vision_model.py` + `visual_discovery.py` +
   `candidate_resolver.py`):
   - Renders an inexpensive whole-page preview and asks the local Qwen3-VL
     model to *route* it -- does this page likely contain pole
     drawings/IDs? -- before spending time on expensive tiles.
   - Only pages the routing step flags get rendered as overlapping
     high-resolution tiles (`tile_pixels`/`overlap_ratio` configurable),
     each read by the model for candidate pole ID text.
   - Each raw reading goes through `CatalogCandidateResolver`: strips
     known plan-marker prefixes (`POLE#`, `P-`, etc.), tries scored
     letter/digit homoglyph corrections (`0`→`O`, `1`→`I`, ...) only when
     they land on a real catalog ID, and flags an ID as ambiguous rather
     than guessing when it's one digit away from more than one catalog
     entry.
   - `apply_candidate_evidence_rules` then requires an exact catalog
     match *and* model-reported completeness *and* no tile-edge clipping
     *and* minimum confidence before accepting a reading -- anything short
     of that is held for review rather than silently accepted.
3. Native and visual results are merged by pole ID (`results.py`); each
   accepted pole gets its X/Y from the catalog, never from the model.

From there:

4. Derives a work-area polygon by buffering a multipoint of the accepted
   poles' catalog coordinates (`WORK_AREA_BUFFER_FEET`, default 50ft) --
   works for any pole count, including 1 or 2.
5. Creates the permit in the WorkAreas layer with a system-generated
   permit number (sequential per year, e.g. `1338-2026`, allocated from
   the `PermitNumberSequence` table -- see "Permit numbering" below), one
   point per accepted pole in the Poles layer (linked via `permit_globalid`), and
   attaches the uploaded plan set PDF to the permit feature itself
   (`attach_plan_set` in `permit_creation.py`) -- so the source document
   is on the permit from the moment it's created, not just its derived
   geometry and poles. Every created pole is marked `pole_owner = "City"`
   -- this pipeline only matches against the city's own pole catalog, so
   it has no way to detect foreign-owned poles; that count stays a manual
   field on the permit form.

## Permit numbering

Permit numbers (`1338-2026`) are allocated from a `PermitNumberSequence`
table on the same hosted feature service as the WorkAreas/Poles layers --
a single row (`number`) holding the last number ever issued; see
`Joint-Use-Permits/scripts/create_layers.py`. Creating a permit reads that
row, adds one, writes it back, and uses the result, with the current year
appended for display (`permit_creation.py`'s `generate_permit_number`);
the read-increment-write is lock-protected against two permits being
created at nearly the same moment. The count never resets per year -- it
just keeps climbing; the year in the formatted number is whatever year it
happens to be when that permit is created, not part of the count itself.

**Before this goes live**, open the `PermitNumberSequence` table directly
in Portal and edit its one row so `number` is wherever your real/legacy
numbering scheme currently stands -- the next permit created continues
from `number + 1`, not from 1. If the table is still empty, a row is
created automatically starting from 0 (so the first permit created gets
`1-<year>`).

`PERMIT_NUMBER_SEQUENCE_TABLE_INDEX` (default `0`) is this table's index
within the service's *tables*, a separate collection from its layers --
`create_layers.py` prints the right value when it creates the service; if
you're adding this table to an already-live service, run
`Joint-Use-Permits/scripts/add_permit_number_sequence.py` instead, which
prints it too.

## Notifying a contractor by email (`app/email_service.py`)

`POST /notify-message` has no auth of its own -- it's called only by the
internal `Joint-Use-Permits` widget, right after it writes a city reply
directly to the Messages table itself (trusted Portal users, direct
`applyEdits`). This endpoint doesn't touch ArcGIS at all; it only sends
the "you have a new message" email. `{contractorEmail, permitNumber?,
body}`.

This service has no other involvement in contractor <-> city messaging.
The contractor-facing side (`Joint-Use-External`) has its own PHP backend
(that repo's `api/`) that talks to ArcGIS directly -- a contractor has no
Portal account, and that backend is the only thing that ever sees their
access token.

`SMTP_HOST` blank disables sending entirely: a warning is logged and the
caller's request still succeeds, so this doesn't become a hard dependency
before an SMTP relay is provisioned. `CITY_NOTIFICATION_EMAIL` is unused
by this service (Joint-Use-External's PHP backend has its own copy of
this setting for the contractor -> city direction).

## Running it

```bash
pip install -r requirements.txt   # install torch separately first -- see requirements.txt
cp .env.example .env               # fill in POLE_DB_PATH, QWEN_MODEL_DIR, etc.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

CUDA is required for the visual pass -- `vision_model.py` refuses to load
the model on CPU by default (`require_cuda=True`). `QWEN_MODEL_DIR` must
contain `config.json`, `preprocessor_config.json`, `tokenizer_config.json`,
and its `.safetensors` weights, with `model_type` `qwen3_vl`.

## File layout

```text
app/
├── main.py                       FastAPI app: POST /jobs, GET /jobs/{id}, /notify-message
├── config.py                      environment-driven settings (see .env.example)
├── job_store.py                   in-memory job tracking (single-process only)
├── permit_creation.py             work-area geometry, permit numbering, feature creation
├── email_service.py               best-effort "new message" notification email (smtplib)
├── gis_connection.py               cached ArcGIS Portal connection
└── discovery/
    ├── pole_catalog.py             loads/indexes the authoritative pole reference (SQLite)
    ├── pdf_inspection.py            PDF validation + per-page content classification
    ├── native_text.py              fast pass: embedded-text -> catalog match
    ├── renderer.py                  page selection + preview/tile rendering (PyMuPDF)
    ├── vision_model.py              loads/unloads the local Qwen3-VL checkpoint
    ├── candidate_resolver.py        scored catalog resolution (homoglyphs, prefixes, ambiguity)
    ├── visual_discovery.py          orchestrates the preview-route -> tile-read visual pass
    ├── results.py                    merges both passes into the final accepted-pole list
    └── pipeline.py                   orchestrates the whole thing per job
```

## Open items / things to verify before this is real

1. **Never run end-to-end.** No real pole database, Qwen checkpoint, or
   Portal connection was available while building this.
2. **Permit numbering isn't coordinated with the original Joint Use
   Permits layer** (`cad924092c44404b825dec2eef80d604`) -- it's scoped
   only to the `PermitNumberSequence` table on this service (see "Permit
   numbering" above), which can be manually seeded to match wherever that
   original numbering left off.
3. **Vision model reloads on every job.** Model load time is likely a
   meaningful chunk of each job's runtime -- keeping it loaded
   persistently between jobs would be a real perf win, at the cost of
   holding GPU memory continuously.
4. **Single-process, in-memory job store.** Fine for one internal tool
   instance; would need a shared store (database, Redis) behind a load
   balancer or multiple workers.
5. **`ALLOWED_ORIGINS` defaults to `*`.** Lock this down to the actual
   Experience Builder app's origin before this is reachable from
   anywhere untrusted.
6. **Layer index 0 assumed** for both hosted feature services, matching
   `create_layers.py`.
7. **Uploaded PDFs and rendered page images aren't cleaned up.** They
   accumulate under `UPLOAD_DIR/<jobId>/`; add retention/cleanup if that
   matters. `visual_discovery.py` also writes a `visual_discovery.json`
   evidence file per job into that same tree, useful for debugging a
   run after the fact.
8. **Review candidates aren't surfaced anywhere.** The resolver and
   evidence rules correctly hold ambiguous/incomplete readings back from
   being accepted, but nothing currently exposes them for a human to
   review and confirm -- they're just excluded from the final pole list.
9. **Email untested against a real SMTP relay.** `email_service.py`
   hasn't been run against a real server -- confirm `SMTP_HOST` etc. are
   correct and `POST /notify-message` actually lands an email before
   depending on it.
