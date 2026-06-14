# Nydus Frontend Hand-off — 2026-06-14 migration

Migration doc for the backend changes made **after** the original `FRONTEND_HANDOFF.md` was handed
off. That file is frozen (a prior migration); everything new lives here. Four updates, in order:
**A. Custom domains**, **B. Process recovery & ports**, **C. Self-test concurrency & diagnostics**,
**D. Watchdog alerting toggle** (the latest change). Self-contained — build against this alone.

## How the frontend talks to the backend (unchanged)
- **JSON endpoint** → a server action in `app/actions/*.ts` calling `fetchWithAuth(endpoint, opts)`.
- **SSE (streaming) endpoint** → *additionally* a Next proxy route `app/api/**/route.ts` that injects
  auth and pipes `upstream.body` (`EventSource` can't set headers).
- **Base URL / auth split:** prod → `http://INTERNAL:4000/api` (no auth header); dev →
  `http://PUBLIC:5013/api` with `X-Auth-Key`. New routes are on both servers already.
- Errors are always `{ "error": "<msg>" }` with a real HTTP status. JSON keys are `snake_case`.

> **No new SSE proxy routes are needed for any of this.** Custom-domain deploys reuse the existing
> deploy-logs stream; everything else new is plain JSON (server actions only).

## Migrations to apply first (backend owner runs these by hand)
1. `migrations/2026-06-13_custom_domains.sql` — `deployments.dns_mode|fqdn|cf_zone_id`, `subdomain` nullable.
2. `migrations/2026-06-13_webhook_custom_domains.sql` — `webhook_projects.fqdn`.
Until applied, custom-domain deploys and the `fqdn`/`dns_mode` fields don't work; existing subdomain
deploys are unaffected (backfilled to `dns_mode:"subdomain"`). Updates B, C, D need **no** migration.

---

# A. Custom domains (`dns_mode`)

A deployment is no longer always `{subdomain}.arvo.team`. Each has a `dns_mode`:
- `subdomain` (default) — `{subdomain}.arvo.team`, DNS automated. **Unchanged behavior.**
- `cloudflare` — a custom domain in the **client's** Cloudflare zone (same account/token), automated.
- `external` — a custom domain whose DNS the client runs elsewhere; nydus does nginx + cert only.

### `POST /api/deploy` — now mode-aware
Always required: `project_uuid`, `github_pat`, `triggered_by`. Then by mode:
```jsonc
// subdomain (omit dns_mode):
{ "project_uuid":"…", "subdomain":"myapp", "github_pat":"ghp_…", "triggered_by":"<discord_id>" }
// cloudflare — full custom domain instead of subdomain:
{ "project_uuid":"…", "dns_mode":"cloudflare", "domain":"shop.client.com", "github_pat":"…", "triggered_by":"…" }
// external:
{ "project_uuid":"…", "dns_mode":"external", "domain":"client.com", "github_pat":"…", "triggered_by":"…" }
```
→ `202 {"run_id":"…"}` (stream via the **existing** deploy-logs SSE). `400` if the mode's required
field is missing or `domain` is invalid (bad hostname, or under `arvo.team` — those must use subdomain
mode). `404` unknown project.

- **`external` is a two-step flow for the user:** the domain must already point an A record at the
  server IP *before* deploying, or the run fails fast with a "point your DNS at <IP>, then redeploy"
  line in the log stream (no cert is attempted until DNS resolves). Surface this in the UI.
- `cloudflare` needs no pre-step. (Ops: the backend's Cloudflare token must have Zone:Read across the
  account, or a `cloudflare` deploy fails with "no authoritative zone".)

### Deployment object — new fields (additive)
```jsonc
{
  "subdomain": "myapp" | null,        // null for custom domains
  "fqdn": "myapp.arvo.team",          // ALWAYS present — the canonical hostname
  "dns_mode": "subdomain",            // "subdomain" | "cloudflare" | "external"
  "cf_zone_id": null                  // set only for dns_mode:"cloudflare"
  // …all existing fields unchanged…
}
```
**Render `fqdn` everywhere you currently build `{subdomain}.arvo.team`.** `GET /api/deployments/{uuid}/status`
also gains `dns_mode`; its `dns` block is `{"managed":false}` for `external`.

### Other effects
- `POST /api/deployments/{uuid}/dns/reconcile` → **`400`** for `external` ("DNS is client-managed…").
  Hide/skip the reconcile button for external deployments.
- **Webhooks now work for custom domains** (keyed on `fqdn`). Register/get/delete exactly as before.

### Frontend work for A
- `deploy` action + form: add a mode selector and a `domain` input for the custom modes; send
  `dns_mode`/`domain`. Default stays subdomain.
- Deployment list/detail: show `fqdn` (badge `dns_mode`); tolerate `subdomain: null`.
- Hide DNS-reconcile for `external`; show the "point your A record first" note for `external`.

---

# B. Process recovery & ports

After a reboot/`pm2` loss every app process can vanish (nginx then 502s, watchdog says "process not
found"). The backend can now bring them back on their correct ports.

### `POST /api/deployments/{uuid}/process` — restart now recovers
Body `{ "action":"start|stop|restart|reload|flush" }` (default `restart`). `start`/`restart`/`reload`
now **recreate** the process if pm2 lost it, on the deployment's `assigned_port` (not just poke a
process that's gone). → `200 {"status","detail"}` | `400`.

### `POST /api/server/recover` (NEW) — one-click "everything's down"
No body. Recreates any lost active node deployment + enabled `pm2` managed service on its stored
port/path; leaves healthy ones alone; persists the pm2 list afterward. → `200`:
```jsonc
{ "status":"ok|partial", "recovered": 7, "failed": 0,
  "report": [ {"target":"arvo.team","pm2_name":"arvo.team","ok":true,"detail":"recreated"},
              {"target":"myapp.arvo.team","pm2_name":"abc123def456","ok":true,"detail":"already online"} ] }
```

### Managed services need `port` + `deploy_path` to be recoverable
A `pm2` managed service (e.g. **arvo.team → `/var/www/arvo.team`, port `3001`**) can only be
*recreated* if its row carries `deploy_path` + `port`. `GET /api/server/discover` →
`nginx_sites[].ports` are the on-disk source of truth for ports.

### `PUT /api/services/{service_uuid}` (NEW) — patch a managed service
Patch any of `name, service_type, pm2_name, systemd_unit, fqdn, health_url, deploy_path, port,
git_url, branch, enabled`. Use it to backfill `deploy_path`+`port` on existing rows (re-`POST`ing the
same `name` fails — `name` is UNIQUE). → `200` updated ManagedService | `404` | `400`.

### Frontend work for B
- A "Recover all" button (admin) → `POST /api/server/recover` → show the `report`.
- Managed-service edit form (new) → `PUT /api/services/{uuid}`, with `deploy_path` + `port` fields;
  prompt to fill them for `pm2` services so recovery/monitoring work.

---

# C. Self-test concurrency & diagnostics

### Self-test is single-flight across **all** admins (Discord + dashboard)
`POST /api/selftest` (body `{variants?, cert_staging?}`) → `202 {status,run_id,variants,cert_staging,log_stream}`
on success. When one is already running → **`409`** identifying the live run:
```jsonc
{ "error":"A self-test is already running (started by 123…, 40s ago, run abc…). Watch that run…",
  "active": { "run_id":"abc…", "started_by":"123…", "started_at":"2026-06-14T…",
              "age_seconds":40, "log_stream":"/api/deploy/logs/abc…" } }
```
`503` if the deploy module is down. A wedged run auto-releases after 45 min.

**`GET /api/selftest` (NEW)** — poll for button state → `{ "running": false, "active": null }` or
`{ "running": true, "active": { run_id, started_by, started_at, age_seconds, log_stream } }`.

Frontend: before showing "Run self-test", call `GET /api/selftest`; if `running`, disable the button
and offer **Watch** → `EventSource(active.log_stream)` (existing deploy-logs proxy). On `POST` `409`,
do the same from the returned `active` block.

### Diagnostics — "why is it down / how did it crash"
`pm2 logs` is empty for a dead process, so use these (plain JSON; work whether up/stopped/errored/lost):

**`GET /api/deployments/{uuid}/diagnostics` (NEW)** →
```jsonc
{
  "fqdn":"myapp.arvo.team", "stack":"node", "status":"unhealthy", "assigned_port":3133,
  "port_listening": false,                 // anything answering on the port? (node only)
  "process": {                             // null for static/laravel
    "pm2_name":"abc123def456", "known": true, "status":"errored",
    "restarts": 17, "unstable_restarts": 16, "exit_code": 1, "uptime": 1718200000000,
    "error_log": "…last ~120 lines of the app's stderr (the crash)…",
    "output_log": "…stdout tail…", "error_log_path":"/root/.pm2/logs/abc…-error.log"
  },
  "nginx_error_log": "…last ~120 lines of nginx errors…"
}
```

**`GET /api/services/{uuid}/diagnostics` (NEW)** — shape by `service_type`:
- `pm2` → `{ "service_type":"pm2", "process": { …same process object… } }`
- `systemd` → `{ "service_type":"systemd", "unit":"nydus", "status":"<systemctl status>", "journal":"<recent journalctl>" }`
- `nginx`/`static` → `{ "service_type":"…", "nginx_error_log":"…" }`

**Existing `GET /api/deployments/{uuid}/logs/app`** (SSE): when the process is **down**, it now streams
the persisted stderr + a `[diagnostics]` header (pm2 status/restarts/exit_code) then `[done]`, instead
of an empty live tail. When online, it's the live tail as before. No proxy change.

### Frontend work for C
- A **Diagnose** panel (server actions for the two diagnostics endpoints) shown when a deployment is
  `unhealthy`/`failed` or a service is down: render `process.status`/`exit_code`/`restarts` and a
  `<pre>` of `process.error_log`, plus `nginx_error_log`. For systemd show `status` + `journal`.
- The `app` log tab needs no change — it self-serves crash output when down.

---

# D. Watchdog alerting toggle  ← latest update

A reboot leaves every service momentarily down, which would otherwise fire one "Service down" alert
per service all at once. So the health watchdog now **starts with alerting disabled** and is turned on
once the fleet is stable. It also honors a **startup grace window** — even when enabled, it stays quiet
for the first ~5 min after it starts, so a reboot's transient downtime never storms. Detection keeps
running while alerting is off; when you enable it, anything genuinely still down alerts on the next tick.

### `GET /api/watchdog` (NEW)
```jsonc
{ "alerts_enabled": false, "alerting_now": false,   // alerting_now = enabled AND past the grace window
  "grace_seconds": 300, "grace_remaining_seconds": 0,
  "self_heal_enabled": false, "fail_threshold": 2 }
```

### `POST /api/watchdog` (NEW)
Body `{ "alerts_enabled": true }` (optionally `"self_heal_enabled": bool`) → `200` with the same status
object. In-memory: a bot/server restart returns to the default-off state — intentional, so every reboot
starts quiet until you re-enable.

### Frontend work for D
- A settings toggle "Watchdog alerts" backed by `GET`/`POST /api/watchdog`. Surface `alerting_now` vs
  `alerts_enabled` so the grace window is visible ("enabled — alerts begin in Ns").
- There's also a `/watchdog enabled:true|false` Discord command for admins.

**Typical operational flow:** reboot → everything down but **silent** → `POST /api/server/recover` →
once healthy, `POST /api/watchdog {alerts_enabled:true}`.

---

## Endpoint quick-reference (new/changed in this migration)
| Method & path | Type | Update |
|---|---|---|
| `POST /api/deploy` | JSON | A — +`dns_mode`/`domain` |
| `POST /api/deployments/{uuid}/dns/reconcile` | JSON | A — `400` for `external` |
| Deployment object / `…/status` | JSON | A — +`fqdn`,`dns_mode`,`cf_zone_id`; `subdomain` nullable |
| `POST /api/deployments/{uuid}/process` | JSON | B — restart recreates on port |
| `POST /api/server/recover` | JSON | B — NEW |
| `PUT /api/services/{uuid}` | JSON | B — NEW |
| `GET /api/selftest` | JSON | C — NEW |
| `POST /api/selftest` | JSON | C — `409` now returns `active` |
| `GET /api/deployments/{uuid}/diagnostics` | JSON | C — NEW |
| `GET /api/services/{uuid}/diagnostics` | JSON | C — NEW |
| `GET /api/deployments/{uuid}/logs/app` | SSE | C — crash output when down |
| `GET` / `POST /api/watchdog` | JSON | D — NEW (alerts off by default) |

New server actions: `deploy` (mode args), `recoverServer`, `updateService`, `getSelftestStatus`,
`getDeploymentDiagnostics`, `getServiceDiagnostics`, `getWatchdog`/`setWatchdog`. No new SSE proxy routes.
