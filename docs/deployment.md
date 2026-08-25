# Containers and deployment

One `Dockerfile` at the repo root defines both environments the app runs in, as
two build targets:

| Target | Used by | Source tree | Database |
| --- | --- | --- | --- |
| `dev` | `.devcontainer/devcontainer.json` | bind-mounted from the developer's disk | a file in that mount (`./data/mail.db`) |
| `prod` | a deployment (Kubernetes) | baked into the image at `/app` | a separate persistent mount (`/data/mail.db`) |

The two share the base image, the virtualenv layout and the dependency
resolution, so the interpreter and the installed packages cannot drift between
what a change is developed against and what runs in production.

The base image is `python:3.14-slim-trixie`. Python 3.14 is a hard floor rather
than a preference: the export/import format uses `compression.zstd`, which is
standard library only from that release.

## Building

```bash
make image        # docker build --target prod -t mailing-list-ai-check:<version> .
make image-dev    # docker build --target dev  -t mailing-list-ai-check:dev .
```

`make image` tags the image with the package version read from
`src/mailing_list_ai_check/__init__.py`, and additionally as `latest`. The build
needs network access: it installs the Python dependencies from PyPI and the
frontend dependencies from the npm registry.

Both targets are built from the same context, filtered by `.dockerignore`. That
file also keeps local data out of the build: an export written with no path
lands in the repo root and is routinely hundreds of megabytes, so `*.jsonl.zst`,
`data/` and `*.db` are excluded along with `.env` and the other credential
files.

### Stages

- `frontend` (`node:22-trixie-slim`) — `npm ci` followed by `npm run build`,
  which is what `make build` runs. Produces `frontend/dist`.
- `base` — the Python image plus a virtualenv at `/opt/venv`, on `PATH`. The
  virtualenv is outside the workspace so a bind-mounted tree carrying the
  developer's own `.venv`, built for another platform, cannot shadow it.
- `deps` — copies `pyproject.toml` and the version module only, then installs
  the package and immediately uninstalls it. That resolves the dependency list
  from `pyproject.toml` without restating it in the Dockerfile, and leaves the
  layer independent of ordinary source edits.
- `prod`, `dev` — described below.

### Why both targets install the package in editable mode

`webapp.create_app` locates `frontend/dist`, and the `/api/docs` endpoints
locate the documentation set, by walking up from the package's `__file__`
(`webapp._repo_root`). A non-editable install copies the package into
`site-packages`, where that walk lands somewhere without a `frontend/dist` or a
`README.md`: the app would fall back to dev mode and serve no dashboard. An
editable install keeps the import pointing at the tree that holds them.

## The dev image

Contents: Python 3.14 with the `dev` and `prod` dependency sets installed
(`pytest`, `ruff`, `pre-commit`, `gunicorn`), Node and npm copied from the image
that builds `frontend/dist`, and `make`, `git`, `curl`, `sudo` and `zstd`. It
holds no application source.

The dev container mounts the repo at
`/workspaces/mailing-list-ai-check` and runs `.devcontainer/post-create.sh`
once, which installs the mounted tree with `pip install --no-deps -e .` and
installs the frontend dependencies. Consequences of that arrangement:

- Edits on the host are what runs in the container, in both directions, with no
  rebuild and no reinstall.
- The database is a file inside the mounted tree — `DATABASE_PATH` defaults to
  `./data/mail.db`, and `data/` is gitignored — so it lives on the developer's
  disk with the code and survives rebuilding the container.
- `frontend/node_modules` is a named volume rather than part of the bind mount,
  because that tree holds platform-specific binaries (esbuild, rollup): a host
  install must not be visible to the container, nor the container's to the host.
  Docker creates the volume root-owned, and the post-create script takes
  ownership of it before installing.
- The container user is `vscode`, uid/gid 1000, which matches the common
  single-user Linux host so that files created in the mount belong to the
  developer. When the host uid differs, the devcontainer runtime's
  `updateRemoteUserUID` (on by default) remaps the container user to it.
