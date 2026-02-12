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
        self.dev_id = int(os.environ.get('DEV_ID', 0))

    async def check_dev(self, ctx: discord.ApplicationContext) -> bool:
        if ctx.author.id == self.dev_id:
            return True

        dev_user = self.bot.get_user(self.dev_id)
        dev_mention = dev_user.mention if dev_user else f"<@{self.dev_id}>"

        await ctx.respond(
            f"You are not {dev_mention}",
            ephemeral=True
        )
        return False

    def split_message(self, message, max_length=2000):
        return [message[i:i+max_length] for i in range(0, len(message), max_length)]

    @discord.slash_command(name="add", description="Allows users to enter the Nydus")
    @option("users", description="User IDs or mentions")
    async def add(
        self,
        ctx: discord.ApplicationContext,
        users: str
    ):
        if not await self.check_dev(ctx):
            return

        await ctx.respond("Processing additions...", ephemeral=True)

        output_cog = self.bot.get_cog('OutputCog')
        user_ids = re.findall(r"\d+", users)

        if not user_ids:
            if output_cog:
                await output_cog.queue_message("No valid IDs found.", "ERROR")
            return

        added_list = []
        error_list = []

        for user_id in set(user_ids):
            try:
                user = await self.bot.fetch_user(int(user_id))
                await asyncio.sleep(0.1)
                existing = await get_user(str(user.id))

                if existing:
                    error_list.append(f"User {user.mention} is already in the database.")
                    continue

                result = await add_user(str(user.id), user.name)

                if result:
                    added_list.append(f"{user.mention} ({user.id})")
                else:
                    error_list.append(f"Database error for {user_id}.")
            except Exception as e:
                error_list.append(f"Error processing {user_id}: {str(e)}")

        if output_cog:
            if added_list:
                full_message = "\n".join(added_list)
                for chunk in self.split_message(full_message):
                    await output_cog.send_embed(
                        title="Access Granted",
                        description=chunk,
                        color=discord.Color.green()
                    )
            if error_list:
                full_message = "\n".join(error_list)
                for chunk in self.split_message(full_message):
                    await output_cog.queue_message(chunk, "ERROR")

    @discord.slash_command(name="remove", description="Disallows users from entering the Nydus")
    @option("users", description="User IDs or mentions")
    async def remove(
        self,
        ctx: discord.ApplicationContext,
        users: str
    ):
        if not await self.check_dev(ctx):
            return

        await ctx.respond("Processing removals...", ephemeral=True)

        output_cog = self.bot.get_cog('OutputCog')
        user_ids = re.findall(r"\d+", users)

        if not user_ids:
            if output_cog:
                await output_cog.queue_message("No valid IDs found.", "ERROR")
            return

        removed_list = []
        error_list = []

        for user_id in set(user_ids):
            try:
                result = await remove_user(str(user_id))
                if result:
                    removed_list.append(str(user_id))
                else:
                    error_list.append(f"User {user_id} not found in database.")
            except Exception as e:
                error_list.append(f"Error removing {user_id}: {str(e)}")

        if output_cog:
            if removed_list:
                full_message = f"Removed IDs: {', '.join(removed_list)}"
                for chunk in self.split_message(full_message):
                    await output_cog.send_embed(
                        title="Access Revoked",
                        description=chunk,
                        color=discord.Color.red()
                    )
            if error_list:
                full_message = "\n".join(error_list)
                for chunk in self.split_message(full_message):
                    await output_cog.queue_message(chunk, "ERROR")

def setup(bot):
    bot.add_cog(UsersCog(bot))