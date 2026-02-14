import discord
from discord.ext import commands, tasks
import asyncio
import os
import json

class OutputView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Access Nydus via browser", url="https://nydus.arvo.team", style=discord.ButtonStyle.success))

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

    async def send_embed(self, title, description, color=discord.Color.default(), fields=None, thumbnail=None, image=None, author_name=None, author_icon=None, footer_text=None):
        embed = discord.Embed(title=str(title), description=str(description), color=color)
        
        if author_name:
            embed.set_author(name=str(author_name), icon_url=author_icon)
        
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
            
        if image:
            embed.set_image(url=image)

        if fields:
            for name, value in fields.items():
                embed.add_field(name=str(name), value=str(value), inline=False)
        
        link = "https://nydus.arvo.team"
        if footer_text:
            embed.set_footer(text=f"{footer_text}")
        else:
            embed.set_footer(text=f"Sent from {link}")
            
        await self.message_queue.put((None, embed))

    async def queue_message(self, message, msg_type="INFO"):
        if not isinstance(message, str):
            message = json.dumps(message, default=str)

        content = f"**{msg_type}**: {message}"
        await self.message_queue.put((content, None))

    @tasks.loop(seconds=0.5)
    async def process_queue(self):
        if self.message_queue.empty():
            return
        try:
            content, embed = await self.message_queue.get()
            for channel_id in self.channel_ids:
                try:
                    target_id = int(channel_id)
                    channel = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
                    if channel:
                        await channel.send(content=content, embed=embed, view=OutputView())
                except Exception:
                    continue
            self.message_queue.task_done()
        except Exception:
            pass

    @process_queue.before_loop
    async def before_process_queue(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(OutputCog(bot))