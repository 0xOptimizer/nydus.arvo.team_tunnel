import discord
from discord.ext import commands, tasks
import asyncio
import os

class OutputCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.channel_id = int(os.getenv('DISCORD_CHANNEL_ID', 0))
        self.message_queue = asyncio.Queue()
        self.process_queue.start()

    def cog_unload(self):
        self.process_queue.cancel()

    async def send_embed(self, title, description, color, fields=None):
        embed = discord.Embed(title=title, description=description, color=color)
        if fields:
            for name, value in fields.items():
                embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="Nydus Tunnel System")
        
        await self.message_queue.put((None, embed))

    async def queue_message(self, message, msg_type="INFO"):
        if msg_type == "ERROR":
            content = f"**ERROR:** {message}"
        elif msg_type == "SUCCESS":
            content = f"**SUCCESS:** {message}"
        else:
            content = f"{message}"

        await self.message_queue.put((content, None))

    @tasks.loop(seconds=1.0)
    async def process_queue(self):
        if self.message_queue.empty():
            return

        channel = self.bot.get_channel(self.channel_id)
        
        if not channel:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except Exception as e:
                print(f"Could not find channel {self.channel_id}: {e}")
                return

        try:
            while not self.message_queue.empty():
                content, embed = await self.message_queue.get()
                
                await channel.send(content=content, embed=embed)
                
                self.message_queue.task_done()
                await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"Failed to send message: {e}")

    @process_queue.before_loop
    async def before_process_queue(self):
        await self.bot.wait_until_ready()

def setup(bot):
    bot.add_cog(OutputCog(bot))