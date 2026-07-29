# TeamSwarm

TeamSwarm is a Python-first, provider-neutral multi-agent orchestration MVP.
It combines a FastAPI control plane, a database-backed dependency-aware task
queue, provider adapters (mock, OpenAI, and Ollama), persistent project chat,
and a Next.js dashboard.

This guide gets the complete application running on one computer. The fastest
route uses SQLite and the mock provider; Ollama, Docker services, and OpenAI
are optional additions.

## What runs locally

| Component | Purpose | Default address |
| --- | --- | --- |
| FastAPI API | runs, queue, agents, cache, chat, usage, and model catalog | `http://localhost:8000` |
| Next.js Web UI | dashboard, task graph, model/usage views, project chat | `http://localhost:3000` |
| SQLite or PostgreSQL | persistent application data | `./teamswarm.db` or port `5432` |
| Ollama (optional) | local Llama/Qwen inference | `http://localhost:11434` |
| Docker services (optional) | PostgreSQL plus future Redis, MinIO, and Temporal integration | see `infra/docker-compose.yml` |

## Prerequisites

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) for Python environments and dependencies
- Node.js 20 or later and npm
- Git, if you are cloning the project
- [Ollama](https://ollama.com/) or [SGLang](https://docs.sglang.io/) when running local models
- Docker Desktop only when using PostgreSQL and the optional backing-service
  stack

## Quick start: mock provider with SQLite

This path requires no API key, local model, or Docker installation.

1. Create a local environment file.

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set the following values. SQLite is easiest for a single
   local developer; `mock` returns deterministic test responses.

   ```dotenv
   TEAMSWARM_DATABASE_URL=sqlite+aiosqlite:///./teamswarm.db
   TEAMSWARM_PROVIDER_MODE=mock
   TEAMSWARM_PROJECT_CONTEXT_ROOTS=.
   ```

3. Install Python dependencies and start the API from the repository root.

   ```bash
   uv sync --all-groups
   uv run uvicorn services.api.app.main:app --reload --port 8000
   ```

4. In a second terminal, install and start the Web UI.

   ```bash
   cd apps/web
   npm install
   npm run dev
   ```

Open [the local Web UI](http://localhost:3000). Confirm the API separately at
[the health endpoint](http://localhost:8000/health).

The SQLite file is created automatically in the repository root. It contains
projects, chats, runs, tasks, artifacts, cache entries, traces, and usage
records, so restarting the app preserves local state.

## Run Llama and Qwen locally with Ollama

Install Ollama, ensure its local service is running, and pull the models. If
the desktop app is not running the service, start it with `ollama serve` in a
separate terminal.

```bash
ollama pull llama3.2:3b
ollama pull qwen3:8b
ollama list
```

Update `.env`:

```dotenv
TEAMSWARM_DATABASE_URL=sqlite+aiosqlite:///./teamswarm.db
TEAMSWARM_PROVIDER_MODE=ollama
TEAMSWARM_OLLAMA_BASE_URL=http://localhost:11434
TEAMSWARM_OLLAMA_FAST_MODEL=llama3.2:3b
TEAMSWARM_OLLAMA_STRONG_MODEL=qwen3:8b
TEAMSWARM_OLLAMA_FALLBACK_MODEL=qwen3:8b
```

Start the API and Web UI using the commands in the quick start. TeamSwarm uses
Ollama's local non-streaming generation endpoint; inference stays on your
computer. You can substitute any installed Llama or Qwen tags that suit your
hardware. The Model Catalog marks a configured local model as **available**
only when Ollama reports it as installed.

## Attach text files to a prompt

The dashboard accepts text and source-code files for both project-chat messages
and runs. Their contents are included as bounded prompt context; the prompt
trace retains only each filename and content hash. Attachments are limited to
eight supported text files and 240 KB total. Binary documents, images, and files
outside the prompt payload are not uploaded or read by the API.

## Run the delivery-cycle orchestrator

Submitting a run from the dashboard starts the delivery-cycle workflow: a
criteria agent, discovery agent, coding agent, testing agent, and evaluator run
in order. The evaluator must return `GOAL_ACHIEVED: yes`; otherwise TeamSwarm
runs one more bounded cycle before returning a failure. All roles receive the
same user prompt and attached-file context, while downstream roles also receive
their explicitly granted predecessor results.

Each role can use a distinct model by setting `TEAMSWARM_CRITERIA_MODEL`,
`TEAMSWARM_DISCOVERY_MODEL`, `TEAMSWARM_CODING_MODEL`,
`TEAMSWARM_TESTING_MODEL`, and `TEAMSWARM_EVALUATOR_MODEL`. An unset role uses
its configured fast or strong profile.

## Local stable-version history

When a delivery-cycle evaluator returns `GOAL_ACHIEVED: yes`, TeamSwarm creates
a local Git commit for the current workspace, named `teamswarm: stable run …
cycle …`. It does not push or modify remote branches. The run trace records the
commit hash, or an `unchanged`, `unavailable`, or `failed` outcome. Initialize
Git and make an initial baseline commit before using the workflow; Git failures
are observable in the trace but never discard an evaluator-approved result.

## Context optimization

Before every model call, TeamSwarm builds a typed context inventory from
attached files and explicitly granted agent handoffs. The deterministic optimizer
deduplicates content, prioritizes required and high-authority evidence, applies
`TEAMSWARM_CONTEXT_TOKEN_BUDGET`, and stores selected and omitted sources in a
run-scoped context manifest. When a delivery cycle repeats, a bounded structured
handoff summary carries forward prior role outputs rather than replaying the
whole transcript.

## Skills and planning agents

TeamSwarm discovers portable Agent Skills from `TEAMSWARM_SKILL_ROOTS` using
the open `SKILL.md` format. The API advertises metadata at `GET /skills`; a run
selects skills by name, snapshots their instructions and hashes, and includes
the selected instructions in the context optimizer for every worker. Bundled
skill scripts are never executed implicitly, and `allowed-tools` is metadata
rather than an authorization grant.

Set `planner_backend` on `POST /runs` to choose how tasks are created:

- `deterministic` uses the existing validated planner.
- `provider-agent` asks TeamSwarm's configured strong model for a structured,
  bounded task DAG and validates IDs, dependencies, cycles, and contracts.
- `autogen` uses Microsoft's open-source AutoGen `AssistantAgent` as the
  planning-only lead and then hands its validated DAG to TeamSwarm.

Install the optional AutoGen backend with `uv sync --extra autogen`. AutoGen is
used only to generate tasks; TeamSwarm remains responsible for skills, budgets,
permissions, scheduling, context sharing, execution, and stable Git versions.

## Run a local GPU model with SGLang

SGLang is an optional high-throughput local serving backend. It is most useful
on a supported GPU host when several TeamSwarm tasks run concurrently. Install
and launch an SGLang model server, for example:

```bash
python3 -m sglang.launch_server --model Qwen/Qwen3-4B --host 0.0.0.0 --port 30000
```

Update `.env`:

```dotenv
TEAMSWARM_PROVIDER_MODE=sglang
TEAMSWARM_SGLANG_BASE_URL=http://localhost:30000
TEAMSWARM_SGLANG_FAST_MODEL=Qwen/Qwen3-4B
TEAMSWARM_SGLANG_STRONG_MODEL=Qwen/Qwen3-8B
TEAMSWARM_SGLANG_FALLBACK_MODEL=Qwen/Qwen3-8B
```

TeamSwarm calls SGLang's OpenAI-compatible chat-completions API and discovers
models from its `/v1/models` endpoint. The configured models appear as
**available** in the Model Catalog once the server reports them.

## Use OpenAI models

Set a provider mode, model names, and a key in `.env`:

```dotenv
TEAMSWARM_PROVIDER_MODE=openai
OPENAI_API_KEY=replace-with-your-key
TEAMSWARM_FAST_MODEL=gpt-5.6-terra
TEAMSWARM_STRONG_MODEL=gpt-5.6-sol
TEAMSWARM_FALLBACK_MODEL=gpt-5.6-sol
```

Do not commit `.env` or an API key. The model catalog and usage dashboard keep
remote and local models visually distinct.

## Use PostgreSQL and local backing services

The supplied Compose file starts PostgreSQL, Redis, MinIO, and Temporal. The
current API actively uses PostgreSQL; Redis, MinIO, and Temporal are included
for the next durable-execution integration and do not need application setup
for the MVP.

1. Keep the PostgreSQL connection string from `.env.example`:

   ```dotenv
   TEAMSWARM_DATABASE_URL=postgresql+asyncpg://teamswarm:teamswarm@localhost:5432/teamswarm
   ```

2. Start services from the repository root:

   ```bash
   docker compose -f infra/docker-compose.yml up -d
   ```

3. Start the API and Web UI as in the quick start.

To inspect or stop the services:

```bash
docker compose -f infra/docker-compose.yml ps
docker compose -f infra/docker-compose.yml stop
```

Compose volumes retain database and object-store data after `stop`. Use Docker
Desktop to manage or intentionally remove volumes when you want a clean slate.

## Workers and task dependencies

For development, the API starts an inline worker by default. It leases
dependency-ready tasks from the database queue and executes them in-process.

To run a separate worker pool, start the API with inline execution disabled,
then launch one or more workers that point at the same `.env` database:

```bash
TEAMSWARM_INLINE_WORKER_ENABLED=false uv run uvicorn services.api.app.main:app --port 8000
TEAMSWARM_WORKER_ID=worker-a uv run python -m services.worker.local_worker
TEAMSWARM_WORKER_ID=worker-b uv run python -m services.worker.local_worker
```

A task runs only after all declared dependencies succeed. Workers receive only
the task-scoped context and artifacts that the Lead Agent grants; queue-wide
state and ungranted results are not exposed to models.

## Project-scoped persistent chat

In the Web UI, register a project directory, create a chat, and send messages.
Each chat is persisted with its messages, model metadata, context hash, and
project association.

For every chat request TeamSwarm sends bounded context: a capped file manifest,
selected reference files when present (`README.md`, `ARCHITECTURE.md`,
`AGENTS.md`, `pyproject.toml`, and `package.json`), and recent chat history.
It does not provide unrestricted filesystem access.

For safety, project directories must be under
`TEAMSWARM_PROJECT_CONTEXT_ROOTS`. The default `.` allows directories under the
TeamSwarm repository. To chat against other local projects, set one or more
comma-separated absolute roots before starting the API:

```dotenv
TEAMSWARM_PROJECT_CONTEXT_ROOTS=/Users/your-name/projects,/Users/your-name/sandboxes
```

Restart the API after changing this setting. A directory outside these roots is
rejected instead of being read.

## Verify the installation

With the API running:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/models
curl http://localhost:8000/usage/last-24-hours
```

Run the backend checks from the repository root:

```bash
uv run pytest -q
uv run ruff check services tests
```

Run the Web UI checks:

```bash
cd apps/web
npm run lint
npm run build
```

To run the local-model regression suite, first start Ollama and pull the two
configured models, then run:

```bash
npx --yes promptfoo@latest eval -c evals/promptfooconfig.yaml
```

See [evals/README.md](evals/README.md) for the evaluation cases and output.

## Key configuration

| Variable | Meaning | Default in `.env.example` |
| --- | --- | --- |
| `TEAMSWARM_DATABASE_URL` | SQLAlchemy async database connection | local PostgreSQL Compose service |
| `TEAMSWARM_PROVIDER_MODE` | `mock`, `ollama`, `sglang`, or `openai` | `mock` |
| `TEAMSWARM_OLLAMA_BASE_URL` | Ollama server base URL | `http://localhost:11434` |
| `TEAMSWARM_SGLANG_BASE_URL` | SGLang server base URL | `http://localhost:30000` |
| `TEAMSWARM_*_MODEL` | fast, strong, and fallback model selection | see `.env.example` |
| `TEAMSWARM_INLINE_WORKER_ENABLED` | runs a development worker inside the API | `true` |
| `TEAMSWARM_MAX_CONCURRENT_TASKS` | maximum tasks the local worker executes | `4` |
| `TEAMSWARM_TASK_TIMEOUT_SECONDS` | per-task attempt timeout | `45` |
| `TEAMSWARM_PROJECT_CONTEXT_ROOTS` | allowed roots for project chat context | `.` |
| `NEXT_PUBLIC_API_BASE_URL` | Web UI API URL | `http://localhost:8000` |

## Troubleshooting

- **`Connection refused` for Ollama:** start the Ollama desktop app or run
  `ollama serve`; verify the model with `ollama list`.
- **PostgreSQL connection failure:** start Docker Desktop and run the Compose
  command, or switch `TEAMSWARM_DATABASE_URL` to the SQLite value shown above.
- **A project directory is rejected in chat:** add its parent directory to
  `TEAMSWARM_PROJECT_CONTEXT_ROOTS`, then restart the API.
- **The Web UI cannot reach the API:** confirm `http://localhost:8000/health`
  works and that `NEXT_PUBLIC_API_BASE_URL` matches the API address; restart
  `npm run dev` after changing it.
- **Models are listed but unavailable:** the tag is configured but not pulled
  into the active Ollama instance. Run `ollama pull <model-tag>`.

## MVP capabilities and boundaries

- Runs support contracts, priorities, idempotency keys, retries, timeouts, and
  dependency-aware database leasing.
- Successful task results become immutable, hashed artifacts with provenance.
  Cache sharing is deny-by-default and uses Lead-Agent-issued task grants.
- Run replay, queue claims, routing decisions, evaluations, and per-model token
  usage are available through the API and dashboard.
- Token totals are exact for the mock provider and provider-reported where the
  provider supplies usage. Subscription usage is internal until reconciled
  against a provider billing feed.
- Temporal is scaffolded but is not yet the API's active execution backend.

For the implementation scope, see [MVP.md](MVP.md). For the complete
provider-neutral design, see [ARCHITECTURE.md](ARCHITECTURE.md).