- The image sets `VENV_BIN=""`. The Makefile runs the Python tools from
  `./.venv` when the tree has one and from `PATH` otherwise; the empty value
  pins the container to `PATH` — that is, `/opt/venv` — so a `.venv` the
  developer built on the host is ignored inside the container while still being
  used by the same `make test` on the host.

Ports 8050 (the Flask API) and 5173 (the Vite dev server) are forwarded. The
two-terminal workflow is unchanged; `make dev` prints it.

Without VS Code, the same image serves as an ordinary container:

```bash
make image-dev
docker run --rm -it \
  -v "$PWD":/workspaces/mailing-list-ai-check \
  -w /workspaces/mailing-list-ai-check \
  -p 8050:8050 -p 5173:5173 \
  mailing-list-ai-check:dev bash
# then, once:  pip install --no-deps -e . && make install-frontend
```

## The production image

Contents at `/app`: `src/`, `docs/`, `README.md`, `CHANGELOG.md`,
`pyproject.toml`, and `frontend/dist` from the frontend stage. Not included:
`tests/`, `.devcontainer/`, `docs/design/`, the git history, and — by
`.dockerignore` — every credential file and local database.

- **User.** Runs as `mlac`, uid/gid 1001, both fixed so a Kubernetes
  `securityContext` and a volume's ownership can name them.
- **Writable paths.** `/app` stays root-owned: the application writes only to
  the database mount and to a temporary directory, and
  `PYTHONDONTWRITEBYTECODE=1` keeps it from writing `__pycache__` either. The
  image therefore runs with a read-only root filesystem, given a writable `/tmp`
  (see "Temporary space" below).
- **Database.** `DATABASE_PATH` defaults to `/data/mail.db`, and `/data` is
  created owned by uid 1001. Nothing else in the image writes there; the volume
  mounted at `/data` is the only persistent state.
- **Web server.** `CMD` is `mlac-web` (`docker/mlac-web`), which execs gunicorn
  with `mailing_list_ai_check.webapp:create_app()` as an application factory.
  `mail-ai-web` remains Flask's single-process development server and is not
  what the image runs.
- **Other commands.** `mail-ai-pull`, `mail-ai-extract`, `mail-ai-score`,
  `mail-ai-export`, `mail-ai-export-stats` and `mail-ai-import` are all on
  `PATH` in the same image, so a Job or a `docker run` overrides the command
  without needing a second image.
- **Health.** The `HEALTHCHECK` requests `GET /api/capabilities`, the cheapest
  GET in the API: it reads the app config and never opens the database.

```bash
docker run --rm -p 8050:8050 \
  -v mlac-data:/data \
  -e PUBLIC_READONLY=1 \
  mailing-list-ai-check:latest
```

### Configuration

Every setting is an environment variable; the image bakes in no configuration
beyond the defaults below. `Config.load` reads the application keys (a `.env`
file is a development convenience and is absent from the image), and
`docker/mlac-web` reads the server keys.

