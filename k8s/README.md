# Kubernetes manifests

The manifests that run the production image (`make image`, the `prod` target of
the repo's `Dockerfile`) as a single-instance deployment. Background, the
environment variables the image reads, and the pipeline stages as Jobs are in
[../docs/deployment.md](../docs/deployment.md).

| File | Object | Purpose |
| --- | --- | --- |
| `namespace.yaml` | `Namespace mlac` | the namespace every other object is in |
| `pvc.yaml` | `PersistentVolumeClaim mlac-data` | 50Gi `ReadWriteOnce` claim holding `/data/mail.db` |
| `configmap.yaml` | `ConfigMap mlac-config` | non-secret configuration, read as environment variables |
| `deployment.yaml` | `Deployment mlac-web` | one gunicorn pod serving the dashboard and API on port 8050 |
| `service.yaml` | `Service mlac-web` | `ClusterIP` on port 80, targeting the pod's `http` port |
| `kustomization.yaml` | — | applies the five in dependency order |

## Prerequisites

1. The image. `.github/workflows/build.yml` publishes it to
   `ghcr.io/ietf-tools/mailing-list-ai-check` on every push to `main` and every
   `v*` tag, which is the reference `deployment.yaml` carries; a private cluster
   also needs an `imagePullSecret` for that registry. `make image` builds the
   same target locally, tagged `mailing-list-ai-check:<version>`, for a cluster
   that pulls from somewhere else.
2. A default `StorageClass` that provisions `ReadWriteOnce` volumes, or an
   explicit `storageClassName` in `pvc.yaml`.
3. Credentials, only if the instance pulls mail or scores text. They are not in
   this directory and must not be added to it: create the Secret out of band.

```bash
kubectl create namespace mlac
kubectl -n mlac create secret generic mlac-credentials \
  --from-literal=IMAP_HOST="$IMAP_HOST" \
  --from-literal=IMAP_USERNAME="$IMAP_USERNAME" \
  --from-literal=IMAP_PASSWORD="$IMAP_PASSWORD" \
  --from-literal=PANGRAM_API_KEY="$PANGRAM_API_KEY"
```

The Deployment reads that Secret with `optional: true`, so an instance that only
serves an imported corpus runs without it: the dashboard and the extraction
stage read the database alone.

## Applying

```bash
kubectl apply -k k8s/
kubectl -n mlac rollout status deployment/mlac-web
kubectl -n mlac port-forward svc/mlac-web 8050:80    # then open http://127.0.0.1:8050
```

Expose the Service through whatever ingress the cluster uses. The application
has no authentication of its own; `configmap.yaml` therefore sets
`PUBLIC_READONLY=1`, which refuses every request other than a `GET` with a 403,
and `ALLOW_EXPORT=0`, which refuses the full corpus download. An ingress should
supply access control if the message text is not meant to be public.

## Constraints these manifests encode

- **One replica, `strategy: Recreate`.** The store is a single SQLite file.
  WAL mode supports concurrent access from one machine, not from several, so two
  pods must never hold the claim at once — including during the overlap of a
  rolling update. Raising `replicas` corrupts nothing immediately but is not a
  supported configuration; scaling out would require a different store.
- **A read-only root filesystem.** `/app` is root-owned in the image and the
  process runs as uid 1001, writing only to the `/data` mount and to `/tmp`.
  The `emptyDir` at `/tmp` is spool space for `GET /api/export` and
  `POST /api/import`, both of which stage a file before streaming it; size it to
  about one export.
- **Probes on `GET /api/capabilities`.** The one endpoint that reads the app
  config without opening the database, so a pull, an extraction run or an import
  holding the write lock cannot fail a liveness check. The startup probe allows
  120 seconds, matching the store's `PRAGMA busy_timeout`, which is the window
  in which the first open after an upgrade applies its migrations.
- **`terminationGracePeriodSeconds: 60`.** `docker/mlac-web` execs gunicorn as
  PID 1, so `SIGTERM` reaches it directly and in-flight requests finish within
  `WEB_GRACEFUL_TIMEOUT` (30s). The margin matters because a request may hold
  the SQLite write lock.

## What is not here

Pipeline stages. `mail-ai-pull`, `mail-ai-extract`, `mail-ai-score`,
`mail-ai-export`, `mail-ai-export-stats` and `mail-ai-import` are on `PATH` in
the same image and run as Jobs or CronJobs against the same claim, with the same
`securityContext`, `envFrom` and `volumeMounts` as the Deployment and only the
`command` changed. `docs/deployment.md` carries a worked CronJob, the import
procedure and the upgrade procedure. A Job and this Deployment can hold a
`ReadWriteOnce` claim simultaneously only when they are scheduled onto the same
node; where they are not, scale the Deployment to zero for the duration.
