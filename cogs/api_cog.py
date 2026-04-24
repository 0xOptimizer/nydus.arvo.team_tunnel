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
    delete_webhook_project, add_github_project, get_all_github_projects, get_all_attached_projects,
    remove_github_project, get_user, get_auth_key, validate_auth_key, execute_query,
    get_all_recent_backups,
    get_all_schedules, get_schedule_by_uuid, set_schedule_enabled, set_schedule_next_run, create_schedule_log,
    get_all_deployments, get_deployment_by_uuid, update_deployment,
    create_tusd_upload, create_tusd_upload_meta,
)
import jwt
import bcrypt
import re
import aiomysql
import uuid as uuid_lib
import shutil
import ipaddress
from typing import Optional

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

def is_valid_uuid(uuid_str: str) -> bool:
    """Validate UUID v4 format."""
    return True
    # try:
    #     uuid_obj = uuid_lib.UUID(uuid_str)
    #     return uuid_obj.version == 4
    # except ValueError:
    #     return False

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

        # Deployments
        self._add_route('GET', '/api/deployments', self.handle_list_deployments)
        self._add_route('GET', '/api/deployments/{deployment_uuid}', self.handle_get_deployment)
        self._add_route('POST', '/api/deploy', self.handle_deploy)
        self._add_route('GET', '/api/deploy/logs/{run_uuid}', self.handle_stream_logs)
        self._add_route('POST', '/api/deploy/rebuild/{deployment_uuid}', self.handle_rebuild)
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
        uuid = request.match_info['uuid']
        project = await get_webhook_project_by_uuid(uuid)
        if not project:
            return self.json_response({'error': 'Project not found'}, status=404)
        signature = request.headers.get('X-Hub-Signature-256')
        body = await request.read()
        secret = project['webhook_secret']
        if not secret:
            return self.json_response({'error': 'Secret config error'}, status=500)
        hash_obj = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + hash_obj.hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            return self.json_response({'error': 'Invalid signature'}, status=401)
        deployer = self.bot.get_cog('DeploymentCog')
        if deployer:
            asyncio.create_task(deployer.deploy_project(project))
            return self.json_response({'status': 'queued'})
        return self.json_response({'error': 'Deployment module unavailable'}, status=500)

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
            if not all([database_type, username, password, created_by]):
                return self.json_response({'error': 'Missing required fields: database_type, username, password, created_by'}, status=400)
            success, result = await db_cog.create_actual_user(database_type, username, password, created_by)
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

    async def handle_deploy(self, request):
        """POST /api/deploy  body: {project_uuid, subdomain, github_pat, triggered_by}"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        try:
            data = await request.json()
            project_uuid = data.get('project_uuid')
            subdomain = data.get('subdomain')
            github_pat = data.get('github_pat')
            triggered_by = data.get('triggered_by')
            
            if not all([project_uuid, subdomain, github_pat, triggered_by]):
                return self.json_response(
                    {'error': 'Missing project_uuid, subdomain, github_pat, or triggered_by'}, 
                    status=400
                )
            
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
            
            run_id = dep_cog.queue_deploy(project_data, subdomain, github_pat, triggered_by)
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
                line = await asyncio.wait_for(queue.get(), timeout=30.0)
                if line is None:
                    break
                await response.write(f"data: {json.dumps({'line': line})}\n\n".encode())
                await response.drain()
        except asyncio.TimeoutError:
            await response.write(f"data: {json.dumps({'error': 'Stream timeout'})}\n\n".encode())
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

    async def handle_delete_deployment(self, request):
        """DELETE /api/deployments/{deployment_uuid}"""
        dep_cog = self.bot.get_cog('DeploymentCog')
        if not dep_cog:
            return self.json_response({'error': 'Deployment module unavailable'}, status=503)
        deployment_uuid = request.match_info['deployment_uuid']
        success, msg = await dep_cog.delete_deployment(deployment_uuid)
        if not success:
            return self.json_response({'error': msg}, status=400)
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