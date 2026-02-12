import discord
from discord.ext import commands, tasks
import psutil
import logging
from database.db import log_usage, execute_query

class MonitoringCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.monitor_system.start()
        self.cleanup_old_logs.start()

    def cog_unload(self):
        self.monitor_system.cancel()
        self.cleanup_old_logs.cancel()

    @tasks.loop(seconds=10)
    async def monitor_system(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent
            connections = len(psutil.net_connections())

            await log_usage(cpu, ram, disk, connections)
        except Exception as e:
            logging.error(f"Monitoring error: {e}")

    @tasks.loop(hours=24)
    async def cleanup_old_logs(self):
        try:
            await execute_query(
                "DELETE FROM usage_logs WHERE timestamp < NOW() - INTERVAL 7 DAY"
            )
            logging.info("Cleaned up old usage logs.")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")

    @monitor_system.before_loop
    @cleanup_old_logs.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(MonitoringCog(bot))