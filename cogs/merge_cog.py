import os
import aiohttp
import asyncio
import discord
from datetime import datetime, timezone
from discord.ext import commands
from database.db import get_user, log_slash_command

GITHUB_API = "https://api.github.com"

class MergeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.default_token = os.getenv("GITHUB_TOKEN")
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        await self.session.close()

    async def github_request(self, method: str, url: str, token: str | None = None, json_data: dict | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ArvoNydus/1.0",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self.session.request(method, url, headers=headers, json=json_data, timeout=15) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return resp.status, data
        except asyncio.TimeoutError:
            return 504, {"message": "Request timed out"}
        except aiohttp.ClientError as e:
            return 503, {"message": str(e)}

    def build_embed(self, ctx: discord.ApplicationContext, success: bool, repo_url: str, title_text: str, body_text: str) -> discord.Embed:
        color = discord.Color(0x00ff99) if success else discord.Color(0xff4d6d)
        embed = discord.Embed(title=title_text[:256], description=body_text[:3500], color=color, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=ctx.user.display_name, icon_url=ctx.user.display_avatar.url)
        embed.set_thumbnail(url="https://i.imgur.com/g6QHFKR.gif")
        footer_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        embed.set_footer(text=f"Auto-merged from {repo_url} ● {footer_time}")
        return embed

    @discord.slash_command(name="merge", description="Auto-merge eligible pull requests in a repository")
    async def merge(self, ctx: discord.ApplicationContext, owner: str, repo: str, pat: str = None):
        await ctx.defer(ephemeral=True)
        discord_id = str(ctx.user.id)
        used_pat = bool(pat)
        repo_url = f"https://github.com/{owner}/{repo}"

        # Authorization check
        user = await get_user(discord_id)
        if not user:
            embed = self.build_embed(ctx, False, repo_url, "Merge Failed", "Authorization failed. User not registered.")
            await log_slash_command(discord_id, "merge", owner, repo, used_pat, False, "User not registered")
            return await ctx.followup.send(embed=embed, ephemeral=True)

        token = pat or self.default_token

        # Check repo accessibility
        status, _ = await self.github_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}", token)
        if status != 200:
            embed = self.build_embed(ctx, False, repo_url, "Merge Failed", "Repository not accessible. It may be private and require a valid PAT.")
            await log_slash_command(discord_id, "merge", owner, repo, used_pat, False, "Repository inaccessible")
            return await ctx.followup.send(embed=embed, ephemeral=True)

        # Fetch open PRs with pagination
        pulls, page = [], 1
        while True:
            status, prs = await self.github_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/pulls?state=open&per_page=50&page={page}", token)
            if status != 200 or not prs:
                break
            pulls.extend(prs)
            if len(prs) < 50:  # last page
                break
            page += 1

        if not pulls:
            embed = self.build_embed(ctx, False, repo_url, "Merge Failed", "No open pull requests found.")
            await log_slash_command(discord_id, "merge", owner, repo, used_pat, False, "No open PRs")
            return await ctx.followup.send(embed=embed, ephemeral=True)

        merged_blocks, failure_reasons = [], []

        # Process PRs sequentially to avoid abuse detection, could be parallel with care
        for pr in pulls:
            pr_number = pr["number"]
            pr_title = pr["title"]
            pr_body = pr.get("body") or "No description provided."
            pr_url = pr["html_url"]

            # Fetch PR again to check mergeable_state
            status, pr_data = await self.github_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}", token)
            if status != 200 or not pr_data or pr_data.get("mergeable_state") != "clean":
                failure_reasons.append(f"PR #{pr_number} not mergeable (state not clean).")
                continue

            # Check check-runs
            status, checks = await self.github_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/commits/{pr_data['head']['sha']}/check-runs", token)
            if status != 200 or any(run["conclusion"] != "success" and run["required"] for run in checks.get("check_runs", [])):
                failure_reasons.append(f"PR #{pr_number} has failing or incomplete checks.")
                continue

            # Attempt merge
            merge_status, merge_data = await self.github_request("PUT", f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/merge", token, json_data={"merge_method": "squash"})
            if merge_status not in (200, 201):
                failure_reasons.append(f"PR #{pr_number} failed to merge: {merge_data.get('message')}")
                continue

            # Fetch commits (limit 5)
            status, commits = await self.github_request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/commits", token)
            commit_lines = []
            if status == 200:
                for commit in commits[:5]:
                    message = commit["commit"]["message"].split("\n")[0]
                    commit_lines.append(f"- {message}")

            block = f"Pull Request: {pr_title}\nDescription: {pr_body[:500]}\n\nCommits\n{chr(10).join(commit_lines)}\n\nPR URL: {pr_url}"
            merged_blocks.append(block)

        # Build final embed
        if merged_blocks:
            content = "\n\n---\n\n".join(merged_blocks)[:3500]
            embed = self.build_embed(ctx, True, repo_url, "Merged Successfully", content)
            await log_slash_command(discord_id, "merge", owner, repo, used_pat, True, None)
            await ctx.send(embed=embed)
        else:
            reason_text = "\n".join(failure_reasons) if failure_reasons else "No eligible pull requests."
            embed = self.build_embed(ctx, False, repo_url, "Merge Failed", reason_text)
            await log_slash_command(discord_id, "merge", owner, repo, used_pat, False, reason_text)
            await ctx.send(embed=embed)

def setup(bot: commands.Bot):
    bot.add_cog(MergeCog(bot))