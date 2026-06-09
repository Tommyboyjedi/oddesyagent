# OddesyAgent

OddesyAgent is a local-first Django control plane for Telegram-to-ComfyUI image-to-video generation.

Phase 1 is intentionally narrow:

- Django only
- SQLite only
- Telegram is the first UI
- ComfyUI HTTP API is mandatory
- One GPU job at a time
- No web dashboard
- No FastAPI
- No Celery or Redis
- No local LLM integration
- No shell execution features
- No arbitrary file access

## Phase 1 MVP flow

1. An allowed Telegram user sends an image.
2. The bot stores the image as a `MediaAsset`.
3. The user sends `make video`.
4. OddesyAgent creates a queued `GenerationJob`.
5. The worker picks one queued job at a time.
6. The worker loads a predefined ComfyUI workflow.
7. The worker replaces `{INPUT_IMAGE}`, `{PROMPT}`, and `{SEED}`.
8. The worker submits the workflow to ComfyUI.
9. The worker polls until completion or failure.
10. The worker stores generated output media.
11. The bot sends the generated video back to the originating user.

## Project layout

- `oddesyagent/`: Django project settings and URLs
- `apps/core/`: models, admin, management commands, and services
- `workflows/`: ComfyUI workflow JSON templates
- `media/`: Django-managed runtime media

## Requirements

- Python 3.11+
- A Telegram bot token
- A local ComfyUI instance, defaulting to `http://127.0.0.1:8188`

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create your environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- `COMFYUI_BASE_URL`
- `ODDESY_MEDIA_ROOT`
- `ODDESY_INTERNAL_API_ENABLED`
- `ODDESY_INTERNAL_API_TOKEN`
- `LITELLM_ENABLED`
- `LITELLM_MODEL`
- `LITELLM_API_KEY`
- `ODDESY_SAFE_ROOTS`
- `VAST_API_KEY`
- `YOUTUBE_UPLOAD_ENABLED`

Run migrations:

```powershell
python manage.py makemigrations
python manage.py migrate
```

Optional admin user:

```powershell
python manage.py createsuperuser
```

## Running

Start the Telegram bot:

```powershell
python manage.py run_telegram_bot
```

Start the worker:

```powershell
python manage.py run_worker
```

Useful worker options:

```powershell
python manage.py run_worker --once
python manage.py run_worker --sleep-seconds 3 --poll-seconds 5 --timeout-seconds 1800
```

Phase 2 control commands:

- `/queue` shows the queued and running jobs for the allowed user.
- `/history` shows recent completed, failed, and cancelled jobs with workflow, seed, and prompt details.
- `/rerun` queues a copy of the latest completed job.
- `/rerun 123` queues a copy of completed job `123` if it belongs to the allowed user.
- `/cancel` cancels queued jobs immediately and marks running jobs as `cancellation_requested` when interruption is not available.
- Completed jobs retain output metadata including file size, output asset type, ComfyUI filename/subfolder/type, and duration when ComfyUI exposes it.
- Failed jobs retain structured failure metadata such as `workflow_missing`, `placeholder_missing`, `output_missing`, `timeout`, or `comfyui_unavailable`.
- Queued local GPU jobs now carry explicit scheduler fields: `priority` and `requested_executor`. The current worker still runs only one `local_gpu` job at a time, but it now claims higher-priority work first.

Operator job inspection:

```powershell
python manage.py inspect_jobs
python manage.py inspect_jobs --job-id 12
python manage.py inspect_jobs --cancel 12
python manage.py inspect_jobs --retry 12
```

Local internal API boundary:

- Disabled by default with `ODDESY_INTERNAL_API_ENABLED=false`.
- Requires loopback access and `Authorization: Bearer <ODDESY_INTERNAL_API_TOKEN>`.
- Exposes only:
  - `GET /api/internal/workflows/`
  - `POST /api/internal/jobs/`
  - `GET /api/internal/jobs/<job_id>/`
  - `GET /api/internal/jobs/<job_id>/output/`
  - `GET /api/internal/media/`

Phase 3 natural-language parsing:

- Disabled by default with `LITELLM_ENABLED=false`.
- When enabled, plain text requests can be parsed into structured video job instructions.
- LiteLLM is limited to existing workflow names from `workflows/` and cannot request file paths, URLs, shell commands, or arbitrary tools.
- When disabled, the text fallback supports `make video`, `status`, `queue`, and `rerun [job_id]`.

Phase 5 tool registry foundation:

- Tool execution remains disabled by default; this phase adds policy checks, not open-ended execution.
- Tool definitions declare allowed inputs, forbidden inputs, audit requirements, confirmation requirements, and optional safe roots.
- Any future NAS/file tools must stay inside `ODDESY_SAFE_ROOTS`.
- Vast.ai support requires `VAST_API_KEY`.
- YouTube support requires `YOUTUBE_UPLOAD_ENABLED=true` plus later OAuth-specific work before any uploads are added.
- Destructive or external tool requests now create tracked `ToolExecutionRequest` records and must be explicitly confirmed before later execution work is added.
- `JobSchedulerService` makes queue selection explicit and testable while preserving the current single-local-GPU behavior.

Tool registry operator commands:

```powershell
python manage.py manage_tool_registry
python manage.py manage_tool_registry --submit media_cleanup_preview --inputs '{"target_path":"C:\\safe-root\\job-1","dry_run":true}'
python manage.py manage_tool_registry --submit safe_root_browser --inputs '{"target_path":"C:\\safe-root","limit":25}'
python manage.py manage_tool_registry --submit media_cleanup_preview --inputs '{"target_path":"C:\\safe-root","limit":25,"older_than_days":7,"extensions":[".mp4",".png"]}'
python manage.py manage_tool_registry --submit media_cleanup --inputs '{"target_path":"C:\\safe-root","limit":25,"older_than_days":30,"extensions":[".mp4"]}'
python manage.py manage_tool_registry --submit media_library_report --inputs '{"limit":50,"older_than_days":14,"asset_types":["generated_video"]}'
python manage.py manage_tool_registry --requests
python manage.py manage_tool_registry --confirm 3
python manage.py manage_tool_registry --execute 3
python manage.py manage_tool_registry --reject 4 --reason "Not approved"
```

## Telegram commands

- `/start`
- `/help`
- `/status`
- `/workflows`
- `/queue`
- `/history`
- `/rerun [job_id]`
- `/last`
- `/cancel`

Plain text inputs:

- `make video`
- `status`
- `queue`
- `rerun`
- Natural-language video requests when LiteLLM is enabled

## ComfyUI workflow setup

The file `workflows/i2v_wan_480p.json` is a placeholder example in API JSON format.

You should replace it with a real workflow exported from ComfyUI:

1. Build and test the workflow in ComfyUI.
2. Export the workflow in API format.
3. Save it as `workflows/i2v_wan_480p.json`.
4. Keep the placeholder values:
   - `{INPUT_IMAGE}`
   - `{PROMPT}`
   - `{SEED}`

OddesyAgent only loads workflows from the configured workflows directory. It does not accept arbitrary file paths from users.

## Validation

Expected validation commands:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
python manage.py check
python manage.py test
```

## Troubleshooting

- `Access denied` from Telegram usually means your Telegram numeric user ID is missing from `TELEGRAM_ALLOWED_USER_IDS`.
- If the worker stays idle, confirm a queued `GenerationJob` exists and the bot and worker are using the same SQLite database.
- If ComfyUI calls fail, confirm `COMFYUI_BASE_URL` is correct and ComfyUI is reachable locally.
- If no media is returned, verify the ComfyUI workflow writes output files and that the exported API workflow still includes the placeholders.
