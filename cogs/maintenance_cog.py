import discord
from discord.ext import commands
import asyncio
import os
import logging

class MaintenanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('nydus')
        self.nextjs_path = "/var/www/nydus.arvo.team"
        self.bot_path = "/opt/nydus"

    async def _run_command(self, cmd, cwd=None):
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode().strip()
            error = stderr.decode().strip()
            if process.returncode != 0:
                return False, f"Error: {error}\nOutput: {output}"
            return True, output if output else "Done."
        except Exception as e:
            return False, str(e)

    async def get_service_logs(self, service):
        cmd = ""
        if service == 'nginx':
            cmd = "tail -n 100 /var/log/nginx/error.log"
        elif service == 'nydus-ui':
            cmd = "pm2 logs nydus-ui --lines 100 --nostream"
        elif service == 'nydus':
            cmd = "journalctl -u nydus -n 100 --no-pager"
        else:
            return False, "Unknown service."
        return await self._run_command(cmd)

    async def run_maintenance_stream(self, service):
        if service == 'nginx':
            yield {"status": "progress", "message": "Restarting Nginx service..."}
            success, out = await self._run_command("sudo systemctl restart nginx")
            yield {"status": "success" if success else "error", "message": out, "done": True}

        elif service == 'nydus-ui':
            steps = [
                ("Pulling latest code...", "git pull"),
                ("Installing dependencies...", "npm install"),
                ("Building project...", "npm run build"),
                ("Restarting PM2 process...", "pm2 restart nydus-ui")
            ]
            for msg, cmd in steps:
                yield {"status": "progress", "message": msg}
                success, out = await self._run_command(cmd, cwd=self.nextjs_path)
                if not success:
                    yield {"status": "error", "message": f"Failed at: {msg}\n{out}", "done": True}
                    return
            yield {"status": "success", "message": "nydus-ui updated and rebuilt successfully.", "done": True}

        elif service == 'nydus':
            yield {"status": "progress", "message": "Initiating Bot Pull & Restart..."}
            yield {"status": "success", "message": "Restarting systemd service. Connection will drop.", "done": True}
            await asyncio.sleep(1)
            asyncio.create_task(self._run_command("git pull && sudo systemctl restart nydus", cwd=self.bot_path))

def setup(bot):
    bot.add_cog(MaintenanceCog(bot))