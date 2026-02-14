from discord.ext import commands, tasks
from aiohttp import web
import os
import hmac
import hashlib
import json
import logging
import traceback
import asyncio
from database.db import (
    get_recent_usage, 
    get_project_by_uuid, 
    get_all_projects, 
    create_new_project, 
    delete_project,
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
        self.app.router.add_get('/api/projects', self.handle_get_projects)
        self.app.router.add_post('/api/projects', self.handle_create_project)
        self.app.router.add_delete('/api/projects/{uuid}', self.handle_delete_project)
        self.app.router.add_post('/webhook/{uuid}', self.handle_webhook)

    @tasks.loop(count=1)
    async def start_server(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()

    @start_server.before_loop
    async def before_start_server(self):
        await self.bot.wait_until_ready()

    async def handle_options(self, request):
        return web.Response(status=200, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        })

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
            
        except json.JSONDecodeError:
             return web.json_response({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            self.logger.error(f"Auth Check Error: {e}")
            return web.json_response({'error': 'Internal Server Error'}, status=500)

    async def handle_get_stats(self, request):
        try:
            stats = await get_recent_usage(limit=1)
            return web.json_response(stats, headers={'Access-Control-Allow-Origin': '*'})
        except Exception as e:
            self.logger.error(f"Stats API Error: {e}")
            return web.json_response({'error': str(e)}, status=500)

    async def handle_get_projects(self, request):
        projects = await get_all_projects()
        return web.json_response(projects, headers={'Access-Control-Allow-Origin': '*'})

    async def handle_create_project(self, request):
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

            result = await create_new_project(
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
            traceback.print_exc()
            return web.json_response({'error': str(e)}, status=500)

    async def handle_delete_project(self, request):
        uuid = request.match_info['uuid']
        
        project = await get_project_by_uuid(uuid)
        if project and project['cloudflare_record_id']:
            cf_cog = self.bot.get_cog('CloudflareCog')
            if cf_cog:
                await cf_cog.delete_dns_record(project['cloudflare_record_id'])

        await delete_project(uuid)
        return web.json_response({'status': 'deleted'}, headers={'Access-Control-Allow-Origin': '*'})

    async def handle_webhook(self, request):
        uuid = request.match_info['uuid']
        project = await get_project_by_uuid(uuid)

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