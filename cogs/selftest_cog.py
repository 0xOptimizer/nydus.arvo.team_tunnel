"""
In-product self-test harness for the nydus deployment pipeline.

Exercises the *real* pipeline end-to-end against hermetic, bot-generated git
fixtures (no external repos, zero npm dependencies — Node built-ins only) using
Let's Encrypt **staging** certs, then tears everything back down. It is the
single command/endpoint the team can run after a change to confirm deploy →
build → nginx → DNS → cert → pm2 → health, plus rebuild, automatic rollback,
and the GitHub webhook, all still work.

How it streams: a self-test run registers a queue in DeploymentCog's
`_active_streams` keyed by its run id, so the existing SSE endpoint
`GET /api/deploy/logs/{run_uuid}` (and the `/logs` slash command) stream its
progress with no new transport. Sub-runs (each deploy / rebuild) are relayed
line-by-line into that same stream.

Surfaces:
  - Discord slash command  `/selftest [variants]`
  - HTTP                   `POST /api/selftest`  -> 202 {run_id}

Everything created (deploy dirs, nginx configs, Cloudflare A records, staging
certs, pm2 processes, deployment/webhook/project rows, and the local fixtures)
is removed in a best-effort teardown, even if a step fails midway.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import uuid as uuid_lib

import aiohttp
import discord
from discord.ext import commands

from database.db import (
    add_github_project,
    create_new_webhook_project,
    delete_webhook_project,
    get_deployment_by_uuid,
    get_deployment_log_by_run,
    get_live_deployment_by_subdomain,
    remove_github_project,
)

_SELFTEST_DIR = os.getenv('SELFTEST_REPO_DIR', '/var/lib/nydus/selftest')
_DEV_ID = int(os.getenv('DEV_ID', '0'))

# Per-line timeout while relaying a sub-run's stream. A healthy deploy emits
# frequently (git/npm/nginx/dns/certbot), so a long gap means the sub-run hung.
_SUBRUN_LINE_TIMEOUT = 600.0
# How long the finished self-test stream lingers before cleanup, so a slightly
# late SSE client can still attach and replay the [done] sentinel.
_STREAM_LINGER = 90.0

# Canonical step order. Steps after 'node' operate on the node deployment, so
# 'node' must run (and succeed) before rebuild/rollback/webhook.
_ALL_VARIANTS = ['static', 'node', 'rebuild', 'webhook', 'rollback']
_NODE_DEPENDENT = {'node', 'rebuild', 'webhook', 'rollback'}


class SelfTestError(Exception):
    pass


# ---------------------------------------------------------------------------
# Fixtures — emitted to disk at run time. Hermetic: Node built-ins only, so
# `npm install` is a no-op and the build/run steps need nothing off the network.
# ---------------------------------------------------------------------------

_NODE_PKG = json.dumps({
    "name": "selftest-node",
    "version": "1.0.0",
    "private": True,
    "scripts": {"build": "node build.js", "start": "node server.js"},
})

_STATIC_PKG = json.dumps({
    "name": "selftest-static",
    "version": "1.0.0",
    "private": True,
    "scripts": {"build": "node build.js"},
})

_STATIC_BUILD_JS = (
    "const fs = require('fs');\n"
    "const path = require('path');\n"
    "fs.mkdirSync('dist', { recursive: true });\n"
    "fs.writeFileSync(path.join('dist', 'index.html'),\n"
    "  '<!doctype html><html><head><title>selftest-static</title></head>'\n"
    "  + '<body>selftest-static ok</body></html>');\n"
    "console.log('selftest-static build ok');\n"
)


def _node_server_js(status: int, tag: str) -> str:
    """A one-file HTTP server that binds process.env.PORT and returns `status`."""
    return (
        "const http = require('http');\n"
        "const port = process.env.PORT || 3000;\n"
        "const server = http.createServer((req, res) => {\n"
        f"  res.writeHead({status}, {{ 'Content-Type': 'text/plain' }});\n"
        f"  res.end('selftest-node {tag}\\n');\n"
        "});\n"
        f"server.listen(port, () => console.log('selftest-node {tag} on ' + port));\n"
    )


def _node_files(status: int, tag: str) -> dict:
    return {
        'package.json': _NODE_PKG,
        'build.js': "console.log('selftest-node build ok');\n",
        'server.js': _node_server_js(status, tag),
    }


_STATIC_FILES = {'package.json': _STATIC_PKG, 'build.js': _STATIC_BUILD_JS}


class SelfTestCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('nydus')
        # Serialize runs: a self-test does heavy, real server work (pm2/nginx/
        # certbot) and floods the logs; one at a time keeps it legible and safe.
        self._running = False

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------
    def queue_selftest(self, triggered_by: str, variants: list, cert_staging: bool = True):
        """Register a stream and fire the run. Returns run_id, or None if busy /
        the deployment module is unavailable."""
        dep = self.bot.get_cog('DeploymentCog')
        if not dep or self._running:
            return None
        self._running = True
        run_id = str(uuid_lib.uuid4())
        dep._active_streams[run_id] = asyncio.Queue()
        asyncio.create_task(self._run_selftest(run_id, triggered_by, variants, cert_staging))
        return run_id

    @staticmethod
    def parse_variants(value) -> list:
        """Normalize a slash/API 'variants' value into the canonical ordered list."""
        if not value or (isinstance(value, str) and value.strip().lower() in ('all', '*', '')):
            return list(_ALL_VARIANTS)
        if isinstance(value, str):
            requested = {v.strip().lower() for v in value.split(',') if v.strip()}
        else:
            requested = {str(v).strip().lower() for v in value}
        # Keep canonical order; always include 'node' if any node-dependent step
        # was asked for, otherwise those steps have nothing to act on.
        if requested & _NODE_DEPENDENT:
            requested.add('node')
        return [v for v in _ALL_VARIANTS if v in requested] or list(_ALL_VARIANTS)

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------
    async def _run_selftest(self, run_id: str, triggered_by: str, variants: list, cert_staging: bool):
        dep = self.bot.get_cog('DeploymentCog')
        log_lines: list[str] = []
        results: list[dict] = []
        token = uuid_lib.uuid4().hex[:8]
        root = os.path.join(_SELFTEST_DIR, token)
        created = {'deployments': [], 'subdomains': [], 'projects': [], 'webhooks': []}

        async def emit(line: str):
            await dep._emit(run_id, log_lines, line)

        async def step(name: str, ok: bool, detail: str = ''):
            results.append({'step': name, 'ok': bool(ok), 'detail': detail})
            await emit(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")

        node_fx = None
        node_uuid = None
        node_sub = None
        try:
            await emit(
                f"[SELFTEST] Starting (token={token}, staging={cert_staging}, "
                f"steps={','.join(variants)}). Fixtures under {root}."
            )

            # ---- STATIC deploy ----
            if 'static' in variants:
                try:
                    fx = await self._scaffold_repo(dep, root, 'static', _STATIC_FILES, emit)
                    sub = f"selftest-st-{token}"
                    created['subdomains'].append(sub)
                    await emit(f"[STATIC] Deploying {sub}...")
                    await self._deploy(dep, run_id, fx, sub, 'selftest', cert_staging, created, emit)
                    row = await get_live_deployment_by_subdomain(sub)
                    if row:
                        created['deployments'].append({'uuid': row['deployment_uuid'], 'sub': sub})
                    ok = bool(row) and row['status'] == 'active' and row['tech_stack'] == 'static'
                    await step('static-deploy', ok,
                               f"status={row and row.get('status')}, stack={row and row.get('tech_stack')}, "
                               f"port={row and row.get('assigned_port')}")
                except Exception as e:
                    await step('static-deploy', False, f"{type(e).__name__}: {e}")

            # ---- NODE deploy (prereq for rebuild/rollback/webhook) ----
            if 'node' in variants:
                try:
                    node_fx = await self._scaffold_repo(dep, root, 'node', _node_files(200, 'ok'), emit)
                    node_sub = f"selftest-nd-{token}"
                    created['subdomains'].append(node_sub)
                    await emit(f"[NODE] Deploying {node_sub}...")
                    await self._deploy(dep, run_id, node_fx, node_sub, 'selftest', cert_staging, created, emit)
                    row = await get_live_deployment_by_subdomain(node_sub)
                    if row:
                        node_uuid = row['deployment_uuid']
                        created['deployments'].append({'uuid': node_uuid, 'sub': node_sub})
                    ok = bool(row) and row['status'] == 'active' and row['tech_stack'] == 'node'
                    await step('node-deploy', ok,
                               f"status={row and row.get('status')}, stack={row and row.get('tech_stack')}, "
                               f"port={row and row.get('assigned_port')}")
                except Exception as e:
                    await step('node-deploy', False, f"{type(e).__name__}: {e}")

            # ---- REBUILD (healthy update) ----
            if 'rebuild' in variants:
                if node_uuid and node_fx:
                    try:
                        row = await get_deployment_by_uuid(node_uuid)
                        deploy_path = row['deploy_path']
                        new_sha = await self._push_update(dep, node_fx, _node_files(200, 'ok-v2'),
                                                          'selftest: healthy update', emit)
                        rb = dep.queue_rebuild(node_uuid, 'selftest')
                        await self._relay(dep, rb, emit, 'rebuild')
                        log = await get_deployment_log_by_run(rb)
                        head = await self._rev_parse(dep, deploy_path)
                        ok = bool(log) and log.get('status') == 'success' and head == new_sha
                        await step('node-rebuild', ok,
                                   f"log={log and log.get('status')}, head={head[:8]} == new={new_sha[:8]}")
                    except Exception as e:
                        await step('node-rebuild', False, f"{type(e).__name__}: {e}")
                else:
                    await step('node-rebuild', False, 'skipped: node deploy did not succeed')

            # ---- WEBHOOK (HMAC ping / bad-sig / push -> rebuild) ----
            if 'webhook' in variants:
                if node_uuid and node_fx and node_sub:
                    try:
                        # Ensure the remote tip is a healthy commit so the webhook
                        # rebuild is clean regardless of any prior rollback step.
                        await self._push_update(dep, node_fx, _node_files(200, 'ok-webhook'),
                                                'selftest: webhook update', emit)
                        wh = await create_new_webhook_project(
                            name=f"selftest-{token}", repo_url=node_fx['git_url'], branch=node_fx['branch'],
                            tech_stack='node', subdomain=node_sub, cloudflare_id=None, nginx_port=None,
                        )
                        if not wh or not wh.get('webhook_uuid'):
                            raise SelfTestError('could not create webhook project')
                        wh_uuid = wh['webhook_uuid']
                        secret = wh['webhook_secret']
                        created['webhooks'].append(wh_uuid)
                        port = getattr(self.bot.get_cog('ApiCog'), 'internal_port', None) or int(os.getenv('PORT', 4000))

                        s_ping, _ = await self._fire_webhook(secret, wh_uuid, 'ping', None, port)
                        s_bad, _ = await self._fire_webhook('wrong-secret', wh_uuid, 'push',
                                                            'refs/heads/' + node_fx['branch'], port)
                        s_push, body = await self._fire_webhook(secret, wh_uuid, 'push',
                                                                'refs/heads/' + node_fx['branch'], port)
                        await emit(f"[WEBHOOK] ping={s_ping} bad-signature={s_bad} push={s_push}")
                        # Let the queued rebuild finish so teardown doesn't race it.
                        rid = body.get('run_id') if isinstance(body, dict) else None
                        if rid:
                            await self._relay(dep, rid, emit, 'webhook-rebuild')
                        ok = s_ping == 200 and s_bad == 401 and s_push == 202
                        await step('webhook', ok,
                                   f"ping={s_ping}(200) bad-sig={s_bad}(401) push={s_push}(202)")
                    except Exception as e:
                        await step('webhook', False, f"{type(e).__name__}: {e}")
                else:
                    await step('webhook', False, 'skipped: node deploy did not succeed')

            # ---- ROLLBACK (unhealthy push auto-reverts) — last, leaves remote broken ----
            if 'rollback' in variants:
                if node_uuid and node_fx:
                    try:
                        row = await get_deployment_by_uuid(node_uuid)
                        deploy_path = row['deploy_path']
                        good_sha = await self._rev_parse(dep, deploy_path)
                        broken_sha = await self._push_update(dep, node_fx, _node_files(500, 'broken'),
                                                             'selftest: break the build', emit)
                        rb = dep.queue_rebuild(node_uuid, 'selftest')
                        await self._relay(dep, rb, emit, 'rollback')
                        log = await get_deployment_log_by_run(rb)
                        head = await self._rev_parse(dep, deploy_path)
                        # A correct rollback restores the previous good commit: the
                        # deployed tree is back at good_sha (not the broken push) and
                        # the run still ends 'success' (site healthy after revert).
                        ok = (head == good_sha and head != broken_sha
                              and bool(log) and log.get('status') == 'success')
                        await step('node-rollback', ok,
                                   f"head={head[:8]} good={good_sha[:8]} broken={broken_sha[:8]} "
                                   f"log={log and log.get('status')}")
                    except Exception as e:
                        await step('node-rollback', False, f"{type(e).__name__}: {e}")
                else:
                    await step('node-rollback', False, 'skipped: node deploy did not succeed')

        except Exception as e:
            self.logger.exception(f"Self-test crashed [{run_id}]: {e}")
            await emit(f"[FATAL] Self-test crashed: {e}")
        finally:
            await self._teardown(dep, run_id, log_lines, created, root, emit)
            passed = sum(1 for r in results if r['ok'])
            total = len(results)
            await emit(f"[SELFTEST] Complete: {passed}/{total} steps passed.")
            await emit("[RESULT] " + json.dumps(
                {'token': token, 'passed': passed, 'total': total, 'ok': passed == total and total > 0,
                 'steps': results}
            ))
            await self._notify_summary(passed, total, results)
            # End the stream cleanly, then linger briefly before dropping it.
            q = dep._active_streams.get(run_id)
            if q:
                await q.put(None)
            self._running = False
            await asyncio.sleep(_STREAM_LINGER)
            dep._active_streams.pop(run_id, None)

    # ------------------------------------------------------------------
    # Deploy / rebuild plumbing
    # ------------------------------------------------------------------
    async def _deploy(self, dep, parent_run_id, fx, subdomain, triggered_by, cert_staging, created, emit):
        """Create a backing project row, then run a real deploy whose stream is
        relayed live into the parent self-test stream. Awaits completion."""
        project_uuid = await add_github_project(
            name=f"selftest-{subdomain}", owner='nydus-selftest', owner_discord_id='0',
            owner_type='User', description='nydus self-test fixture', url_path=subdomain,
            git_url=fx['git_url'], ssh_url='', visibility='private', branch=fx['branch'],
        )
        if not project_uuid:
            raise SelfTestError('could not create backing project row')
        created['projects'].append(project_uuid)

        project_data = {
            'project_uuid': project_uuid,
            'name': f"selftest-{subdomain}",
            'git_url': fx['git_url'],
            'default_branch': fx['branch'],
        }
        sub_run = str(uuid_lib.uuid4())
        dep._active_streams[sub_run] = asyncio.Queue()
        relay = asyncio.create_task(self._relay(dep, sub_run, emit, 'deploy'))
        try:
            await dep.deploy_project(sub_run, project_data, subdomain, '', triggered_by, cert_staging)
        finally:
            q = dep._active_streams.get(sub_run)
            if q:
                await q.put(None)  # deploy_project doesn't emit the end-sentinel; we do
            try:
                await relay
            except Exception:
                pass
            dep._active_streams.pop(sub_run, None)

    async def _relay(self, dep, sub_run_id, emit, prefix):
        """Drain a sub-run's stream into the parent stream until its None sentinel."""
        q = dep.get_stream(sub_run_id)
        if not q:
            await emit(f"[{prefix}] (no stream for sub-run {sub_run_id[:8]})")
            return
        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=_SUBRUN_LINE_TIMEOUT)
            except asyncio.TimeoutError:
                await emit(f"[{prefix}] timed out waiting for output; abandoning relay.")
                return
            if line is None:
                return
            await emit(f"  {prefix}| {line}")

    # ------------------------------------------------------------------
    # Git fixture scaffolding
    # ------------------------------------------------------------------
    async def _git(self, dep, args, cwd=None, allow_fail=False):
        code, out, err = await dep.run_exec(
            ['git'] + args, cwd=cwd, env_extra={'GIT_TERMINAL_PROMPT': '0'}, timeout=120
        )
        if code != 0 and not allow_fail:
            raise SelfTestError(f"git {' '.join(args)} failed (exit {code}): {(err or out).strip()[:300]}")
        return code, out, err

    async def _write_files(self, base, files):
        loop = asyncio.get_running_loop()

        def _w():
            for rel, content in files.items():
                p = os.path.join(base, rel)
                d = os.path.dirname(p)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(p, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)

        await loop.run_in_executor(None, _w)

    async def _scaffold_repo(self, dep, root, name, files, emit, branch='main'):
        """Build a bare 'remote' repo plus a working tree that pushes to it, so the
        deploy can `file://`-clone it and later steps can push new commits."""
        repo_git = os.path.join(root, f"{name}.git")
        work = os.path.join(root, f"{name}-work")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: os.makedirs(work, exist_ok=True))

        await self._git(dep, ['init', work])
        await self._git(dep, ['-C', work, 'config', 'user.email', 'selftest@arvo.team'])
        await self._git(dep, ['-C', work, 'config', 'user.name', 'nydus-selftest'])
        await self._write_files(work, files)
        await self._git(dep, ['-C', work, 'add', '-A'])
        await self._git(dep, ['-C', work, '-c', 'commit.gpgsign=false', 'commit', '-m', 'selftest: initial'])
        await self._git(dep, ['-C', work, 'branch', '-M', branch])
        await self._git(dep, ['init', '--bare', repo_git])
        await self._git(dep, ['-C', repo_git, 'symbolic-ref', 'HEAD', f'refs/heads/{branch}'])
        await self._git(dep, ['-C', work, 'remote', 'add', 'origin', repo_git])
        await self._git(dep, ['-C', work, 'push', '-u', 'origin', branch])
        await emit(f"[SCAFFOLD] {name}: bare repo + working tree ready on '{branch}'.")
        return {'repo_git': repo_git, 'work': work, 'git_url': f"file://{repo_git}", 'branch': branch}

    async def _push_update(self, dep, fx, files, message, emit):
        work = fx['work']
        await self._write_files(work, files)
        await self._git(dep, ['-C', work, 'add', '-A'])
        await self._git(dep, ['-C', work, '-c', 'commit.gpgsign=false', 'commit', '-m', message])
        await self._git(dep, ['-C', work, 'push', 'origin', fx['branch']])
        _, out, _ = await self._git(dep, ['-C', work, 'rev-parse', 'HEAD'])
        sha = out.strip()
        await emit(f"[PUSH] {message} -> {sha[:8]}")
        return sha

    async def _rev_parse(self, dep, cwd):
        code, out, _ = await dep.run_exec(['git', 'rev-parse', 'HEAD'], cwd=cwd, timeout=30)
        return out.strip() if code == 0 else ''

    # ------------------------------------------------------------------
    # Webhook firing
    # ------------------------------------------------------------------
    async def _fire_webhook(self, secret, webhook_uuid, event, ref, port):
        """POST a GitHub-shaped, HMAC-signed delivery to the internal webhook route.
        Returns (status, parsed_json_or_empty)."""
        payload = {'ref': ref} if ref else {'zen': 'nydus self-test ping'}
        body = json.dumps(payload).encode()
        sig = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            'X-Hub-Signature-256': sig,
            'X-GitHub-Event': event,
            'Content-Type': 'application/json',
        }
        url = f"http://127.0.0.1:{port}/webhook/{webhook_uuid}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=body, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    try:
                        parsed = await resp.json()
                    except Exception:
                        parsed = {}
                    return resp.status, parsed
        except Exception as e:
            return 0, {'error': str(e)}

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    async def _teardown(self, dep, run_id, log_lines, created, root, emit):
        await emit("[TEARDOWN] Removing self-test resources...")
        # Full per-deployment teardown (pm2 + nginx + Cloudflare + staging cert + dir + row).
        for d in created['deployments']:
            try:
                ok, msg = await dep.delete_deployment(d['uuid'])
                await emit(f"[TEARDOWN] deployment {d['sub']}: {msg}")
            except Exception as e:
                await emit(f"[TEARDOWN] deployment {d['sub']} error: {e}")
        # Sweep any subdomain that never produced a tracked row (e.g. a deploy that
        # failed before persisting): idempotent reclaim clears stray nginx/DNS/cert/dir.
        for sub in created['subdomains']:
            try:
                if not await get_live_deployment_by_subdomain(sub):
                    await dep._reclaim_subdomain(sub, run_id, log_lines, '')
            except Exception as e:
                await emit(f"[TEARDOWN] reclaim {sub} error: {e}")
        for wh in created['webhooks']:
            try:
                await delete_webhook_project(wh)
            except Exception as e:
                await emit(f"[TEARDOWN] webhook {wh[:8]} error: {e}")
        for p in created['projects']:
            try:
                await remove_github_project(p)
            except Exception as e:
                await emit(f"[TEARDOWN] project {p[:8]} error: {e}")
        # Local fixtures.
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: shutil.rmtree(root, ignore_errors=True))
            await emit(f"[TEARDOWN] removed fixtures at {root}.")
        except Exception as e:
            await emit(f"[TEARDOWN] fixture removal error: {e}")

    async def _notify_summary(self, passed, total, results):
        """Record an alert with the run outcome (Discord only when something failed)."""
        out = self.bot.get_cog('OutputCog')
        if not out:
            return
        failed = [r['step'] for r in results if not r['ok']]
        level = 'success' if not failed and total else 'error'
        try:
            await out.alert(
                level, 'Self-test complete',
                f"{passed}/{total} steps passed." + (f" Failed: {', '.join(failed)}." if failed else ''),
                fields={'Passed': f"{passed}/{total}"},
                source='selftest', critical=bool(failed),
            )
        except Exception:
            self.logger.debug("OutputCog alert failed for self-test summary", exc_info=True)

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------
    @commands.slash_command(name="selftest", description="Run the nydus deployment self-test (LE staging)")
    async def slash_selftest(self, ctx: discord.ApplicationContext, variants: str = 'all'):
        if ctx.author.id != _DEV_ID:
            await ctx.respond("You are not authorized to use this command.", ephemeral=True)
            return
        await ctx.respond("Starting self-test...", ephemeral=True)
        run_id = self.queue_selftest(str(ctx.author.id), self.parse_variants(variants), cert_staging=True)
        if not run_id:
            await ctx.send_followup(
                "Could not start: a self-test is already running, or the deployment module is unavailable.",
                ephemeral=True,
            )
            return
        await ctx.send_followup(
            f"Self-test started. Run ID: `{run_id}`\n"
            f"Watch with `/logs {run_id}` or stream `GET /api/deploy/logs/{run_id}`.",
            ephemeral=True,
        )


def setup(bot):
    bot.add_cog(SelfTestCog(bot))