| Variable | Default in the image | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/mail.db` | SQLite file, on the persistent mount |
| `FLASK_HOST` / `FLASK_PORT` | `0.0.0.0` / `8050` | bind address |
| `LOG_LEVEL` | `INFO` | logging level |
| `PUBLIC_READONLY` | unset (off) | refuse every non-GET request with 403 |
| `ALLOW_EXPORT` | unset (on) | serve `GET /api/export` (includes message bodies) |
| `ALLOW_STATS_EXPORT` | unset (on) | serve `GET /api/export/stats` |
| `IMAP_HOST` / `IMAP_PORT` / `IMAP_USERNAME` / `IMAP_PASSWORD` | unset / `993` | needed only to pull mail |
| `PANGRAM_API_KEY` | unset | needed only to score |
| `WEB_WORKERS` / `WEB_THREADS` | `2` / `4` | gunicorn concurrency |
| `WEB_TIMEOUT` / `WEB_GRACEFUL_TIMEOUT` | `1800` / `30` | request and shutdown timeouts |
| `WEB_ACCESS_LOG` | `-` (stdout) | access-log destination; empty disables it |
| `WEB_FORWARDED_ALLOW_IPS` | `*` | proxies whose `X-Forwarded-*` headers are trusted |

The request timeout is long because `POST /api/pull`, `/api/extract`, `/api/score`
and `/api/import` each run for minutes on a real corpus; gunicorn's 30-second
default would kill the worker part-way through. An instance that only serves the
dashboard — `PUBLIC_READONLY=1`, with pulling and scoring run as Jobs — can set
it far lower.

Credentials belong in a `Secret`, never in the image or a manifest. An instance
that neither pulls nor scores needs none: the dashboard and the extraction stage
read only the database.

### Temporary space

`GET /api/export` builds the export into a temporary file and streams it, so the
process needs writable temporary space of about the size of the export (an
`emptyDir` at `/tmp`, or `TMPDIR` pointed at a directory on the data volume).
The response itself is streamed in 64 KB chunks and does not scale with the
export's size. `POST /api/import` likewise spools the upload to a temporary file
before importing it.

## Kubernetes

The one constraint that shapes every manifest: the store is a single SQLite
file, and SQLite in WAL mode supports concurrent access from one machine, not
from several. So the Deployment runs **one** replica, with
`strategy.type: Recreate` so a rollout never has two pods holding the same
volume, on a `ReadWriteOnce` claim. Scaling out is not a configuration change;
it would require a different store.

Create the namespace and, if the instance pulls or scores, the credentials:

```bash
kubectl create namespace mlac
kubectl -n mlac create secret generic mlac-credentials \
  --from-literal=IMAP_USERNAME="$IMAP_USERNAME" \
  --from-literal=IMAP_PASSWORD="$IMAP_PASSWORD" \
  --from-literal=PANGRAM_API_KEY="$PANGRAM_API_KEY"
```

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mlac-data
  namespace: mlac
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      # The database plus room for an import: an import is one transaction, and
      # its WAL grows to roughly the size of the resulting database, so allow
      # about 2.5x the final size to import into it. See docs/export-import.md.
      storage: 50Gi
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: mlac-config
  namespace: mlac
data:
  DATABASE_PATH: /data/mail.db
  LOG_LEVEL: INFO
  # A publicly reachable instance: reads work, every write is refused, and the
  # full corpus download (message bodies included) is off.
  PUBLIC_READONLY: "1"
  ALLOW_EXPORT: "0"
  ALLOW_STATS_EXPORT: "1"
  WEB_WORKERS: "2"
  WEB_THREADS: "4"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mlac-web
  namespace: mlac
spec:
  replicas: 1              # SQLite: one writer, one pod
  strategy:
    type: Recreate         # never two pods on the same volume during a rollout
  selector:
    matchLabels: {app: mlac-web}
  template:
    metadata:
      labels: {app: mlac-web}
    spec:
      securityContext:
        runAsUser: 1001
        runAsGroup: 1001
        runAsNonRoot: true
        fsGroup: 1001      # makes the volume writable by the image's user
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: web
          image: mailing-list-ai-check:1.15.3
          ports:
            - {name: http, containerPort: 8050}
          envFrom:
            - configMapRef: {name: mlac-config}
            - secretRef: {name: mlac-credentials, optional: true}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: tmp, mountPath: /tmp}
          # /api/capabilities never opens the database, so a probe cannot be
          # blocked by a long-running write holding the SQLite lock.
          startupProbe:
            httpGet: {path: /api/capabilities, port: http}
            periodSeconds: 5
            failureThreshold: 24
          readinessProbe:
            httpGet: {path: /api/capabilities, port: http}
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /api/capabilities, port: http}
            periodSeconds: 30
            timeoutSeconds: 5
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {memory: 1Gi}
      # Long enough for the graceful shutdown in docker/mlac-web to finish an
      # in-flight request that holds the write lock.
      terminationGracePeriodSeconds: 60
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: mlac-data}
        - name: tmp
          emptyDir: {sizeLimit: 8Gi}   # export/import spool space
---
apiVersion: v1
kind: Service
metadata:
  name: mlac-web
  namespace: mlac
spec:
  selector: {app: mlac-web}
  ports:
    - {name: http, port: 80, targetPort: http}
```

