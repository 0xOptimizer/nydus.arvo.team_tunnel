from discord.ext import commands
import asyncio
import os
from database.db import log_deployment

class DeploymentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def run_command(self, cmd, cwd=None):
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(), stderr.decode()

    async def deploy_project(self, project_data):
        output_cog = self.bot.get_cog('OutputCog')
        path = project_data['deploy_path']
        name = project_data['project_name']

        if output_cog:
            await output_cog.queue_message(f"Starting deployment for {name}...")

        commands_list = ["git pull origin main"]
        
        if os.path.exists(os.path.join(path, 'package.json')):
            commands_list.append("npm install")
            commands_list.append("npm run build")
            commands_list.append(f"pm2 reload {name}")
        
        full_log = ""
        success = True

        for cmd in commands_list:
            code, out, err = await self.run_command(cmd, cwd=path)
            full_log += f"$ {cmd}\n{out}\n{err}\n"
            
            if code != 0:
                success = False
                if output_cog:
                    await output_cog.queue_message(f"Deployment failed at step: {cmd}", "ERROR")
                break

        status = "SUCCESS" if success else "FAILED"
        await log_deployment(name, status, "WEBHOOK", full_log)

        if success and output_cog:
            await output_cog.queue_message(f"Deployment successful for {name}.", "SUCCESS")
        
        return success, full_log

def setup(bot):
    bot.add_cog(DeploymentCog(bot))