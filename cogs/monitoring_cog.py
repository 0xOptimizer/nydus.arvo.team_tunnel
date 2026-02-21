import discord
from discord.ext import commands, tasks
import psutil
import os
import logging
from database.db import log_system_resources, execute_query

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
            
            mem = psutil.virtual_memory()
            ram_percent = mem.percent
            ram_remaining = mem.available
            ram_total = mem.total

            disk_info = psutil.disk_usage('/')
            disk_percent = disk_info.percent
            disk_remaining = disk_info.free
            disk_total = disk_info.total

            st = os.statvfs('/')
            inodes_total = st.f_files
            inodes_free = st.f_ffree
            inodes_used = inodes_total - inodes_free

            connections = len(psutil.net_connections())

            await log_system_resources(
                cpu, 
                ram_percent, 
                ram_remaining, 
                ram_total, 
                disk_percent, 
                disk_remaining, 
                disk_total,
                inodes_used, 
                inodes_total, 
                connections
            )
        except Exception as e:
            logging.error(f"Monitoring error: {e}")

    @tasks.loop(hours=24)
    async def cleanup_old_logs(self):
        try:
            await execute_query(
                "DELETE FROM system_stats WHERE timestamp < NOW() - INTERVAL 30 DAY"
            )
            logging.info("Cleaned up old system resources logs.")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")

    @monitor_system.before_loop
    @cleanup_old_logs.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(MonitoringCog(bot))