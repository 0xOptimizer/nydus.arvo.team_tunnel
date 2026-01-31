from discord.ext import commands, tasks
from aiohttp import web
import os
import hmac
import hashlib
import json
from database.db import get_recent_usage, get_deployments, get_project_by_uuid

class ApiCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.app = web.Application()
        self.setup_routes()
        self.runner = None
        self.site = None
        self.port = int(os.getenv('PORT', 4000))
        self.start_server.start()

    def cog_unload(self):
        self.start_server.cancel()

    def setup_routes(self):
        self.app.router.add_post('/webhook/{uuid}', self.handle_webhook)
        self.app.router.add_get('/api/stats', self.handle_stats)
        self.app.router.add_get('/api/deployments', self.handle_deployments)
        self.app.router.add_get('/api/nginx/status', self.handle_nginx_status)
        self.app.router.add_post('/api/nginx/reload', self.handle_nginx_reload)

    @tasks.loop(count=1)
    async def start_server(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()
        
        output = self.bot.get_cog('OutputCog')
        if output:
            await output.queue_message(f"API Server listening on port {self.port}")

    @start_server.before_loop
    async def before_start_server(self):
        await self.bot.wait_until_ready()

    async def handle_stats(self, request):
        data = await get_recent_usage()
        return web.json_response(data)

    async def handle_deployments(self, request):
        data = await get_deployments()
        return web.json_response(data)

    async def handle_nginx_status(self, request):
        nginx = self.bot.get_cog('NginxCog')
        if not nginx:
            return web.json_response({'error': 'Nginx module not loaded'}, status=500)
        
        status = await nginx.get_status()
        return web.json_response(status)

    async def handle_nginx_reload(self, request):
        nginx = self.bot.get_cog('NginxCog')
        if not nginx:
            return web.json_response({'error': 'Nginx module not loaded'}, status=500)

        success, msg = await nginx.reload_nginx()
        status_code = 200 if success else 500
        return web.json_response({'success': success, 'message': msg}, status=status_code)

    async def handle_webhook(self, request):
        uuid = request.match_info['uuid']
        project = await get_project_by_uuid(uuid)

        if not project:
            return web.json_response({'error': 'Project not found'}, status=404)

        signature = request.headers.get('X-Hub-Signature-256')
        body = await request.read()
        
        if not signature:
             return web.json_response({'error': 'Missing signature'}, status=401)

        secret = project['webhook_secret']
        hash_obj = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
        expected = "sha256=" + hash_obj.hexdigest()

        if not hmac.compare_digest(expected, signature):
            return web.json_response({'error': 'Invalid signature'}, status=401)

        deployer = self.bot.get_cog('DeploymentCog')
        if deployer:
            asyncio.create_task(deployer.deploy_project(project))
            return web.json_response({'status': 'Deployment queued'})
        
        return web.json_response({'error': 'Deployment module unavailable'}, status=500)

def setup(bot):
    bot.add_cog(ApiCog(bot))