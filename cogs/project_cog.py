import discord
from discord.ext import commands
from discord import option
import os
import traceback
from database.db import (
    add_github_project, 
    get_github_project, 
    get_all_github_projects, 
    remove_github_project
)

class ProjectCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dev_id = int(os.environ.get("DEV_ID", 0))

    async def check_dev(self, ctx: discord.ApplicationContext) -> bool:
        if ctx.author.id == self.dev_id:
            return True
        await ctx.respond(f"Unauthorized. Developer access only.")
        return False

    project = discord.SlashCommandGroup("project", "Manage GitHub project records")

    @project.command(name="add", description="Add a new GitHub project to the database")
    @option("name", description="The name of the repository")
    @option("owner", description="GitHub username or organization name")
    @option("url_path", description="The web URL path to the project")
    @option("git_url", description="The .git clone URL")
    @option("ssh_url", description="The SSH clone URL")
    @option("owner_type", choices=["User", "Organization"], default="User")
    @option("visibility", choices=["public", "private", "internal"], default="public")
    @option("branch", description="Default branch name", default="main")
    @option("description", description="Short project description", default=None)
    async def add_project(
        self, 
        ctx: discord.ApplicationContext, 
        name: str, 
        owner: str, 
        url_path: str, 
        git_url: str, 
        ssh_url: str, 
        owner_type: str, 
        visibility: str, 
        branch: str, 
        description: str
    ):
        if not await self.check_dev(ctx):
            return

        await ctx.defer()
        
        project_uuid = await add_github_project(
            name, owner, owner_type, description, url_path, git_url, ssh_url, visibility, branch
        )

        if project_uuid:
            embed = discord.Embed(
                title="Project Registered",
                description=f"Successfully added {name} to the database.",
                color=discord.Color.blue()
            )
            embed.add_field(name="UUID", value=f"`{project_uuid}`", inline=False)
            embed.add_field(name="Owner", value=owner, inline=True)
            embed.add_field(name="Visibility", value=visibility, inline=True)
            await ctx.followup.send(embed=embed)
        else:
            await ctx.followup.send("Failed to add project to the database.")

    @project.command(name="list", description="List all registered GitHub projects")
    async def list_projects(self, ctx: discord.ApplicationContext):
        if not await self.check_dev(ctx):
            return

        await ctx.defer()
        projects = await get_all_github_projects()

        if not projects:
            await ctx.followup.send("No projects found in the database.")
            return

        embed = discord.Embed(
            title="GitHub Projects",
            color=discord.Color.purple()
        )

        for p in projects[:10]:
            info = f"Owner: {p['owner_login']} | Branch: {p['default_branch']}\nUUID: `{p['project_uuid']}`"
            embed.add_field(name=p['name'], value=info, inline=False)

        if len(projects) > 10:
            embed.set_footer(text=f"Showing 10 of {len(projects)} projects.")

        await ctx.followup.send(embed=embed)

    @project.command(name="remove", description="Remove a project by its UUID")
    @option("uuid", description="The unique ID of the project")
    async def remove_project(self, ctx: discord.ApplicationContext, uuid: str):
        if not await self.check_dev(ctx):
            return

        await ctx.defer()
        project = await get_github_project(uuid)
        
        if not project:
            await ctx.followup.send("Project with that UUID not found.")
            return

        await remove_github_project(uuid)
        await ctx.followup.send(f"Successfully removed project: {project['name']}")

def setup(bot):
    bot.add_cog(ProjectCog(bot))