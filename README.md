# OddesyAgent

OddesyAgent is a local-first Django control plane for Telegram-to-ComfyUI image-to-video generation.

This MVP intentionally keeps the surface area small:

- Django only
- SQLite database
- Telegram bot as a Django management command
- Background worker as a Django management command
- ComfyUI integration through HTTP
- DB-backed queue with one GPU job at a time
- Media library and audit logging
- No web dashboard
- No FastAPI
- No shell execution
- No arbitrary file access

## Implemented flow

1. An allowed Telegram user sends an image.
2. The bot stores the image as a `MediaAsset`.
3. The user sends `make video`.
4. The bot creates a queued `GenerationJob`.
5. The worker picks one queued job at a time.
6. The worker loads a workflow JSON template and replaces:
   - `{INPUT_IMAGE}`
   - `{PROMPT}`
   - `{SEED}`
7. The worker submits the workflow to ComfyUI and polls for completion.
8. Output media is downloaded, stored as a `MediaAsset`, and linked to the job.
9. The worker sends the generated video back to the Telegram user.

## Project layout

- `oddesyagent/`: Django project settings and URLs
- `apps/core/`: models, admin, bot command, worker command, services
- `workflows/`: ComfyUI workflow templates
- `media/`: runtime-uploaded and generated files

## Setup

1. Create a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy environment variables:

```bash
cp .env.example .env
```

4. Edit `.env` with your Telegram bot token and allowed user IDs.

5. Run migrations:

```bash
python manage.py makemigrations
python manage.py migrate
```

6. Optional: create an admin user:

```bash
python manage.py createsuperuser
```

## Running

Start the Telegram bot:

```bash
python manage.py run_telegram_bot
```

Start the worker in another terminal:

```bash
python manage.py run_worker
```

## Environment

Required or commonly used settings:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS`
- `COMFYUI_BASE_URL`
- `COMFYUI_WORKFLOW_PATH`
- `DEFAULT_PROMPT`
- `POLL_INTERVAL_SECONDS`

`TELEGRAM_ALLOWED_USER_IDS` must be a comma-separated list of Telegram numeric user IDs. Any user not in this list is rejected and logged.

## Telegram commands

- `/start`
- `/help`
- `/status`
- `/workflows`
- `/last`
- `/cancel`

## Notes

- This scaffold assumes ComfyUI is reachable at `http://127.0.0.1:8188` by default.
- The worker processes jobs serially to respect the "one GPU job at a time" requirement.
- Audit log rows are created for incoming commands, rejected access, and job transitions.
- Workflow templates are JSON files with simple placeholder replacement.
