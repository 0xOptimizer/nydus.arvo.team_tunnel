import discord
from discord.ext import commands
from discord import option
import os
import re
import asyncio
from database.db import add_user, remove_user, get_user

class UsersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dev_id = int(os.environ.get("DEV_ID", 0))
        self.role_id = 1472305983815549032
        self._lock = asyncio.Lock()
        self._id_cache = set()

    async def check_dev(self, ctx: discord.ApplicationContext) -> bool:
        if ctx.author.id == self.dev_id:
            return True
        await ctx.respond("Unauthorized.", ephemeral=True)
        return False

    @discord.slash_command(name="add", description="Allows users to enter the Nydus")
    @option("users", description="User IDs or mentions")
    async def add(self, ctx: discord.ApplicationContext, users: str):
        if not await self.check_dev(ctx):
            return
        await ctx.defer()

        output_cog = self.bot.get_cog("OutputCog")
        user_ids = list(set(re.findall(r"\d+", users)))
        added_list, error_list = [], []
        role = ctx.guild.get_role(self.role_id)

        async with self._lock:
            for count, user_id in enumerate(user_ids):
                try:
                    str_id = str(user_id)
                    if str_id in self._id_cache:
                        continue

                    target_id = int(user_id)
                    user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
                    
                    if await get_user(str_id):
                        self._id_cache.add(str_id)
                        error_list.append(f"{user.mention} is already in the database.")
                    elif await add_user(str_id, user.name):
                        self._id_cache.add(str_id)
                        added_list.append(f"{user.mention} ({user.id})")

                        if role:
                            try:
                                member = ctx.guild.get_member(target_id) or await ctx.guild.fetch_member(target_id)
                                if member:
                                    await member.add_roles(role)
                            except discord.HTTPException:
                                pass
                    
                    if count % 5 == 0 and count > 0:
                        await asyncio.sleep(0.05)
                except Exception as e:
                    error_list.append(f"Error {user_id}: {str(e)}")

        if output_cog:
            for i in range(0, len(added_list), 20):
                await output_cog.send_embed("Access Granted", "\n".join(added_list[i:i + 20]), discord.Color.green())
            for i in range(0, len(error_list), 10):
                await output_cog.queue_message("\n".join(error_list[i:i + 10]), "ERROR")

        await ctx.followup.send("Batch processing complete!")

    @discord.slash_command(name="remove", description="Disallows users from entering the Nydus")
    @option("users", description="User IDs or mentions")
    async def remove(self, ctx: discord.ApplicationContext, users: str):
        if not await self.check_dev(ctx):
            return
        await ctx.defer()

        output_cog = self.bot.get_cog("OutputCog")
        user_ids = list(set(re.findall(r"\d+", users)))
        removed_list, error_list = [], []
        role = ctx.guild.get_role(self.role_id)

        async with self._lock:
            for count, user_id in enumerate(user_ids):
                try:
                    str_id = str(user_id)
                    target_id = int(user_id)
                    
                    success = await remove_user(str_id)
                    if not success:
                        success = await remove_user(target_id)

                    if success:
                        self._id_cache.discard(str_id)
                        removed_list.append(str_id)

                        if role:
                            try:
                                member = ctx.guild.get_member(target_id) or await ctx.guild.fetch_member(target_id)
                                if member:
                                    await member.remove_roles(role)
                            except (discord.NotFound, discord.HTTPException):
                                pass
                    else:
                        error_list.append(f"ID {user_id} not found.")
                    
                    if count % 10 == 0 and count > 0:
                        await asyncio.sleep(0.01)
                except Exception as e:
                    error_list.append(f"Error: {str(e)}")

        if output_cog:
            for i in range(0, len(removed_list), 20):
                await output_cog.send_embed("Access Revoked", f"Removed IDs:\n{', '.join(removed_list[i:i + 20])}", discord.Color.red())
            if error_list:
                for i in range(0, len(error_list), 10):
                    await output_cog.queue_message("\n".join(error_list[i:i + 10]), "ERROR")

        await ctx.followup.send("Removal processing complete!")

def setup(bot):
    bot.add_cog(UsersCog(bot))