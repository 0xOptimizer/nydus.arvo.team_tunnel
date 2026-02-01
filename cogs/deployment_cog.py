from discord.ext import commands
import asyncio
import os
import discord
from database.db import log_deployment

class DeploymentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def run_command(self, cmd, cwd=None, env=None):
        environment = os.environ.copy()
        if env:
            environment.update(env)

        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=environment
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(), stderr.decode()

    async def deploy_project(self, project_data):
        output = self.bot.get_cog('OutputCog')
        
        name = project_data['project_name']
        uuid = project_data['webhook_uuid']
        repo_url = project_data['github_repository_url']
        branch = project_data['branch']
        path = f"/var/www/{uuid}" 
        stack = project_data.get('tech_stack', 'html').lower()
        
        if output:
            await output.send_embed(
                title=f"Deployment Started: {name}",
                description=f"Stack: {stack}\nBranch: {branch}",
                color=discord.Color.blue()
            )

        full_log = f"Deployment Started: {name}\nID: {uuid}\n"
        success = True

        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            full_log += f"Directory created: {path}\n"
            code, out, err = await self.run_command(f"git clone -b {branch} {repo_url} .", cwd=path)
            full_log += f"$ git clone\n{out}\n{err}\n"
            if code != 0:
                success = False
        else:
            cmd = f"git fetch origin {branch} && git reset --hard origin/{branch}"
            code, out, err = await self.run_command(cmd, cwd=path)
            full_log += f"$ {cmd}\n{out}\n{err}\n"
            if code != 0:
                success = False

        if success:
            commands_list = []
            
            if 'node' in stack or 'next' in stack:
                if os.path.exists(os.path.join(path, 'package.json')):
                    commands_list.append("npm install")
                    commands_list.append("npm run build")
                    commands_list.append(f"pm2 reload {name} || pm2 start npm --name \"{name}\" -- start")
            
            elif 'php' in stack or 'laravel' in stack:
                if os.path.exists(os.path.join(path, 'composer.json')):
                    commands_list.append("composer install --no-dev --optimize-autoloader")
                if os.path.exists(os.path.join(path, 'artisan')):
                    commands_list.append("php artisan migrate --force")
                    commands_list.append("php artisan config:cache")

            for cmd in commands_list:
                code, out, err = await self.run_command(cmd, cwd=path)
                full_log += f"$ {cmd}\n{out}\n{err}\n"
                if code != 0:
                    success = False
                    break

        status = "SUCCESS" if success else "FAILED"
        color = discord.Color.green() if success else discord.Color.red()
        
        await log_deployment(name, status, "WEBHOOK", full_log)

        if output:
            await output.send_embed(
                title=f"Deployment {status}: {name}",
                description="Check dashboard for full logs.",
                color=color,
                fields={"Commit": "Latest", "Status": status}
            )

        return success, full_log

def setup(bot):
    bot.add_cog(DeploymentCog(bot))