from discord.ext import commands, tasks
import psutil
from database.db import log_usage

class MonitoringCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.monitor_system.start()

    def cog_unload(self):
        self.monitor_system.cancel()

    @tasks.loop(minutes=5)
    async def monitor_system(self):
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        connections = len(psutil.net_connections())

        await log_usage(cpu, ram, disk, connections)
        
        if ram > 85:
            output = self.bot.get_cog('OutputCog')
            if output:
                await output.queue_message(f"High Memory Usage Alert: {ram}%", "WARNING")

    @monitor_system.before_loop
    async def before_monitor_system(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(MonitoringCog(bot))