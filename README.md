# Joint Use Permits REST

REST API behind the Joint Use Permits Experience Builder widget's "Create
New Permit" flow. Runs PoleScan's PDF intake/discovery pipeline in-process
and creates features via the ArcGIS API for Python.

## Status: first pass, untested against a live PoleScan/Portal environment

This has not been run yet. It's built directly against PoleScan's actual
pipeline modules and `pole_discovery.json` output shape (verified by
reading that code), and against the exact field names in
`Joint-Use-Permits/scripts/create_layers.py`. See "Open items" below for
what still needs verifying end-to-end.

## What it does

1. `POST /jobs` (multipart, field `file`) -- accepts a plan set PDF, saves
   it, and starts background processing. Returns `{"jobId": "..."}`
   immediately (202).
2. In the background: imports PoleScan's pipeline modules directly (from
   wherever `POLESCAN_PROJECT_DIR` points) and runs the same steps
   PoleScan's `main.py` runs -- intake, native-text discovery, visual
   discovery -- in this same process, skipping PoleScan's own GIS output
   step entirely. See "Why in-process, not a subprocess" below.
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

## Why in-process, not a subprocess

The first version of this shelled out to `python main.py <pdf>` as a
subprocess. That turned out to be the wrong call: PoleScan was built as a
one-off proof-of-concept CLI, not something meant to be invoked as an
external tool from a separate service/environment -- it needed a second
Python interpreter with a matching dependency set, a hardcoded project
path, and (since `main.py` generates its own job ID internally and doesn't
print anything structured) recovering that ID by regexing a log line back
out of captured output. All fragile, all avoidable.

Instead, `polescan_pipeline.py` puts `POLESCAN_PROJECT_DIR` on `sys.path`
and imports PoleScan's actual pipeline functions (`prepare_plan_job`,
`discover_native_text`, `prepare_visual_images`, `discover_visual_poles`,
`write_pole_discovery_result`, `load_vision_model`/`unload_vision_model`)
directly, running the same sequence `main.py` does. This means **this
service now runs inside PoleScan's own Python environment** -- the one
with `torch`, `transformers`, `arcgis`, `numpy`, `pymupdf`, and `pillow`
already installed (see PoleScan's `INSTALL.txt`) -- with
`fastapi`/`uvicorn`/`python-multipart` additionally installed into it.
There's no longer a "this service's environment" separate from
"PoleScan's environment"; they're the same one. `arcpy` is not required
here -- it's only used by `polescan.output`, which this service never
imports (Joint Use Permits creates its own features in `permit_creation.py`
instead of going through PoleScan's `write_gis_outputs`/`PoleScan_Poles`).

Runs are serialized with a lock (`_pipeline_lock`) -- `main.py` was never
built to run two jobs concurrently against one shared GPU/vision model, so
this service doesn't try to either.

## Running it

Install this repo's dependencies into PoleScan's own Anaconda environment
(the one already running PoleScan's `main.py` successfully), then run
uvicorn from there:

```bash
conda activate <PoleScan's environment name>
pip install -r requirements.txt
cp .env.example .env   # fill in POLESCAN_PROJECT_DIR etc.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## File layout

```text
app/
├── main.py               FastAPI app: POST /jobs, GET /jobs/{id}
├── config.py              environment-driven settings (see .env.example)
├── job_store.py           in-memory job tracking (single-process only)
├── polescan_pipeline.py    imports and runs PoleScan's pipeline in-process
├── permit_creation.py      geometry, permit numbering, feature creation
└── gis_connection.py       cached ArcGIS Portal connection
```

## Open items / things to verify before this is real

1. **Never run end-to-end.** No live PoleScan environment or Portal
   connection was available while building this -- verify the whole path
   (upload -> discovery -> permit + pole creation) against a real plan
   set before relying on it.
2. **Permit numbering isn't coordinated with the original Joint Use
   Permits layer** (`cad924092c44404b825dec2eef80d604`) -- it's scoped
   only to `JointUsePermits_WorkAreas`' own `PERMIT_NUMBER` values. If
   permit numbers need to be unique across both layers, this needs a
   shared counter.
3. **Vision model reloads on every job.** `polescan_pipeline.py` loads and
   unloads Qwen per run, matching `main.py`'s own behavior exactly. Model
   load time is likely a meaningful chunk of each job's runtime -- keeping
   it loaded persistently between jobs (rather than per-job) would be a
   real perf win, at the cost of holding GPU memory continuously.
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
