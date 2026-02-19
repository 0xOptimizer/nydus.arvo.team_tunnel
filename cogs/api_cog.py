from discord.ext import commands, tasks
from aiohttp import web
import os
import hmac
import hashlib
import json
import logging
import asyncio
import discord
from datetime import datetime
from database.db import (
    get_recent_usage, get_webhook_project_by_uuid, get_all_webhook_projects, create_new_webhook_project,
    delete_webhook_project, add_github_project, get_all_github_projects, get_all_attached_projects,
    remove_github_project, get_user, get_auth_key, validate_auth_key, execute_query
)

def json_serial(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

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
    def setup_routes(self):
        # Internal server routes
        self.internal_app.router.add_options('/{tail:.*}', self.handle_options)
        self.internal_app.router.add_post('/api/auth/check-user', self.handle_check_user)
        self.internal_app.router.add_get('/api/stats', self.handle_get_stats)
        self.internal_app.router.add_get('/api/cloudflare/records', self.handle_get_dns_records)
        self.internal_app.router.add_post('/api/cloudflare/records', self.handle_create_dns_record)
        self.internal_app.router.add_put('/api/cloudflare/records/{record_id}', self.handle_update_dns_record)
        self.internal_app.router.add_delete('/api/cloudflare/records/{record_id}', self.handle_delete_dns_record)
        self.internal_app.router.add_get('/api/cloudflare/analytics', self.handle_get_analytics)
        self.internal_app.router.add_get('/api/cloudflare/dynamic-analytics', self.handle_get_dynamic_analytics)
        self.internal_app.router.add_get('/api/github-projects', self.handle_get_github_projects)
        self.internal_app.router.add_post('/api/github-projects', self.handle_create_github_project)
        self.internal_app.router.add_delete('/api/github-projects/{uuid}', self.handle_delete_github_project)
        self.internal_app.router.add_get('/api/attached-projects', self.handle_get_attached_projects)
        self.internal_app.router.add_post('/webhook/{uuid}', self.handle_webhook)
        self.internal_app.router.add_get('/api/maintenance/logs/{service}', self.handle_get_logs)
        self.internal_app.router.add_get('/api/maintenance/restart/{service}', self.handle_restart_service)
        self.internal_app.router.add_post('/api/toggle-public', self.handle_toggle_public)

        # Public server routes (same handlers, auth applied via middleware)
        self.public_app.router.add_options('/{tail:.*}', self.handle_options)
        self.public_app.router.add_post('/api/auth/check-user', self.handle_check_user)
        self.public_app.router.add_get('/api/stats', self.handle_get_stats)
        self.public_app.router.add_get('/api/cloudflare/records', self.handle_get_dns_records)
        self.public_app.router.add_post('/api/cloudflare/records', self.handle_create_dns_record)
        self.public_app.router.add_put('/api/cloudflare/records/{record_id}', self.handle_update_dns_record)
        self.public_app.router.add_delete('/api/cloudflare/records/{record_id}', self.handle_delete_dns_record)
        self.public_app.router.add_get('/api/cloudflare/analytics', self.handle_get_analytics)
        self.public_app.router.add_get('/api/cloudflare/dynamic-analytics', self.handle_get_dynamic_analytics)
        self.public_app.router.add_get('/api/github-projects', self.handle_get_github_projects)
        self.public_app.router.add_post('/api/github-projects', self.handle_create_github_project)
        self.public_app.router.add_delete('/api/github-projects/{uuid}', self.handle_delete_github_project)
        self.public_app.router.add_get('/api/attached-projects', self.handle_get_attached_projects)
        self.public_app.router.add_post('/webhook/{uuid}', self.handle_webhook)
        self.public_app.router.add_get('/api/maintenance/logs/{service}', self.handle_get_logs)
        self.public_app.router.add_get('/api/maintenance/restart/{service}', self.handle_restart_service)

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
        is_active = hasattr(self, 'public_server') and self.public_server is not None
        return self.json_response({'running': is_active})

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
    async def handle_get_stats(self, request):
        try:
            stats = await get_recent_usage(limit=1)
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
        service = request.match_info['service']
        cog = self.bot.get_cog('MaintenanceCog')
        if not cog:
            return self.json_response({'error': 'Maintenance module unavailable'}, status=503)
        success, output = await cog.get_service_logs(service)
        if not success:
            return self.json_response({'error': output}, status=400)
        return self.json_response({'logs': output})

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
            import traceback
            traceback.print_exc()
            return self.json_response({'error': str(e)}, status=500)

def setup(bot):
    bot.add_cog(ApiCog(bot))