Expose the Service through whatever ingress the cluster uses. The app has no
authentication of its own: an instance reachable beyond the cluster should run
with `PUBLIC_READONLY=1`, and the ingress should supply access control if the
message text is not meant to be public.

### Pipeline stages as Jobs

The pipeline stages are batch work, not request handling, and each is
idempotent, so they run as Jobs against the same image and the same claim. A Job
and the web Deployment can hold a `ReadWriteOnce` claim at the same time only
when they are scheduled onto the same node; where they are not, scale the
Deployment to zero for the duration, or use a `ReadWriteOncePod`-aware plan that
keeps them together.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: mlac-pull
  namespace: mlac
spec:
  schedule: "17 * * * *"
  concurrencyPolicy: Forbid        # two pulls must not write at once
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: OnFailure
          securityContext:
            runAsUser: 1001
            runAsGroup: 1001
            runAsNonRoot: true
            fsGroup: 1001
          containers:
            - name: pull
              image: mailing-list-ai-check:1.15.3
              command: ["mail-ai-pull", "--all-lists", "--incremental"]
              envFrom:
                - configMapRef: {name: mlac-config}
                - secretRef: {name: mlac-credentials}
              volumeMounts:
                - {name: data, mountPath: /data}
          volumes:
            - name: data
              persistentVolumeClaim: {claimName: mlac-data}
```

`mail-ai-extract` and `mail-ai-score` follow the same shape.
`mail-ai-score` sends paid API calls: keep its `--limit` explicit and its
schedule deliberate.

### Loading an export into a deployed instance

An export file is loaded by `mail-ai-import`, which needs the file and the
database on the same filesystem. Copy the file onto the claim and run one Job:

```bash
# 1. get the file onto the volume (via the running web pod)
kubectl -n mlac cp mlac-export-all-20260819.jsonl.zst \
  "$(kubectl -n mlac get pod -l app=mlac-web -o name | head -1 | cut -d/ -f2)":/data/import.jsonl.zst

# 2. verify without writing, then import
kubectl -n mlac create job mlac-import-dryrun --image=mailing-list-ai-check:1.15.3 \
  -- mail-ai-import /data/import.jsonl.zst --dry-run
kubectl -n mlac create job mlac-import --image=mailing-list-ai-check:1.15.3 \
  -- mail-ai-import /data/import.jsonl.zst
```

`kubectl create job` does not attach the volume; write the Job as a manifest
with the same `volumeMounts`, `envFrom` and `securityContext` as the CronJob
above, and only the `command` changed. The import is one transaction and is
rolled back on any error, `--dry-run` runs the identical path, and importing the
same file twice is a no-op — so the safe order is dry run, then import, then
delete the file from the volume.

Import cost at scale is measured in [export-import.md](export-import.md): about
45 seconds for 100,000 messages, holding the write lock for about 27 of them,
with free space of roughly 2.5x the final database size required. Readers are
not blocked, so the dashboard stays usable throughout.

### Upgrading

Schema migrations are applied automatically by whichever process opens the
database first after the image changes, and they are one-way. Two practical
consequences:

- Copy the database file (with its `-wal` and `-shm` side-files) before rolling
  out a version whose migrations have not run against it. On a
  `ReadWriteOnce` claim that means a volume snapshot, or a `cp` inside the pod
  before the rollout.
- The first open after the upgrade holds the write lock for the migration and
  any backfill. `PRAGMA busy_timeout` is 120 seconds, so concurrent openers wait
  rather than fail. To keep that work out of the first user request, run it
  deliberately with a one-off Job on the new image before rolling out the
  Deployment:

  ```
  command: ["python", "-c", "import os; from mailing_list_ai_check.store import Store; Store(os.environ['DATABASE_PATH']).close()"]
  ```

The dashboard is built into the image, so it never lags the API the way a
checkout with a stale `frontend/dist` can.
