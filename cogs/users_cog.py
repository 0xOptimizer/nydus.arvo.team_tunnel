import discord
from discord import app_commands
from discord.ext import commands
import os
from database.db import add_user, remove_user, get_user

class UsersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dev_id = int(os.environ.get('DEV_ID', 0))

    async def check_dev(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.dev_id:
            return True
        
        # Security: Reply strictly to the user, don't expose logic
        await interaction.response.send_message(
            f"⛔ You are not authorized to use this command. Expected: <@{self.dev_id}>", 
            ephemeral=True
        )
        return False

    @app_commands.command(name="add", description="Add a user to the allowed list")
    @app_commands.describe(user="The user to add")
    async def add(self, interaction: discord.Interaction, user: discord.User):
        if not await self.check_dev(interaction):
            return

        # Defer immediately to prevent timeouts
        await interaction.response.defer(ephemeral=True)

        output_cog = self.bot.get_cog('OutputCog')
        
        # Check existence first
        existing = await get_user(str(user.id))
        if existing:
            msg = f"User {user.mention} is already in the database."
            if output_cog:
                await output_cog.queue_message(msg, "ERROR")
            await interaction.followup.send(msg)
            return

        # Perform Insert
        result = await add_user(str(user.id), user.name)

        if result:
            success_msg = f"User {user.mention} has been added to the database."
            if output_cog:
                await output_cog.send_embed(
                    title="User Access Granted",
                    description=success_msg,
                    color=discord.Color.green(),
                    fields={"Username": user.name, "ID": str(user.id)}
                )
            await interaction.followup.send(success_msg)
        else:
            error_msg = f"Database error while adding {user.mention}."
            if output_cog:
                await output_cog.queue_message(error_msg, "ERROR")
            await interaction.followup.send(error_msg)

    @app_commands.command(name="remove", description="Remove a user from the allowed list")
    @app_commands.describe(user="The user to remove")
    async def remove(self, interaction: discord.Interaction, user: discord.User):
        if not await self.check_dev(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        result = await remove_user(str(user.id))
        output_cog = self.bot.get_cog('OutputCog')

        if result:
            success_msg = f"User {user.mention} has been removed from the database."
            if output_cog:
                await output_cog.send_embed(
                    title="User Access Revoked",
                    description=success_msg,
                    color=discord.Color.red(),
                    fields={"Username": user.name, "ID": str(user.id)}
                )
            await interaction.followup.send(success_msg)
        else:
            error_msg = f"Database error while removing {user.mention}."
            if output_cog:
                await output_cog.queue_message(error_msg, "ERROR")
            await interaction.followup.send(error_msg)

async def setup(bot):
    await bot.add_cog(UsersCog(bot))