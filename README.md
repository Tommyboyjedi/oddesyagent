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

## Telegram commands

- `/start`
- `/help`
- `/status`
- `/workflows`
- `/last`
- `/cancel`

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
