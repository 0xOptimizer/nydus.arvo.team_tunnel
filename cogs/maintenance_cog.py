import discord
from discord.ext import commands
import asyncio
import os
import logging

class MaintenanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('nydus')
        # PATHS
        self.arvo_path = "/var/www/arvo.team"
        self.nydus_ui_path = "/var/www/nydus"
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
        elif service == 'arvo-team':
            cmd = "pm2 logs arvo.team --lines 100 --nostream"
        elif service == 'nydus-ui':
            cmd = "pm2 logs nydus-ui --lines 100 --nostream"
        elif service == 'nydus':
            cmd = "journalctl -u nydus -n 100 --no-pager"
        else:
            return False, "Unknown service."
        return await self._run_command(cmd)

    async def run_maintenance_stream(self, service):
        # ------------------------------
        # NGINX RESTART
        # ------------------------------
        if service == 'nginx':
            yield {"status": "progress", "message": "Restarting Nginx service..."}
            success, out = await self._run_command("sudo systemctl restart nginx")
            yield {"status": "success" if success else "error", "message": out, "done": True}

        # ------------------------------
        # ARVO.TEAM (NEXT.JS)
        # ------------------------------
        elif service == 'arvo-team':
            steps = [
                ("Pulling arvo.team code...", "git pull"),
                ("Installing dependencies...", "npm install"),
                ("Building arvo.team...", "npm run build"),
                ("Restarting PM2 (arvo.team)...", "pm2 restart arvo.team")
            ]
            for msg, cmd in steps:
                yield {"status": "progress", "message": msg}
                success, out = await self._run_command(cmd, cwd=self.arvo_path)
                if not success:
                    yield {"status": "error", "message": f"Failed at: {msg}\n{out}", "done": True}
                    return
            yield {"status": "success", "message": "arvo.team updated successfully.", "done": True}

        # ------------------------------
        # NYDUS-UI (NEXT.JS)
        # ------------------------------
        elif service == 'nydus-ui':
            steps = [
                ("Pulling nydus.arvo.team code...", "git pull"),
                ("Installing dependencies...", "npm install"),
                ("Building nydus-ui...", "npm run build"),
                ("Restarting PM2 (nydus-ui)...", "pm2 restart nydus-ui")
            ]
            for msg, cmd in steps:
                yield {"status": "progress", "message": msg}
                success, out = await self._run_command(cmd, cwd=self.nydus_ui_path)
                if not success:
                    yield {"status": "error", "message": f"Failed at: {msg}\n{out}", "done": True}
                    return
            yield {"status": "success", "message": "nydus-ui updated successfully.", "done": True}

        # ------------------------------
        # NYDUS BOT (PYTHON)
        # ------------------------------
        elif service == 'nydus':
            yield {"status": "progress", "message": "Initiating Bot Pull & Restart..."}
            yield {"status": "success", "message": "Restarting systemd service. Connection will drop.", "done": True}
            await asyncio.sleep(1)
            asyncio.create_task(self._run_command("git pull && sudo systemctl restart nydus", cwd=self.bot_path))

def setup(bot):
    bot.add_cog(MaintenanceCog(bot))