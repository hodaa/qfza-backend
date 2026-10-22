# JumpTo — YouTube Keyword Timestamp Finder

A web application where users input a YouTube URL and a keyword/phrase to get exact timestamps where that phrase is spoken.

## Architecture

```
React (frontend) → FastAPI (backend) → PostgreSQL + Redis + Assembly.ai
```

## Quick Start

### Prerequisites

- Python 3.12+
- Node.js 20+
- PostgreSQL 17+ (running on localhost:5432)
- Redis 7+ (running on localhost:6379)

### Backend Setup

```bash
cd backend

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Copy environment template
cp ../.env.example .env
# Edit .env as needed (SMTP, Google sign-in, Blob token, ...)

# Setup databases
../scripts/setup_db.sh

# Run API server
../scripts/run_api.sh
```

API will be available at `http://localhost:8000`

### Run with Docker

`docker-compose.yml` builds and runs the API only — PostgreSQL and Redis are
expected to be reachable servers, configured through `.env` (`DATABASE_URL`,
`REDIS_URL`):

```bash
docker compose up --build          # API on http://localhost:8000

# Apply schema migrations (a deploy-time step, see below)
docker compose run --rm api alembic upgrade head

docker compose down
```

Override the published port with `API_PORT`. If PostgreSQL or Redis runs on the
Docker host itself, use `host.docker.internal` instead of `localhost` in those
URLs so the container can reach it.

### Worker (transcription pipeline)

The Celery worker lives in the separate **`jumpto-worker`** repo. Run it from
there:

```bash
cd ../jumpto-worker
.venv/bin/celery -A app.tasks.celery_app worker --loglevel=info
```

### Frontend Setup

```bash
cd frontend
npm install
cp .env.example .env        # VITE_API_BASE_URL defaults to http://localhost:8000
npm run dev
```

Frontend will be available at `http://localhost:5173` (defaults to Arabic, RTL; toggle to English in the header). In dev, `/api/*` requests are proxied to the backend on port 8000.

## API Endpoints

### POST /api/search
Search for keyword in YouTube video.

**Request:**
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "keyword": "never gonna give you up"
}
```

**Response (cached):**
```json
{
  "status": "found",
  "results": [
    {"timestamp": "00:00", "progress_seconds": 0.0, "text_snippet": "never gonna give you up"},
    {"timestamp": "00:02", "progress_seconds": 2.0, "text_snippet": "never gonna give you up"}
  ]
}
```

**Response (not cached):**
```json
{
  "status": "processing",
  "job_id": "uuid-here"
}
```

### GET /api/status/{job_id}
Check transcription job status.

**Response:**
```json
{
  "status": "pending|processing|completed|failed",
  "progress": 50,
  "results": [...],
  "error": "user-safe error message if failed"
}
```

### GET /api/video/{video_id}/search?keyword=...
Search cached transcript for specific video.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| DATABASE_URL | PostgreSQL connection URL | `postgresql://postgres:postgres@localhost:5432/jumpto` |
| REDIS_URL | Redis connection URL | `redis://localhost:6379/0` |
| CORS_ORIGINS | Allowed CORS origins (comma-separated) | `http://localhost:5173` |
| ENVIRONMENT | `development` or `production` | `development` |
| LIVE_EXTERNAL_CALLS | Enable live yt-dlp/Assembly.ai | `false` |
| TRANSCRIPT_MODE | `real` or `fake` | `real` |

Set `TRANSCRIPT_MODE=fake` to run without an Assembly.ai key (downloads are skipped and a fake transcript is used in dev).

## Production Deployment (Vercel + Render)

The API is a single stateful FastAPI app split across two hosts:

- **API (FastAPI)** → deployed on **Vercel** serverless functions.
- **Worker (Celery)** → deployed on **Render** as a background worker from the
  `jumpto-worker` repo (see its [`Dockerfile`](../jumpto-worker/Dockerfile)).

Both must be configured to reach the **same Redis broker and the same Neon
database**:

1. **Redis**: Render's managed Redis is private and unreachable from Vercel, so
   use an internet-reachable Redis (e.g. Upstash free tier). Set `REDIS_URL`
   identically on **both** Vercel and Render.
2. **Database**: set the Neon `DATABASE_URL` on both. `sslmode=...` query params
   are supported (translated to asyncpg's `ssl`); `channel_binding` is stripped.
3. **Transcription**: set `ASSEMBLY_API_KEY`,
   `LIVE_EXTERNAL_CALLS=true`, `TRANSCRIPT_MODE=real`, and
   `ENVIRONMENT=production` on the worker.

Vercel API env: `DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `ENVIRONMENT`.
Render worker env: `DATABASE_URL`, `REDIS_URL`, `ASSEMBLY_API_KEY`,
`LIVE_EXTERNAL_CALLS`, `TRANSCRIPT_MODE`, `ENVIRONMENT`.

## Database Migrations on Deploy

Schema migrations are a **deploy-time step, not an app-startup step**. The
serverless API never runs migrations on cold start — it relies on the schema
already existing. Each deploy applies pending migrations **before** release:

1. **CI/CD job (`.github/workflows/migrate.yml`)**: on every push to `main`, runs
   `alembic upgrade head` against the production database and turns the check
   **red** if it fails, so a broken migration blocks the release. Needs a GitHub
   repository secret `DATABASE_URL` pointing at the production Neon database.
2. **Worker**: the worker image runs `alembic upgrade head` before starting
   Celery, applying any schema migrations that run on the worker deploy path.
3. **One-time legacy bootstrap**: if a database was created by the old
   `create_all` path it has no `alembic_version` table, so a plain
   `alembic upgrade head` would try to recreate existing tables and fail. Run
   the migration workflow manually once with the `stamp_revision` input
   (the revision matching the schema already in the DB) so alembic only applies
   the pending changes after it.

## Testing

### Backend

```bash
cd backend
source .venv/bin/activate
pytest --cov=app --cov-fail-under=80 -v
```

### Frontend

```bash
cd frontend
npx vitest run --coverage   # 80% thresholds enforced in vite.config.ts
npm run lint                # ESLint strict
npx prettier --check .      # formatting
npm run build               # tsc --noEmit && vite build
```

## Project Structure

```
jumpto/
├── backend/           # FastAPI application
│   ├── app/
│   │   ├── api/       # API routes
│   │   ├── core/      # Config, DB, logging, exceptions
│   │   ├── models/    # SQLAlchemy models
│   │   ├── repositories/  # Data access layer
│   │   ├── services/  # Business logic
│   │   ├── schemas/   # Pydantic schemas
│   │   └── tasks/     # Celery tasks
│   ├── tests/
│   │   ├── unit/      # Unit tests
│   │   ├── integration/ # Integration tests
│   │   └── utils/     # Test utilities
│   ├── alembic/       # Database migrations
│   └── pyproject.toml
├── frontend/          # React application (TypeScript, Vite, Vitest, RTL)
│   ├── src/
│   │   ├── api/       # HTTP client + error mapping
│   │   ├── components/# UI components (bilingual EN/AR, RTL)
│   │   ├── hooks/     # Job polling hook
│   │   ├── i18n/      # react-i18next catalogs (en, ar)
│   │   └── utils/     # YouTube URL parsing
│   └── vite.config.ts
├── scripts/           # Setup and run scripts
├── .env.example       # Environment template
└── .gitignore
```

## Development

### Database Migrations

```bash
cd backend
# Create new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head
```

### Code Quality

```bash
# Format
black backend/

# Lint
ruff check backend/

# Type check (if using mypy)
mypy backend/app/
```

## License

MIT