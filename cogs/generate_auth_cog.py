import discord
from discord.ext import commands
from database.db import add_auth_key
from datetime import datetime, timezone

class GenerateAuthCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="generate", description="Generate a new auth key for an app")
    async def generate(self, ctx: discord.ApplicationContext, app_name: str):
        discord_id = str(ctx.author.id)
        result = await add_auth_key(discord_id, app_name)

        if result.get("success"):
            # Only respond with the key, ephemeral
            await ctx.respond(result['secret'], ephemeral=True)
        else:
            await ctx.respond("Failed to generate auth key.", ephemeral=True)

def setup(bot):
    bot.add_cog(GenerateAuthCog(bot))