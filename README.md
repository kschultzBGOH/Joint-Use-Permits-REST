# Joint Use Permits REST

REST API behind the Joint Use Permits Experience Builder widget's "Create
New Permit" flow. Wraps PoleScan's PDF intake/discovery pipeline and the
ArcGIS API for Python.

## Status: first pass, untested against a live PoleScan/Portal environment

This has not been run yet. It's built directly against PoleScan's actual
`main.py` CLI and `pole_discovery.json` output format (verified by reading
that code), and against the exact field names in
`Joint-Use-Permits/scripts/create_layers.py`. See "Open items" below for
what still needs verifying end-to-end.

## What it does

1. `POST /jobs` (multipart, field `file`) -- accepts a plan set PDF, saves
   it, and starts background processing. Returns `{"jobId": "..."}`
   immediately (202).
2. In the background: runs PoleScan's `main.py <pdf>` as a subprocess,
   recovers PoleScan's own internal job ID from its logged output (`Job
   ID: <id>`), and reads that job's `results/pole_discovery.json`.
3. Derives a work-area polygon by buffering a multipoint of the discovered
   poles' catalog coordinates (`WORK_AREA_BUFFER_FEET`, default 50ft) --
   this works for any pole count, including 1 or 2, without needing a
   convex hull.
4. Creates the permit in `JointUsePermits_WorkAreas` with a
   system-generated `PERMIT_NUMBER` (sequential per year, e.g.
   `1338-2026`), and one point per discovered pole in
   `JointUsePermits_Poles`, linked via `PERMIT_GLOBALID`. Every created
   pole is marked `POLE_OWNER = "City"` -- PoleScan only matches against
   the city's own pole catalog, so it has no way to detect foreign-owned
   poles; that count stays a manual field on the permit form.
5. `GET /jobs/{jobId}` -- poll for status. Returns `queued`, `processing`,
   `completed` (with the new permit's `objectId`/`globalId`/
   `permitNumber`/`poleCount`), or `failed` (with an error message).

## Running it

Requires Python in an environment with the ArcGIS API for Python
(`arcgis`) installed, on the same machine as a working PoleScan checkout
(CUDA/PyTorch/Qwen/ArcPy already set up per PoleScan's own
`GIS_OUTPUT_SETUP.md`).

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in POLESCAN_PROJECT_DIR etc.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Important:** if PoleScan's own `.env` has `GIS_OUTPUT_TARGETS` set,
running `main.py` will *also* write discovered poles to `PoleScan_Poles`
as a side effect -- that's a separate dataset from
`JointUsePermits_Poles`. Leave `GIS_OUTPUT_TARGETS` unset in whatever
environment this service invokes `main.py` in, unless writing to both is
genuinely wanted.

## File layout

```text
app/
├── main.py               FastAPI app: POST /jobs, GET /jobs/{id}
├── config.py              environment-driven settings (see .env.example)
├── job_store.py           in-memory job tracking (single-process only)
├── polescan_pipeline.py    subprocess wrapper around PoleScan's main.py
├── permit_creation.py      geometry, permit numbering, feature creation
└── gis_connection.py       cached ArcGIS Portal connection
```

## Open items / things to verify before this is real

1. **Never run end-to-end.** No live PoleScan environment or Portal
   connection was available while building this -- verify the whole path
   (upload -> discovery -> permit + pole creation) against a real plan
   set before relying on it.
2. **Job-ID recovery is log-scraping.** `polescan_pipeline.py` regexes
   PoleScan's `Job ID: <id>` log line out of captured stdout/stderr
   because `main.py` doesn't accept a caller-supplied job ID or print one
   in a structured way. If PoleScan's logging format ever changes, this
   breaks. A more robust fix would be a small change to PoleScan's
   `main.py` to print the job ID (or write a `job_id.txt`) in a fixed,
   parseable location.
3. **Permit numbering isn't coordinated with the original Joint Use
   Permits layer** (`cad924092c44404b825dec2eef80d604`) -- it's scoped
   only to `JointUsePermits_WorkAreas`' own `PERMIT_NUMBER` values. If
   permit numbers need to be unique across both layers, this needs a
   shared counter.
4. **Single-process, in-memory job store.** Fine for one internal tool
   instance; would need a shared store (database, Redis) behind a load
   balancer or multiple workers.
5. **`ALLOWED_ORIGINS` defaults to `*`.** Lock this down to the actual
   Experience Builder app's origin before this is reachable from
   anywhere untrusted.
6. **Layer index 0 assumed** for both hosted feature services, matching
   `create_layers.py` and the `PoleScan_Poles` convention.
7. **Uploaded PDFs aren't cleaned up.** They accumulate in `UPLOAD_DIR`;
   add retention/cleanup if that matters.
