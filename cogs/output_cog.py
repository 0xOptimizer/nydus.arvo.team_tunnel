import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
from datetime import datetime

class OutputView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Access the Nydus", url="https://nydus.arvo.team"))

class OutputCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        raw_channels = os.getenv("DEFAULT_OUTPUT_CHANNELS", "[]")
        try:
            self.channel_ids = json.loads(raw_channels)
            if isinstance(self.channel_ids, int):
                self.channel_ids = [self.channel_ids]
        except Exception:
            self.channel_ids = []
            
        self.message_queue = asyncio.Queue()
        self.process_queue.start()

    def cog_unload(self):
        self.process_queue.cancel()

    async def send_embed(self, title, description, color, fields=None):
        embed = discord.Embed(title=str(title), description=str(description), color=color)
        if fields:
            for name, value in fields.items():
                embed.add_field(name=str(name), value=str(value), inline=False)
        embed.set_footer(text="https://nydus.arvo.team • Nydus Tunnel Network")
        
        await self.message_queue.put((None, embed))

    async def queue_message(self, message, msg_type="INFO"):
        if not isinstance(message, str):
            try:
                message = json.dumps(message, default=str)
            except Exception:
                message = str(message)

        content = f"**{msg_type}**: {message}"
        await self.message_queue.put((content, None))

    @tasks.loop(seconds=0.5)
    async def process_queue(self):
        if self.message_queue.empty():
            return

        try:
            content, embed = await self.message_queue.get()
            dispatch_tasks = [self.dispatch_message(cid, content, embed) for cid in self.channel_ids]
            if dispatch_tasks:
                await asyncio.gather(*dispatch_tasks)
            self.message_queue.task_done()
        except Exception:
            pass

    async def dispatch_message(self, channel_id, content, embed):
        try:
            target_id = int(channel_id)
            channel = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
            if channel:
                await channel.send(content=content, embed=embed, view=OutputView())
        except Exception:
            pass

    @process_queue.before_loop
    async def before_process_queue(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(OutputCog(bot))