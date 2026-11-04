# Florida 511 Research Observatory — Streamlit implementation

A self-contained Streamlit application recreating the Florida 511 camera dashboard. The original `fl511-dashboard` remains a separate application. This folder includes the existing YOLO26 nano model under `models/yolo26n.pt`; no files from a parent directory and no localhost API server are required.

## Run locally

Use Python 3.13 (the locally tested version) and FFmpeg on PATH. On Windows, run `Start-Streamlit.cmd` using the existing research Python environment, or:

```text
cd "streamlit implementation"
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

FFmpeg is a system dependency; `packages.txt` installs it on Streamlit Community Cloud. For other Linux hosting, install the three listed system packages yourself. On Windows, set `FL511_FFMPEG` to the FFmpeg executable path if it is not on PATH.

## Deploy on Streamlit Community Cloud

1. Upload **the contents of this folder** to the root of a GitHub repository. Include `models/yolo26n.pt`, `assets/cameras.json`, `requirements.txt`, `packages.txt`, `.streamlit/config.toml`, and all Python modules. Exclude `.runtime`, secrets, local recordings, and Python caches.
2. In Streamlit Community Cloud, create an app from that repository with entry point **`app.py`**. Select **Python 3.13** in Advanced settings to match local validation.
3. If deploying this folder within a larger repository instead, select **`streamlit implementation/app.py`** as the entry point. Streamlit locates Python dependency files next to the entry point. Copy `packages.txt` to the **repository root** so Community Cloud installs FFmpeg. Also move/copy `.streamlit/config.toml` to the repository root's `.streamlit/` folder because Streamlit reads configuration relative to its working directory. Deploying the folder contents as a standalone repository is the simplest option.
4. Make the deployment private using Streamlit's app-sharing controls. Optionally add `access_password = "your-long-password"` in Cloud Secrets; see `.streamlit/secrets.toml.example`. Never commit a real secret.
5. Open the app, sync Florida 511 for current camera availability, select cameras, and start using the live view or recording controls. A public seed catalog is included so the directory is usable before syncing; it is explicitly dated in the UI.

The Python dependencies pin the locally tested Streamlit, Ultralytics, OpenCV, Torch, NumPy and tracker versions. Torch is requested from its official CPU wheel index to avoid installing CUDA libraries on a CPU-only host. A fresh Linux installation and cloud deployment still need to be verified in Cloud build logs; this project does not claim a deployed URL or pre-provision a hosting account.

## Features

- Searchable statewide camera catalog with region/county/roadway filters, pagination, snapshots and up to four selected cameras.
- Camera-following NOAA/NWS radar with manual Florida station presets, reload and an external full-view link.
- Server-side live motion preview, with no laptop-local HTTP endpoint, browser HLS/CORS workaround, or incoming camera/microphone permission.
- Live YOLO26 + ByteTrack overlays and counts for cars, motorcycles, buses and trucks beside the radar. Configure orientation, position and confidence; start/reset or stop a counting session; export crossing events.
- Counts distinguish current vehicles in the frame from cumulative directional line crossings. Each track is counted at most once per session; types use confidence-weighted votes. Occlusion, tracking fragmentation and skipped frames can change results.
- Timezone-aware capture scheduling, up to four overlapping recordings across the app instance, cancellation, and MKV downloads. SQLite preserves state while the underlying files persist.
- Upload MP4/MKV/MOV/AVI files, analyze full clips or their first 30/60 seconds, review annotated H.264 MP4 video and download counts, crossings, tracked observations and JSON summaries.
- Per-workspace storage and resource limits: one live/offline inference worker across the app instance, four overlapping captures, up to 16 cached workspaces. Workers run outside Streamlit's script thread; fragments poll results without resetting the radar on every frame.

## Native Streamlit behavior and hosting limits

Streamlit does not expose the original custom HTTP streaming endpoint on Community Cloud. This implementation therefore displays server-decoded live JPEG frames through native Streamlit image elements. The ordinary preview decodes up to two frames per second; live detection processes the newest frames and the page refreshes results every second. This is a near-live annotated motion view, not full-frame-rate browser video. Completed annotated files play smoothly through `st.video`.

Community Cloud has shared CPU/memory limits, can sleep, and can restart apps. It does not provide a guaranteed always-on scheduler or durable local disk. **Captures and inference only run while the app process is alive.** A future schedule can be missed during sleep/restart. Reboots, redeployment or cache clearing may interrupt jobs, lose local data, or reset workspace resources. For guaranteed unattended recording and persistent research archives, use an external always-on worker and durable object storage, with Streamlit as the UI. This version intentionally makes those limits visible rather than promising reliable 24/7 operation on Community Cloud.

Uploads are limited to 200 MB. Browser downloads are limited to 150 MB per file to keep native Streamlit memory usage bounded. Use shorter recording windows and download results before restarting. Live sessions stop after one hour or after two minutes without polling; navigation away from the live page stops its polling. Preview connections are stopped when leaving the live page. Selecting a new camera resets its live counting session. The app is CPU-only by default, so hosted performance can be much lower than the local test machine.

## Workspace privacy and storage

Each visitor receives an unguessable `?workspace=...` URL and a separate directory under `.runtime/<key>/`. Bookmark that URL to revisit the same workspace while storage persists. Anyone who has that URL and passes the optional app password can access that workspace: it is a bearer link, **not a user-account authentication system**. Use Streamlit's private app access for your research deployment. Do not share workspace links containing private footage.

Set environment variable `FL511_DATA_DIR` to another storage root on infrastructure that offers durable storage; it does not make Community Cloud's disk durable. No stream authorization tokens are stored in job output or logs. Workers resolve short-lived URLs using Florida 511's normal public playback flow. These undocumented source endpoints may change or reject hosted-server connections. Offline feeds report errors; they do not produce fabricated success.

## Code structure

- `app.py`: Streamlit UI, pages, forms, polling fragments and downloads.
- `backend.py`: workspace isolation, scheduling controls, global resource budget and live frame preview.
- `recorder.py`: Florida 511 catalog/resolver, SQLite jobs, FFmpeg recording and scheduler. It contains no HTTP server.
- `analysis_service.py` / `vehicle_analysis.py`: offline worker orchestration, tracking, counting and exports.
- `live_detection.py`: live worker and per-workspace session lifecycle.
- `tests/`: UI and service regression tests.

## Validation

```text
python -m unittest discover -s tests -v
```

Local validation completed: ten UI/service/counting regression tests; a live Streamlit counting session beside radar; and an end-to-end scheduled public-camera sample followed by offline inference on 74 frames with all five exports. The live capture was correctly marked **partial** after a source interruption, and its usable footage was analyzed. The optional network smoke test can be repeated with `python tests/verify_network.py`. These checks use the existing Windows Python environment; a fresh Linux dependency installation and deployed Community Cloud instance have not been tested here.

Recording filenames use camera IDs rather than full location names to avoid Windows path-length failures in nested workspace directories. The UI retains the human-readable camera name.

Sources and deployment references:

- [Florida 511](https://fl511.com/cctv)
- [NWS Radar](https://radar.weather.gov/)
- [Streamlit fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment)
- [Community Cloud dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- [Community Cloud resources and management](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)
