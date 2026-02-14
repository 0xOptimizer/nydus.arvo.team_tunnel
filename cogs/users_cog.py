import discord
from discord.ext import commands
from discord import option
import os
import re
import asyncio
import traceback
from database.db import add_user, remove_user, get_user

class UsersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dev_id = int(os.environ.get("DEV_ID", 0))
        self.role_id = int(os.environ.get("DISCORD_ROLE_AUTHENTICATED_NYDUS", 0))
        self._lock = asyncio.Lock()
        self._id_cache = set()

    async def check_dev(self, ctx: discord.ApplicationContext) -> bool:
        if ctx.author.id == self.dev_id:
            return True
        await ctx.respond(f"Unauthorized. You are not <@{self.dev_id}>.")
        return False

    async def _update_member_role(self, guild: discord.Guild, user_id: int, role: discord.Role, add: bool):
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            if member:
                if add:
                    await member.add_roles(role)
                else:
                    await member.remove_roles(role)
        except Exception:
            pass

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

        if not role:
            await ctx.followup.send("Target role not found in server.")
            return

        async with self._lock:
            for count, user_id in enumerate(user_ids):
                str_id = str(user_id)
                target_id = int(user_id)
                
                if str_id in self._id_cache:
                    continue

                try:
                    if await get_user(str_id):
                        self._id_cache.add(str_id)
                        error_list.append(f"ID {user_id} is already registered.")
                        continue

                    user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
                    if await add_user(str_id, user.name):
                        self._id_cache.add(str_id)
                        added_list.append(f"{user.mention} ({user.id})")
                        await self._update_member_role(ctx.guild, target_id, role, True)

                    if count % 5 == 0 and count > 0:
                        await asyncio.sleep(0.1)

                except Exception as e:
                    error_list.append(f"Failed {user_id}: {str(e)}")

        if output_cog and added_list:
            header = "The following users can now log in to the Nydus:\n\n"
            user_text = "\n".join(added_list)
            full_description = f"{header}{user_text}"
            
            if len(full_description) > 4000:
                full_description = f"{header}Batch of {len(added_list)} users added successfully."
            
            await output_cog.send_embed(
                title="Access Granted",
                description=full_description,
                color=discord.Color.green(),
                thumbnail="https://i.imgur.com/4N8iddi.gif"
            )

        if output_cog and error_list:
            await output_cog.queue_message("\n".join(error_list[:10]), "ERROR")

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
                str_id = str(user_id)
                target_id = int(user_id)

                try:
                    if await get_user(str_id) or await get_user(target_id):
                        await remove_user(str_id)
                        await remove_user(target_id)
                        self._id_cache.discard(str_id)
                        removed_list.append(str_id)
                        await self._update_member_role(ctx.guild, target_id, role, False)
                    else:
                        error_list.append(f"ID {user_id} not found in database.")

                    if count % 10 == 0 and count > 0:
                        await asyncio.sleep(0.05)

                except Exception as e:
                    error_list.append(f"Error {user_id}: {str(e)}")

        if output_cog and removed_list:
            header = "The following users no longer have access to the Nydus:\n\n"
            user_text = ", ".join(removed_list)
            full_description = f"{header}{user_text}"
            
            if len(full_description) > 4000:
                full_description = f"{header}Successfully removed {len(removed_list)} users."

            await output_cog.send_embed(
                title="Access Revoked",
                description=full_description,
                color=discord.Color.red(),
                thumbnail="https://i.imgur.com/mMmB2sg.gif"
            )

        if output_cog and error_list:
            await output_cog.queue_message("\n".join(error_list[:10]), "ERROR")

        await ctx.followup.send("Batch removal complete!")

def setup(bot):
    bot.add_cog(UsersCog(bot))