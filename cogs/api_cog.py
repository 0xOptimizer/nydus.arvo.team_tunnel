from discord.ext import commands, tasks
from aiohttp import web
import os
import hmac
import hashlib
import json
import logging
import asyncio
import discord
from database.db import (
    get_recent_usage, 
    get_webhook_project_by_uuid, 
    get_all_webhook_projects, 
    create_new_webhook_project, 
    delete_webhook_project,
    add_github_project,
    get_all_github_projects,
    remove_github_project,
    get_user
)

class ApiCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.setup_routes()
        self.runner = None
        self.site = None
        self.port = int(os.getenv('PORT', 4000))
        self.logger = logging.getLogger('nydus')
        self.start_server.start()
        self.server_ip = os.getenv('SERVER_IP', '127.0.0.1')

    def cog_unload(self):
        self.start_server.cancel()

    def setup_routes(self):
        self.app.router.add_options('/{tail:.*}', self.handle_options)
        self.app.router.add_post('/api/auth/check-user', self.handle_check_user)
        self.app.router.add_get('/api/stats', self.handle_get_stats)
        self.app.router.add_get('/api/deployments', self.handle_get_deployments)
        self.app.router.add_post('/api/deployments', self.handle_create_deployment)
        self.app.router.add_delete('/api/deployments/{uuid}', self.handle_delete_deployment)
        self.app.router.add_get('/api/github-projects', self.handle_get_github_projects)
        self.app.router.add_post('/api/github-projects', self.handle_create_github_project)
        self.app.router.add_delete('/api/github-projects/{uuid}', self.handle_delete_github_project)
        self.app.router.add_post('/webhook/{uuid}', self.handle_webhook)
        self.app.router.add_get('/api/maintenance/logs/{service}', self.handle_get_logs)
        self.app.router.add_get('/api/maintenance/restart/{service}', self.handle_restart_service)

    @tasks.loop(count=1)
    async def start_server(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()

    @start_server.before_loop
    async def before_start_server(self):
        await self.bot.wait_until_ready()

    async def _log_to_discord(self, title, message, color=discord.Color.blue()):
        output_cog = self.bot.get_cog("OutputCog")
        if output_cog:
            await output_cog.send_embed(
                title=title,
                description=message,
                color=color
            )

    async def handle_options(self, request):
        return web.Response(status=200, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        })

    # ------------------------------
    # AUTHENTICATION
    # ------------------------------

    async def handle_check_user(self, request):
        try:
            data = await request.json()
            discord_id = data.get('discord_id')
            if not discord_id:
                return web.json_response({'error': 'Missing discord_id'}, status=400)
            user = await get_user(discord_id)
            if user:
                return web.json_response({'exists': True}, headers={'Access-Control-Allow-Origin': '*'})
            return web.json_response({'error': 'User not found'}, status=401, headers={'Access-Control-Allow-Origin': '*'})
        except Exception:
            return web.json_response({'error': 'Internal Server Error'}, status=500)

    # ------------------------------
    # STATISTICS
    # ------------------------------

    async def handle_get_stats(self, request):
        try:
            stats = await get_recent_usage(limit=1)
            return web.json_response(stats, headers={'Access-Control-Allow-Origin': '*'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ------------------------------
    # DEPLOYMENTS
    # ------------------------------

    async def handle_get_deployments(self, request):
        projects = await get_all_webhook_projects()
        return web.json_response(projects, headers={'Access-Control-Allow-Origin': '*'})

    async def handle_create_deployment(self, request):
        try:
            data = await request.json()
            name = data.get('project_name')
            subdomain = data.get('subdomain')
            cf_cog = self.bot.get_cog('CloudflareCog')
            cf_record_id = None
            if cf_cog and subdomain:
                cf_record_id, error = await cf_cog.create_dns_record(subdomain, self.server_ip)
                if error:
                    return web.json_response({'error': f'DNS Error: {error}'}, status=400)
            result = await create_new_webhook_project(
                name=name,
                repo_url=data.get('github_repository_url'),
                branch=data.get('branch', 'main'),
                tech_stack=data.get('tech_stack', 'html'),
                subdomain=subdomain,
                cloudflare_id=cf_record_id,
                nginx_port=data.get('nginx_port', 0)
            )
            return web.json_response(result, status=201, headers={'Access-Control-Allow-Origin': '*'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_deployment(self, request):
        uuid = request.match_info['uuid']
        project = await get_webhook_project_by_uuid(uuid)
        if project and project['cloudflare_record_id']:
            cf_cog = self.bot.get_cog('CloudflareCog')
            if cf_cog:
                await cf_cog.delete_dns_record(project['cloudflare_record_id'])
        await delete_webhook_project(uuid)
        return web.json_response({'status': 'deleted'}, headers={'Access-Control-Allow-Origin': '*'})

    # ------------------------------
    # REPOSITORIES
    # ------------------------------

    async def handle_get_github_projects(self, request):
        projects = await get_all_github_projects()
        return web.json_response(projects, headers={'Access-Control-Allow-Origin': '*'})

    async def handle_create_github_project(self, request):
        try:
            data = await request.json()
            project_uuid = await add_github_project(
                name=data.get('name'),
                owner=data.get('owner'),
                owner_type=data.get('owner_type', 'User'),
                description=data.get('description'),
                url_path=data.get('url_path'),
                git_url=data.get('git_url'),
                ssh_url=data.get('ssh_url'),
                visibility=data.get('visibility', 'public'),
                branch=data.get('branch', 'main')
            )
            return web.json_response({'uuid': project_uuid}, status=201, headers={'Access-Control-Allow-Origin': '*'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_github_project(self, request):
        uuid = request.match_info['uuid']
        await remove_github_project(uuid)
        return web.json_response({'status': 'deleted'}, headers={'Access-Control-Allow-Origin': '*'})

    # ------------------------------
    # MAINTENANCE
    # ------------------------------

    async def handle_get_logs(self, request):
        service = request.match_info['service']
        cog = self.bot.get_cog('MaintenanceCog')
        if not cog:
            return web.json_response({'error': 'Maintenance module unavailable'}, status=503)
        success, output = await cog.get_service_logs(service)
        if not success:
            return web.json_response({'error': output}, status=400, headers={'Access-Control-Allow-Origin': '*'})
        return web.json_response({'logs': output}, headers={'Access-Control-Allow-Origin': '*'})

    async def handle_restart_service(self, request):
        service = request.match_info['service']
        cog = self.bot.get_cog('MaintenanceCog')
        if not cog:
            return web.json_response({'error': 'Maintenance module unavailable'}, status=503)
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
            return web.json_response({'error': 'Project not found'}, status=404)
        signature = request.headers.get('X-Hub-Signature-256')
        body = await request.read()
        secret = project['webhook_secret']
        if not secret:
            return web.json_response({'error': 'Secret config error'}, status=500)
        hash_obj = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + hash_obj.hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            return web.json_response({'error': 'Invalid signature'}, status=401)
        deployer = self.bot.get_cog('DeploymentCog')
        if deployer:
            asyncio.create_task(deployer.deploy_project(project))
            return web.json_response({'status': 'queued'})
        return web.json_response({'error': 'Deployment module unavailable'}, status=500)

def setup(bot):
    bot.add_cog(ApiCog(bot))