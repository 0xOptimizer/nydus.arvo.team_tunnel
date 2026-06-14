import traceback
from discord.ext import commands, tasks
from aiohttp import web
import os
import hmac
import hashlib
import secrets
import json
import logging
import asyncio
import discord
from datetime import datetime, timedelta
from database.db import (
    get_recent_system_resources_with_averages, get_webhook_project_by_uuid, get_all_webhook_projects, create_new_webhook_project,
    get_webhook_project_by_subdomain, get_webhook_project_by_fqdn,
    delete_webhook_project, add_github_project, get_all_github_projects, get_all_attached_projects,
    remove_github_project, get_user, get_auth_key, validate_auth_key, execute_query,
    get_all_recent_backups,
    get_all_schedules, get_schedule_by_uuid, set_schedule_enabled, set_schedule_next_run, create_schedule_log,
    get_all_deployments, get_deployment_by_uuid, update_deployment,
    get_live_deployment_by_subdomain, get_live_deployment_by_fqdn, get_active_deployments,
    create_managed_service, get_managed_service, get_all_managed_services,
    update_managed_service, delete_managed_service,
    get_alerts, get_unacknowledged_alert_count, acknowledge_alert, acknowledge_all_alerts,
    create_tusd_upload, create_tusd_upload_meta, update_tusd_upload,
)
import jwt
import bcrypt
import re
import aiomysql
import uuid as uuid_lib
import shutil
import ipaddress
from typing import Optional
from utils.domains import fqdn_of

def json_serial(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

# ------------------------------
# TUSD <START>
# ------------------------------

UPLOAD_DESTINATIONS = {
    "general": "/var/data/uploads",
    "phpmyadmin": "/var/www/phpmyadmin/uploads",
}

MAX_FILENAME_LENGTH = 255
MAX_FILETYPE_LENGTH = 128
MAX_METADATA_PAIRS = 50
MAX_META_KEY_LENGTH   = 64
MAX_META_VALUE_LENGTH = 512

_SAFE_UPLOAD_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,128}$')


def is_valid_uuid(uuid_str: str) -> bool:
    """
    Validate that an upload id is safe to use inside a filesystem path.

    Not strictly UUIDv4 (tusd generates its own id format, which is why the original
    UUIDv4 check was disabled), but a bounded alphanumeric/dash/underscore token —
    enough to block path traversal, null bytes, and injection via the upload id.
    """
    return bool(uuid_str) and bool(_SAFE_UPLOAD_ID_RE.match(uuid_str))

def secure_filename(filename: str) -> str:
    """Return a safe version of the filename (no path separators, null bytes)."""
    # Block null bytes and path traversal sequences
    if '\0' in filename or '/' in filename or '\\' in filename:
        raise ValueError("Invalid characters in filename")
    # Remove any leading/trailing whitespace
    filename = filename.strip()
    if not filename:
        raise ValueError("Empty filename")
    return filename

def _extract_ip(remote_addr: str) -> Optional[str]:
    if not remote_addr:
        return None
    try:
        host, _, _ = remote_addr.rpartition(":")
        host = host.strip("[]")
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return None


def _unique_path(directory: str, filename: str) -> str:
    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate
    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{base}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1

# ------------------------------
# TUSD <END>
# ------------------------------

# ------------------------------
# UTILITIES FOR DEMO PURPOSES <START>
# ------------------------------

def _decode_attendance_jwt(self, request):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:]
    try:
        return jwt.decode(token, os.environ['ATTENDANCE_JWT_SECRET'], algorithms=['HS256'])
    except jwt.PyJWTError:
        return None

# ------------------------------
# UTILITIES FOR DEMO PURPOSES <END>
# ------------------------------

class ApiCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('nydus')

        # Internal server
        self.internal_app = web.Application()
        self.internal_port = int(os.getenv('PORT', 4000))
        self.internal_runner = None
        self.internal_site = None

        # Public server
        self.public_app = web.Application(middlewares=[self.public_auth_middleware])
        self.public_port = int(os.getenv('PUBLIC_PORT', 5013))
        self.public_runner = None
        self.public_site = None
        self.public_enabled = False

        self.setup_routes()
        self.start_internal_server.start()

    def cog_unload(self):
        self.start_internal_server.cancel()
        if self.public_enabled:
            asyncio.create_task(self.stop_public_server())

    # ------------------------------
    # ROUTES
    # ------------------------------
    def _add_route(self, method: str, path: str, handler):
        self.internal_app.router.add_route(method, path, handler)
        self.public_app.router.add_route(method, path, handler)

    def setup_routes(self):
        self._add_route('OPTIONS', '/{tail:.*}', self.handle_options)
        self._add_route('POST', '/api/auth/check-user', self.handle_check_user)
        self._add_route('GET', '/api/stats', self.handle_get_system_resources)
        self._add_route('GET', '/api/cloudflare/records', self.handle_get_dns_records)
        self._add_route('POST', '/api/cloudflare/records', self.handle_create_dns_record)
        self._add_route('PUT', '/api/cloudflare/records/{record_id}', self.handle_update_dns_record)
        self._add_route('DELETE', '/api/cloudflare/records/{record_id}', self.handle_delete_dns_record)
        self._add_route('GET', '/api/cloudflare/analytics', self.handle_get_analytics)
        self._add_route('GET', '/api/cloudflare/dynamic-analytics', self.handle_get_dynamic_analytics)
        self._add_route('GET', '/api/github-projects', self.handle_get_github_projects)
        self._add_route('POST', '/api/github-projects', self.handle_create_github_project)
        self._add_route('DELETE', '/api/github-projects/{uuid}', self.handle_delete_github_project)
        self._add_route('GET', '/api/attached-projects', self.handle_get_attached_projects)
        self._add_route('POST', '/webhook/{uuid}', self.handle_webhook)
        self._add_route('GET', '/api/maintenance/logs/{service}', self.handle_get_logs)
        self._add_route('GET', '/api/maintenance/restart/{service}', self.handle_restart_service)
        self._add_route('POST', '/api/toggle-public', self.handle_toggle_public)
        self._add_route('GET', '/api/public-status', self.handle_public_status)
        self._add_route('GET', '/api/messenger', self.handle_messenger_verification)
        self._add_route('POST', '/api/messenger', self.handle_messenger_webhook)

        # Database management routes
        self._add_route('GET', '/api/databases/backups', self.handle_get_all_backups)
        self._add_route('GET', '/api/databases/schedules', self.handle_get_all_schedules)
        self._add_route('POST', '/api/databases/schedules/{schedule_uuid}/toggle', self.handle_toggle_schedule)
        self._add_route('POST', '/api/databases/schedules/{schedule_uuid}/run', self.handle_force_run_schedule)
        self._add_route('GET', '/api/databases', self.handle_get_databases)
        self._add_route('GET', '/api/databases/{uuid}', self.handle_get_database)
        self._add_route('POST', '/api/databases', self.handle_create_database)
        self._add_route('DELETE', '/api/databases/{uuid}', self.handle_delete_database)
        self._add_route('GET', '/api/databases/users', self.handle_get_database_users)
        self._add_route('POST', '/api/databases/users', self.handle_create_database_user)
        self._add_route('DELETE', '/api/databases/users/{user_uuid}', self.handle_delete_database_user)
        self._add_route('POST', '/api/databases/{uuid}/privileges', self.handle_grant_privileges)
        self._add_route('DELETE', '/api/databases/{uuid}/privileges/{user_uuid}', self.handle_revoke_privileges)
        self._add_route('POST', '/api/databases/{uuid}/backup', self.handle_perform_backup)
        self._add_route('POST', '/api/databases/{uuid}/restore', self.handle_restore_backup)
        self._add_route('GET', '/api/databases/{uuid}/privileges', self.handle_get_database_privileges)
        self._add_route('GET', '/api/databases/privileges', self.handle_get_all_privileges)
        self._add_route('GET', '/api/databases/users/{user_uuid}/credentials', self.handle_get_user_credentials)
        self._add_route('POST', '/api/databases/pma-token', self.handle_pma_token)
        self._add_route('POST', '/api/databases/quickgen', self.handle_db_quickgen)
        self._add_route('GET', '/api/databases/backups/{backup_uuid}/download', self.handle_download_backup)
        self._add_route('GET', '/api/databases/{uuid}/backups', self.handle_get_database_backups)

        # Control plane — server-wide
        self._add_route('GET', '/api/server/overview', self.handle_server_overview)
        self._add_route('GET', '/api/server/discover', self.handle_server_discover)
        self._add_route('POST', '/api/server/recover', self.handle_server_recover)
        # Watchdog down-alert toggle (off by default so a reboot doesn't alert-storm).
        self._add_route('GET', '/api/watchdog', self.handle_watchdog_status)
        self._add_route('POST', '/api/watchdog', self.handle_watchdog_set)

        # Alerts / notifications feed (frontend-first)
        self._add_route('GET', '/api/alerts', self.handle_list_alerts)
        self._add_route('GET', '/api/alerts/count', self.handle_alert_count)
        self._add_route('POST', '/api/alerts/ack-all', self.handle_ack_all_alerts)
        self._add_route('POST', '/api/alerts/{alert_uuid}/ack', self.handle_ack_alert)

        # Control plane — managed services (adopted/external)
        self._add_route('GET', '/api/services', self.handle_list_services)
        self._add_route('POST', '/api/services', self.handle_create_service)
        self._add_route('PUT', '/api/services/{service_uuid}', self.handle_update_service)
        self._add_route('DELETE', '/api/services/{service_uuid}', self.handle_delete_service)
        self._add_route('POST', '/api/services/{service_uuid}/process', self.handle_service_process)
        self._add_route('GET', '/api/services/{service_uuid}/logs', self.handle_service_logs)
        self._add_route('GET', '/api/services/{service_uuid}/diagnostics', self.handle_service_diagnostics)

        # Deployments
        self._add_route('GET', '/api/deployments', self.handle_list_deployments)
        self._add_route('GET', '/api/deployments/{deployment_uuid}', self.handle_get_deployment)
        # Control plane — per-deployment status/logs/config/actions
        self._add_route('GET', '/api/deployments/{deployment_uuid}/status', self.handle_deployment_status)
        self._add_route('GET', '/api/deployments/{deployment_uuid}/diagnostics', self.handle_deployment_diagnostics)
        self._add_route('GET', '/api/deployments/{deployment_uuid}/logs/{kind}', self.handle_deployment_logs)
        self._add_route('GET', '/api/deployments/{deployment_uuid}/config', self.handle_deployment_config)
        self._add_route('POST', '/api/deployments/{deployment_uuid}/process', self.handle_deployment_process)
        self._add_route('POST', '/api/deployments/{deployment_uuid}/nginx', self.handle_deployment_nginx)
        self._add_route('POST', '/api/deployments/{deployment_uuid}/ssl/renew', self.handle_deployment_ssl_renew)
        self._add_route('POST', '/api/deployments/{deployment_uuid}/dns/reconcile', self.handle_deployment_dns_reconcile)
        # Webhook management (register/inspect/remove GitHub auto-deploy for a deployment)
        self._add_route('GET', '/api/deployments/{deployment_uuid}/webhook', self.handle_get_webhook)
        self._add_route('POST', '/api/deployments/{deployment_uuid}/webhook', self.handle_create_webhook)
        self._add_route('DELETE', '/api/deployments/{deployment_uuid}/webhook', self.handle_delete_webhook)
        self._add_route('POST', '/api/deploy', self.handle_deploy)
        self._add_route('GET', '/api/deploy/logs/{run_uuid}', self.handle_stream_logs)
        self._add_route('POST', '/api/deploy/rebuild/{deployment_uuid}', self.handle_rebuild)
        # In-product self-test: exercises the real pipeline against hermetic
        # fixtures (LE staging) and streams via the deploy-logs SSE endpoint above.
        self._add_route('POST', '/api/selftest', self.handle_selftest)
        self._add_route('GET', '/api/selftest', self.handle_selftest_status)
        self._add_route('DELETE', '/api/deployments/{deployment_uuid}', self.handle_delete_deployment)
        self._add_route('GET', '/api/deployments/{deployment_uuid}/env', self.handle_get_env)
        self._add_route('PUT', '/api/deployments/{deployment_uuid}/env', self.handle_update_env)
        self._add_route('POST', '/api/deployments/{deployment_uuid}/env', self.handle_add_env)
        self._add_route('DELETE', '/api/deployments/{deployment_uuid}/env', self.handle_delete_env)

        # DEMO: School Attendance
        self._add_route('POST', '/api/attendance/login', self.handle_attendance_login)
        self._add_route('POST', '/api/attendance/qr-login', self.handle_attendance_qr_login)
        self._add_route('POST', '/api/attendance/qr-scan', self.handle_attendance_qr_scan)
        self._add_route('POST', '/api/attendance/clock', self.handle_attendance_clock)
        self._add_route('GET',  '/api/attendance/history', self.handle_attendance_history)

        # TUSD
        self._add_route('POST', '/tusd/upload-complete', self.handle_tusd_upload_complete)

    # ------------------------------
    # INTERNAL SERVER
    # ------------------------------
    @tasks.loop(count=1)
    async def start_internal_server(self):
        self.internal_runner = web.AppRunner(self.internal_app)
        await self.internal_runner.setup()
        self.internal_site = web.TCPSite(self.internal_runner, '0.0.0.0', self.internal_port)
        await self.internal_site.start()
        self.logger.info(f"Internal API server started on port {self.internal_port}")

    @start_internal_server.before_loop
    async def before_internal_server(self):
        await self.bot.wait_until_ready()

    # ------------------------------
    # PUBLIC SERVER
    # ------------------------------
    async def start_public_server(self):
        if self.public_enabled:
            self.logger.warning("Public server already running")
            return
        self.public_runner = web.AppRunner(self.public_app)
        await self.public_runner.setup()
        self.public_site = web.TCPSite(self.public_runner, '0.0.0.0', self.public_port)
        await self.public_site.start()
        self.public_enabled = True
        self.logger.info(f"Public API server started on port {self.public_port}")

    async def stop_public_server(self):
        if not self.public_enabled or not self.public_runner:
            self.logger.warning("Public server not running")
            return
        await self.public_runner.cleanup()
        self.public_runner = None
        self.public_site = None
        self.public_enabled = False
        self.logger.info("Public API server stopped")

    # ------------------------------
    # MIDDLEWARE
    # ------------------------------
    @web.middleware
    async def public_auth_middleware(self, request, handler):
        if request.path.startswith("/api/"):
            auth_key = request.headers.get("X-Auth-Key")
            success = False
            try:
                if not auth_key:
                    return self.json_response({'error': 'Missing X-Auth-Key'}, status=401)

                result = await validate_auth_key(auth_key)
                success = result['valid']

                if not success:
                    return self.json_response({'error': result['reason']}, status=403)

                request['auth_key_data'] = result['data']
                return await handler(request)

            finally:
                await execute_query(
                    "INSERT INTO auth_key_usage (auth_key_secret, endpoint, method, is_success) VALUES (%s, %s, %s, %s)",
                    (auth_key or "MISSING", request.path, request.method, int(success))
                )
        else:
            return await handler(request)

    # ------------------------------
    # TOGGLE ENDPOINT
    # ------------------------------
    async def handle_toggle_public(self, request):
        try:
            data = await request.json()
            action = data.get('action')
            if action == 'start':
                await self.start_public_server()
                return self.json_response({'status': 'public server started'})
            elif action == 'stop':
                await self.stop_public_server()
                return self.json_response({'status': 'public server stopped'})
            else:
                return self.json_response({'error': 'Invalid action, use "start" or "stop"'}, status=400)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)
    
    async def handle_public_status(self, request):
        return self.json_response({'running': self.public_enabled})

    # ------------------------------
    # COMMON HELPERS
    # ------------------------------
    def json_response(self, data, status=200):
        return web.json_response(
            data,
            status=status,
            headers={'Access-Control-Allow-Origin': '*'},
            dumps=lambda obj: json.dumps(obj, default=json_serial)
        )

    async def handle_options(self, request):
        return web.Response(status=200, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, X-Auth-Key'
        })

    async def _log_to_discord(self, title, message, color=discord.Color.blue()):
        output_cog = self.bot.get_cog("OutputCog")
        if output_cog:
            await output_cog.send_embed(title=title, description=message, color=color)

    # ------------------------------
    # AUTHENTICATION
    # ------------------------------
    async def handle_check_user(self, request):
        try:
            data = await request.json()
            discord_id = data.get('discord_id')
            if not discord_id:
                return self.json_response({'error': 'Missing discord_id'}, status=400)
            user = await get_user(discord_id)
            if user:
                return self.json_response({'exists': True})
            return self.json_response({'error': 'User not found'}, status=401)
        except Exception:
            return self.json_response({'error': 'Internal Server Error'}, status=500)

    # ------------------------------
    # STATISTICS
    # ------------------------------
    async def handle_get_system_resources(self, request):
        try:
            stats = await get_recent_system_resources_with_averages()
            if not stats:
                return self.json_response({'error': 'No data available'}, status=404)
                
            return self.json_response(stats)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    # ------------------------------
    # DEPLOYMENTS (placeholder)
    # ------------------------------

    # ------------------------------
    # REPOSITORIES
    # ------------------------------
    async def handle_get_github_projects(self, request):
        projects = await get_all_github_projects()
        return self.json_response(projects)

    async def handle_create_github_project(self, request):
        try:
            data = await request.json()
            owner_id = data.get('owner_discord_id')
            if not owner_id:
                return self.json_response({'error': 'Missing owner_discord_id'}, status=400)
            project_uuid = await add_github_project(
                name=data.get('name'),
                owner=data.get('owner'),
                owner_type=data.get('owner_type', 'User'),
                description=data.get('description'),
                url_path=data.get('url_path'),
                git_url=data.get('git_url'),
                ssh_url=data.get('ssh_url'),
                visibility=data.get('visibility', 'public'),
                branch=data.get('branch', 'main'),
                owner_discord_id=owner_id
            )
            return self.json_response({'uuid': project_uuid}, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_attached_projects(self, request):
        owner_id = request.query.get('owner_discord_id')
        if not owner_id:
            return self.json_response({'error': 'Missing owner_discord_id parameter'}, status=400)
        projects = await get_all_attached_projects(owner_id)
        return self.json_response(projects or [])

    async def handle_delete_github_project(self, request):
        uuid = request.match_info['uuid']
        await remove_github_project(uuid)
        return self.json_response({'status': 'deleted'})

    # ------------------------------
    # MAINTENANCE
    # ------------------------------
    async def handle_get_logs(self, request):
        service = request.match_info["service"]

        cog = self.bot.get_cog("MaintenanceCog")
        if not cog:
            return web.json_response(
                {"error": "Maintenance module unavailable"},
                status=503
            )

        if service == "arvo-team":
            cmd = "pm2 logs arvo.team --lines 50 --time --raw"
        # elif service == "nginx":
        #     cmd = "tail -n 50 -F /var/log/nginx/error.log"
        # elif service == "nydus-ui":
        #     cmd = "pm2 logs nydus-ui --lines 50 --time --raw"
        # elif service == "nydus":
        #     cmd = "journalctl -u nydus -n 50 -f -o short-iso"
        else:
            return web.json_response(
                {"error": "Unknown service"},
                status=400
            )

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

        await response.prepare(request)

        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            while not process.stdout.at_eof():
                line = await process.stdout.readline()
                if not line:
                    await asyncio.sleep(0.05)
                    continue

                payload = line.decode(errors="ignore").replace("\r", "").rstrip("\n")
                data = f"data: {payload}\n\n"
                await response.write(data.encode())
                await response.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

        return response

    async def handle_restart_service(self, request):
        service = request.match_info['service']
        cog = self.bot.get_cog('MaintenanceCog')
        if not cog:
            return self.json_response({'error': 'Maintenance module unavailable'}, status=503)
        response = web.StreamResponse(status=200, headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        })
        await response.prepare(request)
        async for update in cog.run_maintenance_stream(service):
            await response.write(f"data: {json.dumps(update)}\n\n".encode('utf-8'))
            if update.get('done'):
                break
        return response

    # ------------------------------
    # WEBHOOKS
    # ------------------------------
    async def handle_webhook(self, request):
        """
        GitHub push webhook → rebuild the matching live deployment.

        Verifies the HMAC signature, answers ping events, ignores non-push events and
        pushes to a non-tracked branch, then resolves the deployment by the webhook
        project's subdomain and queues a rebuild (no PAT needed — rebuild reuses the
        persisted git remote).
        """
        uuid = request.match_info['uuid']
        project = await get_webhook_project_by_uuid(uuid)
        if not project:
            return self.json_response({'error': 'Project not found'}, status=404)

        # 1) Verify GitHub HMAC signature over the raw body.
        signature = request.headers.get('X-Hub-Signature-256')
        body = await request.read()
        secret = project.get('webhook_secret')
        if not secret:
            return self.json_response({'error': 'Secret config error'}, status=500)
        expected = "sha256=" + hmac.new(
            secret.encode(), msg=body, digestmod=hashlib.sha256
        ).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            return self.json_response({'error': 'Invalid signature'}, status=401)

        # 2) Event routing.
        event = request.headers.get('X-GitHub-Event', '')
        if event == 'ping':
            return self.json_response({'status': 'pong'})
        if event != 'push':
            return self.json_response(
                {'status': 'ignored', 'reason': f"event '{event}' not handled"}
            )

        # 3) Branch match: only rebuild when the pushed branch is the tracked one.
        try:
            payload = json.loads(body.decode('utf-8', errors='replace') or '{}')
        except (ValueError, TypeError):
            payload = {}
        ref = payload.get('ref', '') or ''
        pushed_branch = ref.split('refs/heads/', 1)[1] if ref.startswith('refs/heads/') else ref
        tracked_branch = project.get('branch') or 'main'
        if pushed_branch and pushed_branch != tracked_branch:
            return self.json_response({
                'status': 'ignored',
                'reason': f"push to '{pushed_branch}' != tracked branch '{tracked_branch}'",
            })

        # 4) Resolve the live deployment and queue a rebuild. Prefer the fqdn (covers
        #    custom domains, whose subdomain is NULL); fall back to subdomain for legacy rows.
        deployer = self.bot.get_cog('DeploymentCog')
        if not deployer:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)

        target_fqdn = project.get('fqdn')
        subdomain = project.get('subdomain')
        deployment = None
        if target_fqdn:
            deployment = await get_live_deployment_by_fqdn(target_fqdn)
        if not deployment and subdomain:
            deployment = await get_live_deployment_by_subdomain(subdomain)
        if not deployment:
            return self.json_response(
                {'error': f"No live deployment for '{target_fqdn or subdomain}' to rebuild"},
                status=404,
            )

        run_id = deployer.queue_rebuild(deployment['deployment_uuid'], 'webhook')
        await self._log_to_discord(
            'Webhook rebuild queued',
            f"Push to `{pushed_branch or tracked_branch}` → rebuilding "
            f"`{target_fqdn or subdomain}` (run `{run_id}`).",
        )
        return self.json_response({'status': 'queued', 'run_id': run_id}, status=202)

    # ------------------------------
    # CLOUDFLARE
    # ------------------------------
    async def handle_get_dns_records(self, request):
        cf_cog = self.bot.get_cog('CloudflareCog')
        if not cf_cog:
            return self.json_response({'error': 'Cloudflare module unavailable'}, status=503)
        type_filter = request.query.get('type')
        name_filter = request.query.get('name')
        try:
            page = int(request.query.get('page', 1))
        except ValueError:
            page = 1
        data, error = await cf_cog.list_dns_records(type=type_filter, name=name_filter, page=page)
        if error:
            return self.json_response({'error': error}, status=400)
        return self.json_response(data)

    async def handle_create_dns_record(self, request):
        cf_cog = self.bot.get_cog('CloudflareCog')
        if not cf_cog:
            return self.json_response({'error': 'Cloudflare module unavailable'}, status=503)
        try:
            data = await request.json()
            record_type = data.get('type', 'A')
            name = data.get('name')
            content = data.get('content')
            ttl = int(data.get('ttl', 1))
            proxied = data.get('proxied', True)
            comment = data.get('comment', '')
            if not name or not content:
                return self.json_response({'error': 'Name and Content are required'}, status=400)
            result, error = await cf_cog.create_dns_record(type=record_type, name=name, content=content, ttl=ttl, proxied=proxied, comment=comment)
            if error:
                return self.json_response({'error': error}, status=400)
            return self.json_response(result, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_update_dns_record(self, request):
        cf_cog = self.bot.get_cog('CloudflareCog')
        if not cf_cog:
            return self.json_response({'error': 'Cloudflare module unavailable'}, status=503)
        record_id = request.match_info['record_id']
        try:
            data = await request.json()
            record_type = data.get('type', 'A')
            name = data.get('name')
            content = data.get('content')
            ttl = int(data.get('ttl', 1))
            proxied = data.get('proxied', True)
            comment = data.get('comment', '')
            result, error = await cf_cog.update_dns_record(record_id=record_id, type=record_type, name=name, content=content, ttl=ttl, proxied=proxied, comment=comment)
            if error:
                return self.json_response({'error': error}, status=400)
            return self.json_response(result)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_delete_dns_record(self, request):
        cf_cog = self.bot.get_cog('CloudflareCog')
        if not cf_cog:
            return self.json_response({'error': 'Cloudflare module unavailable'}, status=503)
        record_id = request.match_info['record_id']
        success, error = await cf_cog.delete_dns_record(record_id)
        if error:
            return self.json_response({'error': error}, status=400)
        return self.json_response({'status': 'deleted'})

    async def handle_get_analytics(self, request):
        try:
            cf_cog = self.bot.get_cog('CloudflareCog')
            if not cf_cog:
                return self.json_response({'error': 'Cloudflare module unavailable'}, status=503)
            days = int(request.query.get('days', 7))
            stats, error = await cf_cog.get_visitor_stats(days=days)
            if error:
                return self.json_response({'error': error}, status=400)
            return self.json_response(stats)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_dynamic_analytics(self, request):
        try:
            days_raw = request.query.get('days', 7)
            cf_cog = self.bot.get_cog('CloudflareCog')
            if not cf_cog:
                return self.json_response({'error': 'Cloudflare module unavailable'}, status=503)
            try:
                days = int(days_raw)
            except ValueError:
                return self.json_response({'error': 'Invalid days parameter'}, status=400)
            stats, error = await cf_cog.get_dynamic_analytics(days=days)
            if error:
                return self.json_response({'error': error}, status=400)
            return self.json_response(stats)
        except Exception as e:
            traceback.print_exc()
            return self.json_response({'error': str(e)}, status=500)

    # ------------------------------
    # MESSENGER API
    # ------------------------------
    async def handle_messenger_verification(self, request):
        hub_mode = request.query.get('hub.mode')
        hub_challenge = request.query.get('hub.challenge')
        hub_verify_token = request.query.get('hub.verify_token')

        verify_token = os.getenv('META_APP_MESSENGER_VERIFY_TOKEN')

        if not verify_token:
            return self.json_response({'error': 'Verify token not configured'}, status=500)

        if hub_mode == 'subscribe' and hub_verify_token == verify_token:
            return web.Response(
                status=200,
                text=hub_challenge,
                headers={'Content-Type': 'text/plain'}
            )
        else:
            return web.Response(status=403, text='Verification failed')

    async def handle_messenger_webhook(self, request):
        try:
            data = await request.json()
            self.logger.info(f"Received Messenger webhook data: {data}")

            if 'entry' in data and data['entry']:
                for entry in data['entry']:
                    if 'messaging' in entry:
                        for messaging_event in entry['messaging']:
                            if 'message' in messaging_event and messaging_event['message'].get('text'):
                                message_text = messaging_event['message']['text']
                                sender_id = messaging_event['sender']['id']
                                await self.echo_to_discord(f"Message from Facebook user {sender_id}: {message_text}")

            return self.json_response({'status': 'success'})
        except Exception as e:
            self.logger.error(f"Error handling Messenger webhook: {str(e)}")
            return self.json_response({'error': str(e)}, status=500)

    async def echo_to_discord(self, message):
        channel_id = int(os.getenv('MESSENGER_ECHO_CHANNEL_ID', 981071936157286421))
        channel = self.bot.get_channel(channel_id)
        if channel:
            await channel.send(message)
        else:
            self.logger.error(f"Could not find channel with ID {channel_id}")

    # ------------------------------
    # DATABASE MANAGEMENT
    # ------------------------------
    async def handle_get_databases(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            include_deleted = request.query.get('include_deleted', 'false').lower() == 'true'
            databases = await db_cog.fetch_all_databases(include_deleted=include_deleted)
            return self.json_response(databases or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_database(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            uuid = request.match_info['uuid']
            database = await db_cog.fetch_database(database_uuid=uuid)
            if not database:
                return self.json_response({'error': 'Database not found'}, status=404)
            return self.json_response(database)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_create_database(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            data = await request.json()
            database_type = data.get('database_type')
            database_name = data.get('database_name')
            allowed_hosts = data.get('allowed_hosts', 'localhost')
            created_by = data.get('created_by')
            if not all([database_type, database_name, created_by]):
                return self.json_response({'error': 'Missing required fields: database_type, database_name, created_by'}, status=400)
            success, result = await db_cog.create_actual_database(database_type, database_name, allowed_hosts, created_by)
            if not success:
                return self.json_response({'error': result}, status=500)
            return self.json_response({'database_uuid': result}, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_delete_database(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            uuid = request.match_info['uuid']
            data = await request.json()
            database_name = data.get('database_name')
            database_type = data.get('database_type')
            deleted_by = data.get('deleted_by')
            if not all([database_name, database_type, deleted_by]):
                return self.json_response({'error': 'Missing required fields: database_name, database_type, deleted_by'}, status=400)
            success, error = await db_cog.drop_actual_database(database_type, database_name, uuid, deleted_by)
            if not success:
                return self.json_response({'error': error}, status=500)
            return self.json_response({'status': 'deleted'})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_create_database_user(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            data = await request.json()
            database_type = data.get('database_type')
            username = data.get('username')
            password = data.get('password')
            created_by = data.get('created_by')
            # Optional: 'localhost' | '%' | '*' | CSV of hosts. Defaults to remote ('%').
            allowed_hosts = data.get('allowed_hosts', '%')
            if not all([database_type, username, password, created_by]):
                return self.json_response({'error': 'Missing required fields: database_type, username, password, created_by'}, status=400)
            success, result = await db_cog.create_actual_user(
                database_type, username, password, created_by, allowed_hosts=allowed_hosts
            )
            if not success:
                return self.json_response({'error': result}, status=500)
            return self.json_response({'user_uuid': result}, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_delete_database_user(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            user_uuid = request.match_info['user_uuid']
            data = await request.json()
            database_type = data.get('database_type')
            username = data.get('username')
            deleted_by = data.get('deleted_by')
            if not all([database_type, username, deleted_by]):
                return self.json_response({'error': 'Missing required fields: database_type, username, deleted_by'}, status=400)
            success, error = await db_cog.drop_actual_user(database_type, username, user_uuid, deleted_by)
            if not success:
                return self.json_response({'error': error}, status=500)
            return self.json_response({'status': 'deleted'})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_grant_privileges(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            database_uuid = request.match_info['uuid']
            data = await request.json()
            database_type = data.get('database_type')
            database_name = data.get('database_name')
            user_uuid = data.get('user_uuid')
            username = data.get('username')
            privileges = data.get('privileges')
            granted_by = data.get('granted_by')
            if not all([database_type, database_name, user_uuid, username, privileges, granted_by]):
                return self.json_response({'error': 'Missing required fields: database_type, database_name, user_uuid, username, privileges, granted_by'}, status=400)
            success, error = await db_cog.grant_actual_privileges(database_type, database_name, database_uuid, username, user_uuid, privileges, granted_by)
            if not success:
                return self.json_response({'error': error}, status=500)
            return self.json_response({'status': 'granted'})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_revoke_privileges(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            database_uuid = request.match_info['uuid']
            user_uuid = request.match_info['user_uuid']
            data = await request.json()
            database_type = data.get('database_type')
            database_name = data.get('database_name')
            username = data.get('username')
            revoked_by = data.get('revoked_by')
            if not all([database_type, database_name, username, revoked_by]):
                return self.json_response({'error': 'Missing required fields: database_type, database_name, username, revoked_by'}, status=400)
            success, error = await db_cog.revoke_actual_privileges(database_type, database_name, database_uuid, username, user_uuid, revoked_by)
            if not success:
                return self.json_response({'error': error}, status=500)
            return self.json_response({'status': 'revoked'})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_perform_backup(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            database_uuid = request.match_info['uuid']
            data = await request.json()
            database_type = data.get('database_type')
            database_name = data.get('database_name')
            if not all([database_type, database_name]):
                return self.json_response({'error': 'Missing required fields: database_type, database_name'}, status=400)
            success, result = await db_cog.perform_backup(database_uuid, database_type, database_name)
            if not success:
                return self.json_response({'error': result}, status=500)
            return self.json_response({'backup_uuid': result}, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_restore_backup(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            database_uuid = request.match_info['uuid']
            data = await request.json()
            database_type = data.get('database_type')
            database_name = data.get('database_name')
            backup_file_path = data.get('backup_file_path')
            if not all([database_type, database_name, backup_file_path]):
                return self.json_response({'error': 'Missing required fields: database_type, database_name, backup_file_path'}, status=400)
            success, error = await db_cog.restore_backup(database_type, database_name, backup_file_path)
            if not success:
                return self.json_response({'error': error}, status=500)
            return self.json_response({'status': 'restored'})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)


    async def handle_get_database_users(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            include_deleted = request.query.get('include_deleted', 'false').lower() == 'true'
            users = await db_cog.fetch_all_database_users(include_deleted=include_deleted)
            return self.json_response(users or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_database_privileges(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            uuid = request.match_info['uuid']
            privileges = await db_cog.fetch_privileges_for_database(uuid)
            return self.json_response(privileges or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_all_privileges(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            privileges = await db_cog.fetch_all_privileges()
            return self.json_response(privileges or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_user_credentials(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            user_uuid = request.match_info['user_uuid']
            credentials = await db_cog.get_user_credentials(user_uuid)
            if not credentials:
                return self.json_response({'error': 'User not found or decryption failed'}, status=404)
            return self.json_response(credentials)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_pma_token(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            data = await request.json()
            user_uuid = data.get('user_uuid')
            if not user_uuid:
                return self.json_response({'error': 'Missing user_uuid'}, status=400)
            credentials = await db_cog.get_user_credentials(user_uuid)
            if not credentials:
                return self.json_response({'error': 'User not found or decryption failed'}, status=404)
            token = secrets.token_hex(32)
            credentials_path = f'/tmp/nydus_pma_{token}.json'
            with open(credentials_path, 'w') as f:
                json.dump(credentials, f)
            os.chmod(credentials_path, 0o644)

            # The handoff file holds plaintext DB credentials in a world-readable /tmp.
            # The phpMyAdmin redirect consumes it immediately; expire it shortly after so
            # the exposure window is seconds, not until reboot. (Perms stay 0o644 because
            # the consuming pma process may run as a different user.)
            async def _expire_pma_creds(path: str):
                await asyncio.sleep(30)
                try:
                    os.remove(path)
                except OSError:
                    pass
            asyncio.create_task(_expire_pma_creds(credentials_path))

            return self.json_response({'token': token})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_db_quickgen(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)

        try:
            data = await request.json()
            database_type = data.get('database_type')
            created_by = data.get('created_by')

            # Validate required fields
            if not database_type or not created_by:
                return self.json_response({'error': 'Missing database_type or created_by'}, status=400)

            success, error, result = await db_cog.quickgen_provision(
                database_type=database_type,
                created_by=created_by
            )

            if success:
                return self.json_response(result)   # ← returns the credentials dict
            else:
                return self.json_response({'error': error}, status=500)

        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_database_backups(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            database_uuid = request.match_info['uuid']
            backups = await db_cog.fetch_backups_for_database(database_uuid)
            return self.json_response(backups or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_download_backup(self, request):
        db_cog = self.bot.get_cog('DatabaseCog')
        if not db_cog:
            return self.json_response({'error': 'Database module unavailable'}, status=503)
        try:
            backup_uuid = request.match_info['backup_uuid']
            backup = await db_cog.fetch_backup(backup_uuid)
            if not backup:
                return self.json_response({'error': 'Backup not found'}, status=404)
            file_path = backup.get('file_path', '')
            if not os.path.exists(file_path):
                return self.json_response({'error': 'Backup file not found on disk'}, status=404)
            filename = backup.get('file_name', os.path.basename(file_path))
            content_type = 'application/gzip' if filename.endswith('.gz') else 'application/octet-stream'
            file_size = os.path.getsize(file_path)
            response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"',
                    'Content-Type': content_type,
                    'Content-Length': str(file_size),
                    'Access-Control-Allow-Origin': '*',
                }
            )
            await response.prepare(request)
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    await response.write(chunk)
            return response
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_all_backups(self, request):
        try:
            limit = int(request.query.get('limit', 50))
            backups = await get_all_recent_backups(limit)
            return self.json_response(backups or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_get_all_schedules(self, request):
        try:
            schedules = await get_all_schedules()
            return self.json_response(schedules or [])
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_toggle_schedule(self, request):
        schedule_cog = self.bot.get_cog('DatabaseScheduleCog')
        if not schedule_cog:
            return self.json_response({'error': 'Schedule module unavailable'}, status=503)
        try:
            schedule_uuid = request.match_info['schedule_uuid']
            s = await get_schedule_by_uuid(schedule_uuid)
            if not s:
                return self.json_response({'error': 'Schedule not found'}, status=404)
            new_state = 0 if s['enabled'] else 1
            await set_schedule_enabled(schedule_uuid, new_state)
            if new_state == 1 and not s['next_run_at']:
                await set_schedule_next_run(schedule_uuid, datetime.utcnow() + timedelta(seconds=s['interval_seconds']))
            await create_schedule_log(
                schedule_uuid=schedule_uuid,
                database_uuid=s['database_uuid'],
                event_type='manual_toggle',
                message=f"Schedule {'enabled' if new_state else 'disabled'} via API"
            )
            return self.json_response({'enabled': bool(new_state)})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_force_run_schedule(self, request):
        schedule_cog = self.bot.get_cog('DatabaseScheduleCog')
        if not schedule_cog:
            return self.json_response({'error': 'Schedule module unavailable'}, status=503)
        try:
            schedule_uuid = request.match_info['schedule_uuid']
            s = await get_schedule_by_uuid(schedule_uuid)
            if not s:
                return self.json_response({'error': 'Schedule not found'}, status=404)
            if s['task_type'] == 'db_validity_check':
                asyncio.create_task(schedule_cog._guarded_validity_check(s))
            elif s['task_type'] == 'db_backup':
                asyncio.create_task(schedule_cog._guarded_backup(s))
            else:
                return self.json_response({'error': f"Unknown task type: {s['task_type']}"}, status=400)
            return self.json_response({'status': 'queued'})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_list_deployments(self, request):
        """GET /api/deployments"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        from database.db import get_all_deployments
        deployments = await get_all_deployments()
        return self.json_response(deployments)

    async def handle_get_deployment(self, request):
        """GET /api/deployments/{deployment_uuid}"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment_uuid = request.match_info['deployment_uuid']
        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        return self.json_response(deployment)

    # ------------------------------
    # CONTROL PLANE
    # ------------------------------
    def _domain(self):
        return os.getenv('DEPLOY_DOMAIN', 'arvo.team')

    async def _stream_shell(self, request, cmd):
        """SSE stream of a shell command's output (raw 'data: <line>' frames)."""
        response = web.StreamResponse(status=200, headers={
            'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
            'Connection': 'keep-alive', 'Access-Control-Allow-Origin': '*',
        })
        await response.prepare(request)
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            while not process.stdout.at_eof():
                line = await process.stdout.readline()
                if not line:
                    await asyncio.sleep(0.05)
                    continue
                payload = line.decode(errors='ignore').replace('\r', '').rstrip('\n')
                await response.write(f"data: {payload}\n\n".encode())
                await response.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return response

    async def _stream_static_text(self, request, text):
        """SSE stream of static text (for stored build logs), ending with [done]."""
        response = web.StreamResponse(status=200, headers={
            'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
            'Connection': 'keep-alive', 'Access-Control-Allow-Origin': '*',
        })
        await response.prepare(request)
        try:
            for line in (text or '').splitlines():
                await response.write(f"data: {line}\n\n".encode())
            await response.write(b"data: [done]\n\n")
            await response.drain()
        except ConnectionResetError:
            pass
        return response

    async def handle_server_overview(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployments = await get_active_deployments()
        services = await get_all_managed_services()
        dep_status = await dep.build_overview(deployments)
        stats = await get_recent_system_resources_with_averages()
        return self.json_response({
            'system': stats,
            'deployments': dep_status,
            'managed_services': services,
        })

    async def handle_server_discover(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        return self.json_response(await dep.discover_server_state())

    async def handle_server_recover(self, request):
        """POST /api/server/recover — bring every active node deployment + enabled pm2 managed
        service back online, recreating any process pm2 lost (on its stored port/path)."""
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        report = await dep.recover_all()
        recovered = sum(1 for r in report if r['ok'] and r['detail'] != 'already online')
        failed = [r for r in report if not r['ok']]
        return self.json_response({
            'status': 'ok' if not failed else 'partial',
            'recovered': recovered, 'failed': len(failed), 'report': report,
        })

    async def handle_watchdog_status(self, request):
        """GET /api/watchdog — watchdog alerting state (off by default; toggle after a reboot settles)."""
        mon = self.bot.get_cog('MonitoringCog')
        if not mon:
            return self.json_response({'error': 'Monitoring module unavailable'}, status=503)
        return self.json_response(mon.watchdog_status())

    async def handle_watchdog_set(self, request):
        """POST /api/watchdog  body {alerts_enabled?: bool, self_heal_enabled?: bool}."""
        mon = self.bot.get_cog('MonitoringCog')
        if not mon:
            return self.json_response({'error': 'Monitoring module unavailable'}, status=503)
        try:
            data = await request.json()
        except Exception:
            data = {}
        return self.json_response(mon.set_watchdog(
            alerts_enabled=data.get('alerts_enabled'),
            self_heal_enabled=data.get('self_heal_enabled'),
        ))

    # ------------------------------
    # ALERTS / NOTIFICATIONS
    # ------------------------------
    async def handle_list_alerts(self, request):
        try:
            limit = int(request.query.get('limit', 50))
        except ValueError:
            limit = 50
        unack = request.query.get('unacknowledged', 'false').lower() == 'true'
        level = request.query.get('level') or None
        alerts = await get_alerts(limit=limit, unacknowledged_only=unack, level=level)
        return self.json_response(alerts)

    async def handle_alert_count(self, request):
        return self.json_response({'unacknowledged': await get_unacknowledged_alert_count()})

    async def handle_ack_alert(self, request):
        ok = await acknowledge_alert(request.match_info['alert_uuid'])
        if not ok:
            return self.json_response({'error': 'Alert not found or already acknowledged'}, status=404)
        return self.json_response({'status': 'acknowledged'})

    async def handle_ack_all_alerts(self, request):
        await acknowledge_all_alerts()
        return self.json_response({'status': 'acknowledged'})

    async def handle_list_services(self, request):
        return self.json_response(await get_all_managed_services())

    async def handle_create_service(self, request):
        try:
            data = await request.json()
            name = data.get('name')
            service_type = data.get('service_type')
            if not name or service_type not in ('pm2', 'systemd', 'nginx', 'static'):
                return self.json_response({'error': 'name and a valid service_type are required'}, status=400)
            record = await create_managed_service(
                name=name, service_type=service_type,
                pm2_name=data.get('pm2_name'), systemd_unit=data.get('systemd_unit'),
                fqdn=data.get('fqdn'), health_url=data.get('health_url'),
                deploy_path=data.get('deploy_path'), port=data.get('port'),
                git_url=data.get('git_url'), branch=data.get('branch'),
            )
            if not record:
                return self.json_response({'error': 'Failed to create service (duplicate name?)'}, status=500)
            return self.json_response(record, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    # Columns a client may patch on a managed service. Whitelisted because
    # update_managed_service interpolates column names into the SQL.
    _MANAGED_SERVICE_FIELDS = (
        'name', 'service_type', 'pm2_name', 'systemd_unit', 'fqdn', 'health_url',
        'deploy_path', 'port', 'git_url', 'branch', 'enabled',
    )

    async def handle_update_service(self, request):
        """PUT /api/services/{service_uuid} — patch fields (e.g. set deploy_path + port so a
        pm2 service like arvo.team can be recovered)."""
        service_uuid = request.match_info['service_uuid']
        if not await get_managed_service(service_uuid=service_uuid):
            return self.json_response({'error': 'Service not found'}, status=404)
        try:
            data = await request.json()
        except Exception:
            data = {}
        updates = {k: data[k] for k in self._MANAGED_SERVICE_FIELDS if k in data}
        if 'service_type' in updates and updates['service_type'] not in ('pm2', 'systemd', 'nginx', 'static'):
            return self.json_response({'error': 'invalid service_type'}, status=400)
        if not updates:
            return self.json_response({'error': 'No updatable fields provided'}, status=400)
        ok = await update_managed_service(service_uuid, **updates)
        if not ok:
            return self.json_response({'error': 'Update failed'}, status=500)
        return self.json_response(await get_managed_service(service_uuid=service_uuid))

    async def handle_delete_service(self, request):
        ok = await delete_managed_service(request.match_info['service_uuid'])
        if not ok:
            return self.json_response({'error': 'Delete failed'}, status=500)
        return self.json_response({'status': 'deleted'})

    async def handle_service_process(self, request):
        maint = self.bot.get_cog('MaintenanceCog')
        if not maint:
            return self.json_response({'error': 'Maintenance module unavailable'}, status=503)
        service = await get_managed_service(service_uuid=request.match_info['service_uuid'])
        if not service:
            return self.json_response({'error': 'Service not found'}, status=404)
        try:
            data = await request.json()
        except Exception:
            data = {}
        ok, msg = await maint.control_managed_service(service, data.get('action', 'restart'))
        if not ok:
            return self.json_response({'error': msg}, status=400)
        return self.json_response({'status': data.get('action', 'restart'), 'detail': msg})

    async def handle_service_logs(self, request):
        maint = self.bot.get_cog('MaintenanceCog')
        if not maint:
            return self.json_response({'error': 'Maintenance module unavailable'}, status=503)
        service = await get_managed_service(service_uuid=request.match_info['service_uuid'])
        if not service:
            return self.json_response({'error': 'Service not found'}, status=404)
        try:
            lines = int(request.query.get('lines', 100))
        except ValueError:
            lines = 100
        cmd = maint.managed_service_log_command(service, lines=lines)
        if not cmd:
            return self.json_response({'error': 'No logs for this service type'}, status=400)
        return await self._stream_shell(request, cmd)

    async def handle_service_diagnostics(self, request):
        """GET /api/services/{service_uuid}/diagnostics — why it's unhealthy, even when down."""
        maint = self.bot.get_cog('MaintenanceCog')
        if not maint:
            return self.json_response({'error': 'Maintenance module unavailable'}, status=503)
        service = await get_managed_service(service_uuid=request.match_info['service_uuid'])
        if not service:
            return self.json_response({'error': 'Service not found'}, status=404)
        return self.json_response(await maint.service_diagnostics(service))

    async def handle_deployment_status(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        return self.json_response(await dep.get_deployment_status(deployment))

    async def handle_deployment_logs(self, request):
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        kind = request.match_info['kind']
        fqdn = fqdn_of(deployment)
        pm2_name = deployment.get('pm2_name') or deployment['deployment_uuid'][:12]
        if kind == 'app':
            if deployment.get('tech_stack') != 'node':
                return self.json_response({'error': 'No app process for this stack'}, status=400)
            dep_cog = self.bot.get_cog('DeploymentCog')
            proc = await dep_cog._pm2_status(pm2_name) if dep_cog else None
            online = bool(proc) and (proc.get('pm2_env', {}) or {}).get('status') == 'online'
            if online:
                # Live tail of stdout/stderr (includes the recent buffer).
                return await self._stream_shell(request, f"pm2 logs {pm2_name} --lines 200 --time --raw")
            # Down/lost: a live tail would just hang empty — serve the persisted crash output
            # (recent stderr) plus the exit/restart state so you can see HOW it crashed.
            diag = await dep_cog.get_process_diagnostics(pm2_name, lines=200) if dep_cog else {}
            header = (
                f"[diagnostics] process is not online (pm2 status: {diag.get('status') or 'not found'}; "
                f"restarts: {diag.get('restarts')}; exit_code: {diag.get('exit_code')}).\n"
                f"[diagnostics] most recent stderr from {diag.get('error_log_path')}:\n"
                + ("-" * 60) + "\n"
            )
            body = diag.get('error_log') or "(no error log found — the process may never have started)"
            return await self._stream_static_text(request, header + body)
        if kind == 'nginx-access':
            return await self._stream_shell(request, f"tail -n 100 -F /var/log/nginx/{fqdn}.access.log")
        if kind == 'nginx-error':
            return await self._stream_shell(request, "tail -n 100 -F /var/log/nginx/error.log")
        if kind == 'build':
            row = await execute_query(
                "SELECT output_log FROM deployment_logs WHERE deployment_uuid=%s "
                "ORDER BY started_at DESC LIMIT 1",
                (request.match_info['deployment_uuid'],), fetch_one=True)
            return await self._stream_static_text(request, (row or {}).get('output_log') or 'No build logs.')
        return self.json_response({'error': 'Unknown log kind'}, status=400)

    async def handle_deployment_diagnostics(self, request):
        """GET /api/deployments/{deployment_uuid}/diagnostics — why it's unhealthy, even when
        the process is down (pm2 exit/restart state + recent stderr + nginx errors)."""
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        return self.json_response(await dep.get_deployment_diagnostics(deployment))

    async def handle_deployment_config(self, request):
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        fqdn = fqdn_of(deployment)
        nginx_path = f"/etc/nginx/sites-available/{fqdn}"
        pkg_path = os.path.join(deployment.get('deploy_path', '') or '', 'package.json')

        def _read():
            nginx_cfg, scripts = None, None
            try:
                with open(nginx_path, 'r', errors='replace') as f:
                    nginx_cfg = f.read()
            except OSError:
                pass
            try:
                with open(pkg_path, 'r') as f:
                    scripts = json.load(f).get('scripts', {})
            except Exception:
                pass
            return nginx_cfg, scripts
        loop = asyncio.get_running_loop()
        nginx_cfg, scripts = await loop.run_in_executor(None, _read)
        return self.json_response({
            'nginx_config': nginx_cfg,
            'package_scripts': scripts,
            'env_file_name': deployment.get('env_file_name'),
        })

    async def handle_deployment_process(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        if deployment.get('tech_stack') != 'node':
            return self.json_response({'error': 'No process for this stack'}, status=400)
        try:
            data = await request.json()
        except Exception:
            data = {}
        pm2_name = deployment.get('pm2_name') or deployment['deployment_uuid'][:12]
        # Pass deploy_path + assigned_port so a process pm2 lost is recreated on its
        # correct port, not just poked.
        ok, msg = await dep.control_process(
            pm2_name, data.get('action', 'restart'),
            deploy_path=deployment.get('deploy_path'),
            port=deployment.get('assigned_port'),
        )
        if not ok:
            return self.json_response({'error': msg}, status=400)
        return self.json_response({'status': data.get('action', 'restart'), 'detail': msg})

    async def handle_deployment_nginx(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        try:
            data = await request.json()
        except Exception:
            data = {}
        fqdn = fqdn_of(deployment)
        ok, msg = await dep.control_nginx(data.get('action', 'reload'), fqdn)
        if not ok:
            return self.json_response({'error': msg}, status=400)
        return self.json_response({'status': data.get('action', 'reload'), 'detail': msg})

    async def handle_deployment_ssl_renew(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        ok, msg = await dep.renew_ssl(fqdn_of(deployment))
        if not ok:
            return self.json_response({'error': msg}, status=400)
        return self.json_response({'status': 'renewed', 'detail': msg})

    async def handle_deployment_dns_reconcile(self, request):
        dep = self.bot.get_cog('DeploymentCog')
        if not dep:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        ok, msg = await dep.reconcile_dns(deployment)
        if not ok:
            return self.json_response({'error': msg}, status=400)
        return self.json_response({'status': 'reconciled', 'detail': msg})

    # ------------------------------
    # WEBHOOK MANAGEMENT
    # ------------------------------
    def _webhook_url(self, webhook_uuid):
        # GitHub must reach /webhook/{uuid} publicly. WEBHOOK_PUBLIC_BASE is where this API
        # is exposed (e.g. https://nydus.arvo.team). If unset, return the path only.
        base = os.getenv('WEBHOOK_PUBLIC_BASE', '').rstrip('/')
        path = f"/webhook/{webhook_uuid}"
        return base + path if base else path

    def _webhook_payload(self, wh):
        return {
            'webhook_uuid': wh['webhook_uuid'],
            'url': self._webhook_url(wh['webhook_uuid']),
            'secret': wh['webhook_secret'],
            'branch': wh.get('branch'),
            'content_type': 'application/json',
            'events': ['push'],
        }

    async def _resolve_webhook(self, deployment):
        """Find a deployment's webhook project by fqdn (covers custom domains, whose
        subdomain is NULL), falling back to subdomain for legacy rows."""
        wh = await get_webhook_project_by_fqdn(fqdn_of(deployment))
        if not wh and deployment.get('subdomain'):
            wh = await get_webhook_project_by_subdomain(deployment['subdomain'])
        return wh

    async def handle_get_webhook(self, request):
        """GET /api/deployments/{deployment_uuid}/webhook — the webhook for this deployment."""
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        wh = await self._resolve_webhook(deployment)
        if not wh:
            return self.json_response({'error': 'No webhook configured'}, status=404)
        return self.json_response(self._webhook_payload(wh))

    async def handle_create_webhook(self, request):
        """
        POST /api/deployments/{deployment_uuid}/webhook — register GitHub auto-deploy.
        Idempotent: returns the existing webhook if one is already configured for the subdomain.
        """
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)

        existing = await self._resolve_webhook(deployment)
        if existing:
            return self.json_response(self._webhook_payload(existing))

        from database.db import get_github_project_by_uuid
        project = await get_github_project_by_uuid(deployment.get('project_uuid')) or {}
        result = await create_new_webhook_project(
            name=project.get('name') or fqdn_of(deployment),
            repo_url=project.get('git_url'),
            branch=deployment.get('branch') or 'main',
            tech_stack=deployment.get('tech_stack'),
            subdomain=deployment.get('subdomain'),
            cloudflare_id=deployment.get('cf_record_id'),
            nginx_port=deployment.get('assigned_port'),
            fqdn=fqdn_of(deployment),
        )
        if not result.get('success'):
            return self.json_response(
                {'error': result.get('error', 'Failed to create webhook')}, status=500
            )
        return self.json_response(self._webhook_payload({
            'webhook_uuid': result['webhook_uuid'],
            'webhook_secret': result['webhook_secret'],
            'branch': deployment.get('branch') or 'main',
        }), status=201)

    async def handle_delete_webhook(self, request):
        """DELETE /api/deployments/{deployment_uuid}/webhook"""
        deployment = await get_deployment_by_uuid(request.match_info['deployment_uuid'])
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        wh = await self._resolve_webhook(deployment)
        if not wh:
            return self.json_response({'error': 'No webhook configured'}, status=404)
        await delete_webhook_project(wh['webhook_uuid'])
        return self.json_response({'status': 'deleted'})

    async def handle_deploy(self, request):
        """POST /api/deploy
        subdomain mode: {project_uuid, subdomain, github_pat, triggered_by}
        custom domain:  {project_uuid, domain, dns_mode: 'cloudflare'|'external', github_pat, triggered_by}
        """
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        try:
            data = await request.json()
            project_uuid = data.get('project_uuid')
            subdomain = data.get('subdomain')
            github_pat = data.get('github_pat')
            triggered_by = data.get('triggered_by')
            dns_mode = (data.get('dns_mode') or 'subdomain').lower()
            domain = data.get('domain')

            if dns_mode not in ('subdomain', 'cloudflare', 'external'):
                return self.json_response(
                    {'error': "dns_mode must be 'subdomain', 'cloudflare', or 'external'"}, status=400
                )
            if not all([project_uuid, github_pat, triggered_by]):
                return self.json_response(
                    {'error': 'Missing project_uuid, github_pat, or triggered_by'}, status=400
                )
            # subdomain XOR domain, by mode.
            if dns_mode == 'subdomain':
                if not subdomain:
                    return self.json_response(
                        {'error': 'subdomain is required for dns_mode=subdomain'}, status=400
                    )
            else:
                if not domain:
                    return self.json_response(
                        {'error': f'domain is required for dns_mode={dns_mode}'}, status=400
                    )
                from utils.validators import validate_domain
                ok, reason = validate_domain(domain)
                if not ok:
                    return self.json_response({'error': reason}, status=400)

            from database.db import get_github_project_by_uuid
            project = await get_github_project_by_uuid(project_uuid)
            if not project:
                return self.json_response({'error': 'Project not found'}, status=404)

            project_data = {
                'project_uuid': project['project_uuid'],
                'name': project['name'],
                'git_url': project['git_url'],
                'default_branch': project.get('branch', 'main')
            }

            run_id = dep_cog.queue_deploy(
                project_data, subdomain, github_pat, triggered_by,
                domain=domain, dns_mode=dns_mode,
            )
            return self.json_response({'run_id': run_id}, status=202)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_stream_logs(self, request):
        """GET /api/deploy/logs/{run_uuid}  - Server-Sent Events stream"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        run_uuid = request.match_info['run_uuid']
        queue = dep_cog.get_stream(run_uuid)
        if not queue:
            return self.json_response({'error': 'No active log stream for that run ID'}, status=404)
        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*'
            }
        )
        await response.prepare(request)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # Quiet stretch (e.g. npm install with no output). Send a keepalive
                    # instead of erroring the stream — the client treats [keepalive] as a no-op.
                    await response.write(b"data: [keepalive]\n\n")
                    await response.drain()
                    continue
                if line is None:
                    # End-of-run sentinel: signal a clean completion so the client closes
                    # without rendering a spurious "connection lost" error.
                    await response.write(b"data: [done]\n\n")
                    await response.drain()
                    break
                await response.write(f"data: {json.dumps({'line': line})}\n\n".encode())
                await response.drain()
        except ConnectionResetError:
            pass
        return response

    async def handle_rebuild(self, request):
        """POST /api/deploy/rebuild/{deployment_uuid}"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment_uuid = request.match_info['deployment_uuid']
        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return self.json_response({'error': 'Deployment not found'}, status=404)
        triggered_by = request.get('auth_key_data', {}).get('owner_discord_id', 'api')
        run_id = dep_cog.queue_rebuild(deployment_uuid, triggered_by)
        return self.json_response({'run_id': run_id}, status=202)

    async def handle_selftest(self, request):
        """POST /api/selftest

        Body (optional): {"variants": "all" | "static,node,rebuild,webhook,rollback",
                          "cert_staging": true}
        Returns 202 {run_id}; stream progress via GET /api/deploy/logs/{run_id}.
        """
        st_cog = self.bot.get_cog('SelfTestCog')
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not st_cog or not dep_cog:
            return self.json_response({'error': 'Self-test module unavailable'}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        variants = st_cog.parse_variants(body.get('variants', 'all'))
        cert_staging = bool(body.get('cert_staging', True))
        triggered_by = request.get('auth_key_data', {}).get('owner_discord_id', 'api')
        result = st_cog.queue_selftest(triggered_by, variants, cert_staging=cert_staging)
        if not result.get('ok'):
            # 409 when another admin's run holds the lock (with who/when so the UI can point
            # there); 503 if the deploy module is down.
            status = 409 if result.get('reason') == 'busy' else 503
            return self.json_response(
                {'error': result.get('message', 'Could not start self-test.'),
                 'active': result.get('active')},
                status=status,
            )
        run_id = result['run_id']
        return self.json_response(
            {'status': 'started', 'run_id': run_id, 'variants': variants,
             'cert_staging': cert_staging, 'log_stream': f"/api/deploy/logs/{run_id}"},
            status=202,
        )

    async def handle_selftest_status(self, request):
        """GET /api/selftest — is a self-test running, and which one (for single-flight UI)."""
        st_cog = self.bot.get_cog('SelfTestCog')
        active = st_cog._active_busy() if st_cog else None
        return self.json_response({'running': bool(active), 'active': active})

    async def handle_delete_deployment(self, request):
        """DELETE /api/deployments/{deployment_uuid}"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment_uuid = request.match_info['deployment_uuid']
        # Capture the row before deletion so we can clean up its webhook afterwards.
        deployment = await get_deployment_by_uuid(deployment_uuid)
        success, msg = await dep_cog.delete_deployment(deployment_uuid)
        if not success:
            return self.json_response({'error': msg}, status=400)
        if deployment:
            wh = await self._resolve_webhook(deployment)
            if wh:
                await delete_webhook_project(wh['webhook_uuid'])
        return self.json_response({'status': 'deleted', 'message': msg})

    async def handle_get_env(self, request):
        """GET /api/deployments/{deployment_uuid}/env
        Returns list of {key, value} for the deployment's env file.
        """
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment_uuid = request.match_info['deployment_uuid']
        env_vars, error = await dep_cog.get_env_lines(deployment_uuid)
        if error:
            return self.json_response({'error': error}, status=404)
        return self.json_response({'env': env_vars})

    async def handle_update_env(self, request):
        """PUT /api/deployments/{deployment_uuid}/env
        Body: {"key": "VAR_NAME", "value": "new_value"}
        Updates an existing environment variable.
        """
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        try:
            data = await request.json()
            key = data.get('key')
            value = data.get('value')
            if not key or value is None:
                return self.json_response({'error': 'Missing key or value'}, status=400)
            deployment_uuid = request.match_info['deployment_uuid']
            success, error = await dep_cog.update_env_line(deployment_uuid, key, value)
            if not success:
                return self.json_response({'error': error}, status=400)
            return self.json_response({'status': 'updated', 'key': key})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_add_env(self, request):
        """POST /api/deployments/{deployment_uuid}/env
        Body: {"key": "NEW_VAR", "value": "some_value"}
        Adds a new environment variable (fails if key already exists).
        """
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        try:
            data = await request.json()
            key = data.get('key')
            value = data.get('value')
            if not key or value is None:
                return self.json_response({'error': 'Missing key or value'}, status=400)
            deployment_uuid = request.match_info['deployment_uuid']
            success, error = await dep_cog.add_env_line(deployment_uuid, key, value)
            if not success:
                return self.json_response({'error': error}, status=400)
            return self.json_response({'status': 'added', 'key': key})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_delete_env(self, request):
        """DELETE /api/deployments/{deployment_uuid}/env?key=VAR_NAME
        Deletes an environment variable by key.
        """
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        try:
            key = request.query.get('key')
            if not key:
                return self.json_response({'error': 'Missing key query parameter'}, status=400)
            deployment_uuid = request.match_info['deployment_uuid']
            success, error = await dep_cog.delete_env_line(deployment_uuid, key)
            if not success:
                return self.json_response({'error': error}, status=400)
            return self.json_response({'status': 'deleted', 'key': key})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_attendance_login(self, request):
        try:
            data = await request.json()
            email = data.get('email')
            password = data.get('password')
            if not email or not password:
                return self.json_response({'error': 'Missing email or password'}, status=400)
    
            attendance_cog = self.bot.get_cog('SchoolAttendanceCog')
            if not attendance_cog:
                return self.json_response({'error': 'Attendance module unavailable'}, status=503)
    
            school_pool = await attendance_cog._get_school_pool()
            async with school_pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute('SELECT * FROM users WHERE email = %s', (email,))
                    user = await cur.fetchone()
    
            if not user:
                return self.json_response({'error': 'Invalid credentials'}, status=401)
    
            stored_hash = user.get('password') or user.get('password_hash', '')
            if not stored_hash:
                return self.json_response({'error': 'Invalid credentials'}, status=401)
    
            if not bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                return self.json_response({'error': 'Invalid credentials'}, status=401)
    
            payload = {
                'sub': user['custom_id'],
                'email': user['email'],
                'iat': datetime.utcnow(),
                'exp': datetime.utcnow() + timedelta(hours=8),
            }
            token = jwt.encode(payload, os.environ['ATTENDANCE_JWT_SECRET'], algorithm='HS256')
            user_safe = {k: v for k, v in user.items() if k not in ('password', 'password_hash')}
            return self.json_response({'token': token, 'user': user_safe})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)
    
    
    async def handle_attendance_qr_login(self, request):
        try:
            data = await request.json()
            qr_data = data.get('qr_data', '')
    
            match = re.match(r'id:(.+)\|token:(.+)', qr_data)
            if not match:
                return self.json_response({'error': 'Invalid QR format'}, status=400)
    
            qr_custom_id = match.group(1)
            secure_token = match.group(2)
    
            attendance_cog = self.bot.get_cog('SchoolAttendanceCog')
            if not attendance_cog:
                return self.json_response({'error': 'Attendance module unavailable'}, status=503)
    
            pool = await attendance_cog._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        'SELECT id FROM demo_school_attendance_tokens WHERE school_custom_id = %s AND secure_token = %s',
                        (qr_custom_id, secure_token),
                    )
                    token_row = await cur.fetchone()
    
            if not token_row:
                return self.json_response({'error': 'Invalid QR code'}, status=401)
    
            school_pool = await attendance_cog._get_school_pool()
            async with school_pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute('SELECT * FROM users WHERE custom_id = %s', (qr_custom_id,))
                    user = await cur.fetchone()
    
            if not user:
                return self.json_response({'error': 'User not found'}, status=404)
    
            payload = {
                'sub': qr_custom_id,
                'email': user.get('email', ''),
                'iat': datetime.utcnow(),
                'exp': datetime.utcnow() + timedelta(hours=8),
            }
            token = jwt.encode(payload, os.environ['ATTENDANCE_JWT_SECRET'], algorithm='HS256')
            user_safe = {k: v for k, v in user.items() if k not in ('password', 'password_hash')}
            return self.json_response({'token': token, 'user': user_safe})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)
    
    
    async def handle_attendance_qr_scan(self, request):
        try:
            decoded = _decode_attendance_jwt(self, request)
            if not decoded:
                return self.json_response({'error': 'Unauthorized'}, status=401)
    
            data = await request.json()
            qr_data = data.get('qr_data', '')
            from_ip = data.get('from_ip') or request.headers.get('X-Forwarded-For', request.remote)
            from_mac = data.get('from_mac')
            from_url = data.get('from_url')
            user_agent = data.get('user_agent') or request.headers.get('User-Agent')
            qr_scan_image = data.get('qr_scan_image')
            attendance_type = data.get('attendance_type', 'time_in')
    
            match = re.match(r'id:(.+)\|token:(.+)', qr_data)
            if not match:
                return self.json_response({'error': 'Invalid QR format'}, status=400)
    
            qr_custom_id = match.group(1)
            secure_token = match.group(2)
    
            attendance_cog = self.bot.get_cog('SchoolAttendanceCog')
            if not attendance_cog:
                return self.json_response({'error': 'Attendance module unavailable'}, status=503)
    
            pool = await attendance_cog._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        'SELECT id FROM demo_school_attendance_tokens WHERE school_custom_id = %s AND secure_token = %s',
                        (qr_custom_id, secure_token),
                    )
                    token_row = await cur.fetchone()
    
            if not token_row:
                return self.json_response({'error': 'Invalid QR code'}, status=401)
    
            record = await attendance_cog.create_attendance(
                school_custom_id=qr_custom_id,
                action_type='qr_scan',
                attendance_timestamp=int(datetime.utcnow().timestamp()),
                attendance_type=attendance_type,
                from_url=from_url,
                from_ip=from_ip,
                from_mac=from_mac,
                user_agent=user_agent,
                qr_scan_image=qr_scan_image,
            )
            return self.json_response({'success': True, 'record': record}, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)
    
    
    async def handle_attendance_clock(self, request):
        try:
            decoded = _decode_attendance_jwt(self, request)
            if not decoded:
                return self.json_response({'error': 'Unauthorized'}, status=401)
    
            data = await request.json()
            attendance_type = data.get('attendance_type', 'time_in')
            from_ip = data.get('from_ip') or request.headers.get('X-Forwarded-For', request.remote)
            from_mac = data.get('from_mac')
            from_url = data.get('from_url')
            user_agent = data.get('user_agent') or request.headers.get('User-Agent')
    
            attendance_cog = self.bot.get_cog('SchoolAttendanceCog')
            if not attendance_cog:
                return self.json_response({'error': 'Attendance module unavailable'}, status=503)
    
            record = await attendance_cog.create_attendance(
                school_custom_id=decoded['sub'],
                action_type='manual',
                attendance_timestamp=int(datetime.utcnow().timestamp()),
                attendance_type=attendance_type,
                from_url=from_url,
                from_ip=from_ip,
                from_mac=from_mac,
                user_agent=user_agent,
            )
            return self.json_response({'success': True, 'record': record}, status=201)
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)
    
    
    async def handle_attendance_history(self, request):
        try:
            decoded = _decode_attendance_jwt(self, request)
            if not decoded:
                return self.json_response({'error': 'Unauthorized'}, status=401)
    
            try:
                limit = int(request.query.get('limit', 100))
                offset = int(request.query.get('offset', 0))
            except ValueError:
                return self.json_response({'error': 'Invalid limit or offset'}, status=400)
    
            attendance_cog = self.bot.get_cog('SchoolAttendanceCog')
            if not attendance_cog:
                return self.json_response({'error': 'Attendance module unavailable'}, status=503)
    
            records = await attendance_cog.get_attendances_by_custom_id(
                school_custom_id=decoded['sub'],
                limit=limit,
                offset=offset,
            )
            return self.json_response({'records': records})
        except Exception as e:
            return self.json_response({'error': str(e)}, status=500)

    async def handle_tusd_upload_complete(self, request):
        try:
            body = await request.json()
        except Exception:
            return self.json_response({"error": "Invalid JSON body"}, status=400)

        asyncio.ensure_future(self._process_tusd_upload(body))
        return self.json_response({"received": True})

    async def _process_tusd_upload(self, body):
        try:
            event    = body.get("Event", {})
            upload   = event.get("Upload", {})
            http_req = event.get("HTTPRequest", {})

            upload_id = upload.get("ID")
            if not upload_id or not is_valid_uuid(upload_id):
                self.logger.error(f"Invalid upload ID: {upload_id}")
                return

            raw_metadata = upload.get("MetaData") or {}
            metadata = dict(raw_metadata)

            filename    = metadata.pop("filename", "unknown")
            filetype    = metadata.pop("filetype", None)
            upload_type = metadata.pop("upload_type", None)

            if not upload_type or upload_type not in UPLOAD_DESTINATIONS:
                self.logger.error(f"Invalid upload_type: {upload_type}")
                return

            try:
                safe_filename = secure_filename(filename)
            except ValueError as e:
                self.logger.error(f"Filename sanitization failed: {e}")
                return

            if not safe_filename:
                self.logger.error("Filename empty after sanitization")
                return

            if len(safe_filename) > MAX_FILENAME_LENGTH:
                self.logger.error("Filename too long")
                return

            if filetype and len(filetype) > MAX_FILETYPE_LENGTH:
                self.logger.error("Filetype too long")
                return

            if len(metadata) > MAX_METADATA_PAIRS:
                self.logger.error("Too many metadata fields")
                return

            for k, v in metadata.items():
                if len(str(k)) > MAX_META_KEY_LENGTH or len(str(v)) > MAX_META_VALUE_LENGTH:
                    self.logger.error(f"Metadata field '{k}' exceeds length limit")
                    return

            file_size = upload.get("Size", 0)
            if not isinstance(file_size, int) or file_size <= 0:
                self.logger.error(f"Invalid file size: {file_size}")
                return

            staging_path = f"/var/data/uploads/{upload_id}"
            if not os.path.isfile(staging_path):
                self.logger.error(f"Staging file not found: {staging_path}")
                return

            dest_dir = UPLOAD_DESTINATIONS[upload_type]
            os.makedirs(dest_dir, exist_ok=True)
            final_path = _unique_path(dest_dir, safe_filename)

            ip_address = _extract_ip(http_req.get("RemoteAddr", ""))
            user_agent = ((http_req.get("Header") or {}).get("User-Agent") or [None])[0]

            inserted = await create_tusd_upload(
                upload_id=upload_id,
                filename=safe_filename,
                filetype=filetype,
                file_path=staging_path,
                file_size=file_size,
                ip_address=ip_address,
                user_agent=user_agent,
                status="pending",
            )
            if not inserted:
                self.logger.error(f"Failed to create upload record for {upload_id}")
                return

            try:
                shutil.move(staging_path, final_path)
            except OSError as e:
                self.logger.error(f"File move failed {staging_path} -> {final_path}: {e}")
                await update_tusd_upload(upload_id, status="failed")
                return

            await update_tusd_upload(upload_id, file_path=final_path, status="complete")

            if metadata:
                await create_tusd_upload_meta(upload_id, metadata)

            self.logger.info(f"Upload complete: {upload_id} -> {final_path}")

        except Exception as e:
            self.logger.exception(f"tusd processing error: {e}")


def setup(bot):
    bot.add_cog(ApiCog(bot))