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

    # ------------------------------
    # Registry-driven control (managed_services)
    # ------------------------------
    async def control_managed_service(self, service: dict, action: str):
        """Lifecycle control for a managed_services row, dispatched by service_type."""
        stype = service.get('service_type')
        dep = self.bot.get_cog('DeploymentCog')

        if stype == 'pm2':
            if not dep:
                return False, "DeploymentCog unavailable"
            name = service.get('pm2_name') or service.get('name')
            return await dep.control_process(name, action)

        if stype == 'systemd':
            unit = service.get('systemd_unit')
            if not unit:
                return False, "no systemd_unit configured"
            if action not in ('restart', 'stop', 'start', 'reload'):
                return False, f"Invalid systemd action '{action}'"
            return await self._run_command(f"sudo systemctl {action} {unit}")

        if stype == 'nginx':
            if not dep:
                return False, "DeploymentCog unavailable"
            nginx_action = 'reload' if action in ('restart', 'reload') else action
            return await dep.control_nginx(nginx_action, service.get('fqdn'))

        if stype == 'static':
            return False, "static services have no process to control"
        return False, f"Unknown service_type '{stype}'"

    def managed_service_log_command(self, service: dict, lines: int = 100):
        """The shell command to tail a managed service's logs, by service_type."""
        stype = service.get('service_type')
        if stype == 'pm2':
            name = service.get('pm2_name') or service.get('name')
            return f"pm2 logs {name} --lines {lines} --time --raw"
        if stype == 'systemd':
            unit = service.get('systemd_unit')
            return f"journalctl -u {unit} -n {lines} -f -o short-iso" if unit else None
        if stype == 'nginx':
            return f"tail -n {lines} -F /var/log/nginx/error.log"
        return None


def setup(bot):
    bot.add_cog(MaintenanceCog(bot))