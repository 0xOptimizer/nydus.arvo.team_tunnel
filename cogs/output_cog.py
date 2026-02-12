import discord
from discord.ext import commands, tasks
import asyncio
import os
import json

class OutputView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(discord.ui.Button(label=1, url=2))

    def update_button(self):
        self.clear_items()
        self.add_item(discord.ui.Button(label="Access the Nydus", url="https://nydus.arvo.team"))

class OutputCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        raw_channels = os.getenv('DEFAULT_OUTPUT_CHANNELS', '[]')
        try:
            self.channel_ids = json.loads(raw_channels)
            if isinstance(self.channel_ids, int):
                self.channel_ids = [self.channel_ids]
        except (json.JSONDecodeError, TypeError):
            print("CRITICAL: DEFAULT_OUTPUT_CHANNELS is not a valid JSON list.")
            self.channel_ids = []
            
        self.message_queue = asyncio.Queue()
        self.process_queue.start()

    def cog_unload(self):
        self.process_queue.cancel()

    async def send_embed(self, title, description, color, fields=None):
        embed = discord.Embed(title=title, description=description, color=color)
        if fields:
            for name, value in fields.items():
                embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="https://nydus.arvo.team • Nydus Tunnel Network")
        
        await self.message_queue.put((None, embed))

    async def queue_message(self, message, msg_type="INFO"):
        prefixes = {
            "ERROR": "",
            "SUCCESS": "",
            "INFO": ""
        }
        content = f"{prefixes.get(msg_type, '')}{message}"
        await self.message_queue.put((content, None))

    @tasks.loop(seconds=0.2)
    async def process_queue(self):
        if self.message_queue.empty():
            return

        content, embed = await self.message_queue.get()

        dispatch_tasks = []
        for channel_id in self.channel_ids:
            dispatch_tasks.append(self.dispatch_message(channel_id, content, embed))
        
        if dispatch_tasks:
            await asyncio.gather(*dispatch_tasks)
        
        self.message_queue.task_done()

    async def dispatch_message(self, channel_id, content, embed):
        try:
            target_id = int(channel_id)
            channel = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
            
            if channel:
                view = OutputView()
                view.update_button()
                await channel.send(content=content, embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Failed to send to {channel_id}: {e}")
        except Exception as e:
            print(f"Unexpected error for {channel_id}: {e}")

    @process_queue.before_loop
    async def before_process_queue(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(OutputCog(bot))