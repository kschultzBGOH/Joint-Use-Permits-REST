# Joint Use Permits REST

REST API behind the Joint Use Permits Experience Builder widget's "Create
New Permit" flow. This service is fully self-contained: it runs its own
PDF intake and pole-discovery pipeline (native PDF text first, then the
Qwen vision-language model for pages that need it) and creates features
via the ArcGIS API for Python, pointed at the pole reference database and
Qwen model files on disk.

## Status: first pass, untested against a real model/database/Portal

Nothing in this repo has been run yet. See "Open items" below for what
still needs verifying, especially the Qwen model loading/prompting code in
`vision_model.py`, which hasn't been exercised against a real checkpoint.

## What it does

1. `POST /jobs` (multipart, field `file`) -- accepts a plan set PDF, saves
   it, and starts background processing. Returns `{"jobId": "..."}`
   immediately (202).
2. In the background, `app/discovery/pipeline.py` runs:
   - **Native-text pass** (`native_text.py`): extracts each page's
     embedded text (PyMuPDF) and checks every alphanumeric token against
     the pole catalog for an exact match. Free, no GPU.
   - **Visual pass** (`visual_discovery.py` + `vision_model.py`): for any
     page where the native-text pass found nothing, renders that page to
     an image and prompts the local Qwen vision-language model to read
     every visible pole ID label on it, then matches each reading against
     the pole catalog (with a zero/O correction fallback -- see
     `pole_catalog.py`).
   - Results are deduplicated by pole ID across both passes.
3. Derives a work-area polygon by buffering a multipoint of the discovered
   poles' catalog coordinates (`WORK_AREA_BUFFER_FEET`, default 50ft) --
   works for any pole count, including 1 or 2, without needing a convex
   hull.
4. Creates the permit in `JointUsePermits_WorkAreas` with a
   system-generated `PERMIT_NUMBER` (sequential per year, e.g.
   `1338-2026`), and one point per discovered pole in
   `JointUsePermits_Poles`, linked via `PERMIT_GLOBALID`. Every created
   pole is marked `POLE_OWNER = "City"` -- this pipeline only matches
   against the city's own pole catalog, so it has no way to detect
   foreign-owned poles; that count stays a manual field on the permit
   form.
5. `GET /jobs/{jobId}` -- poll for status. Returns `queued`, `processing`,
   `completed` (with the new permit's `objectId`/`globalId`/
   `permitNumber`/`poleCount`), or `failed` (with an error message).

## Running it

```bash
pip install -r requirements.txt   # install torch separately first -- see requirements.txt
cp .env.example .env               # fill in POLE_DB_PATH, QWEN_MODEL_DIR, etc.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`POLE_DB_PATH` and `QWEN_MODEL_DIR` should point at the same underlying
pole reference database and Qwen model directory already sitting on disk
(wherever they currently live) -- this service reads those files directly
with its own code, it just doesn't import any other project's code to do
it.

## File layout

```text
app/
├── main.py                      FastAPI app: POST /jobs, GET /jobs/{id}
├── config.py                     environment-driven settings (see .env.example)
├── job_store.py                  in-memory job tracking (single-process only)
├── permit_creation.py            geometry, permit numbering, feature creation
├── gis_connection.py              cached ArcGIS Portal connection
└── discovery/
    ├── pole_catalog.py            loads the authoritative pole reference (SQLite)
    ├── pdf_pages.py                PDF open/inspect/render (PyMuPDF)
    ├── native_text.py             fast pass: embedded-text -> catalog match
    ├── vision_model.py             loads Qwen, prompts it, parses its output
    ├── visual_discovery.py         renders pages, runs the vision pass
    └── pipeline.py                 orchestrates both passes, dedupes results
```

## Open items / things to verify before this is real

1. **Qwen model loading/prompting is unverified.** `vision_model.py` uses
   the standard `transformers` pattern for Qwen-VL family models
   (`AutoModelForImageTextToText`, chat-template prompting, JSON-array
   response parsing), but the exact model class and generation kwargs can
   vary by checkpoint/transformers version. Run it against the real
   `QWEN_MODEL_DIR` and fix whatever doesn't match.
2. **Never run end-to-end.** No real pole database, Qwen checkpoint, or
   Portal connection was available while building this.
3. **Native-text matching is broad.** `native_text.py` checks every
   2-12 character alphanumeric token against the catalog -- a common word
   that happens to collide with a real pole ID would be accepted as a
   false positive. A more selective pattern (e.g. requiring a "POLE"
   prefix nearby) would cut down on that risk at the cost of missing
   labels that don't follow that convention.
4. **Permit numbering isn't coordinated with the original Joint Use
   Permits layer** (`cad924092c44404b825dec2eef80d604`) -- it's scoped
   only to `JointUsePermits_WorkAreas`' own `PERMIT_NUMBER` values.
5. **Vision model reloads on every job.** Model load time is likely a
   meaningful chunk of each job's runtime -- keeping it loaded
   persistently between jobs would be a real perf win, at the cost of
   holding GPU memory continuously.
6. **Single-process, in-memory job store.** Fine for one internal tool
   instance; would need a shared store (database, Redis) behind a load
   balancer or multiple workers.
7. **`ALLOWED_ORIGINS` defaults to `*`.** Lock this down to the actual
   Experience Builder app's origin before this is reachable from
   anywhere untrusted.
8. **Layer index 0 assumed** for both hosted feature services, matching
   `create_layers.py`.
9. **Uploaded PDFs aren't cleaned up.** They accumulate in `UPLOAD_DIR`;
   add retention/cleanup if that matters.
