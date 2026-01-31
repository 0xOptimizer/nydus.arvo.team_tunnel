import discord
from discord.ext import commands, tasks
import asyncio
import logging
import json
import os

class OutputCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = asyncio.Queue()
        self.logger = logging.getLogger('nydus')
        self.channel_ids = json.loads(os.getenv('DEFAULT_OUTPUT_CHANNELS', '[]'))
        self.process_queue.start()

    def cog_unload(self):
        self.process_queue.cancel()

    async def queue_message(self, message, level='INFO'):
        log_msg = f"[{level}] {message}"
        self.logger.info(log_msg)
        await self.queue.put(f"**[{level}]** {message}")

    @tasks.loop(seconds=1.5)
    async def process_queue(self):
        if self.queue.empty():
            return

        message = await self.queue.get()
        
        for channel_id in self.channel_ids:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel:
                    await channel.send(message)
            except Exception as e:
                self.logger.error(f"Failed to send to Discord: {e}")
            
        self.queue.task_done()

    @process_queue.before_loop
    async def before_process_queue(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(OutputCog(bot))