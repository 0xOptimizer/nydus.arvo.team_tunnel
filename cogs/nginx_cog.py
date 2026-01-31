from discord.ext import commands
import subprocess
import asyncio

class NginxCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def reload_nginx(self):
        process = await asyncio.create_subprocess_shell(
            "sudo systemctl reload nginx",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        output = self.bot.get_cog('OutputCog')
        if process.returncode == 0:
            if output:
                await output.queue_message("Nginx reloaded successfully.")
            return True, "Reloaded"
        else:
            error_msg = stderr.decode().strip()
            if output:
                await output.queue_message(f"Nginx reload failed: {error_msg}", "ERROR")
            return False, error_msg

    async def get_status(self):
        process = await asyncio.create_subprocess_shell(
            "sudo systemctl status nginx",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        status_text = stdout.decode()
        
        is_running = "Active: active (running)" in status_text
        return {
            "running": is_running,
            "details": status_text[:500]
        }

def setup(bot):
    bot.add_cog(NginxCog(bot))