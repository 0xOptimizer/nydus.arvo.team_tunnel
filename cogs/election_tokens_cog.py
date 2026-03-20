import os
import asyncio
import aiomysql
import discord
from discord.ext import commands

class ElectionTokenCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.main_pool = None
        self._lock = asyncio.Lock()
        self.issued_ids = set()

    async def _ensure_pool(self):
        if self.main_pool is None:
            self.main_pool = await aiomysql.create_pool(
                host=os.getenv("ELECTION_DB_HOST"),
                port=int(os.getenv("ELECTION_DB_PORT")),
                user=os.getenv("ELECTION_DB_USER"),
                password=os.getenv("ELECTION_DB_PASSWORD"),
                db=os.getenv("ELECTION_DB_NAME"),
                autocommit=True,
                maxsize=5
            )

    async def cog_unload(self):
        if self.main_pool:
            self.main_pool.close()
            await self.main_pool.wait_closed()
            self.main_pool = None

    @commands.slash_command(name="get_tokens", description="Get up to 10 unused perishable tokens")
    async def get_tokens(self, ctx):
        await ctx.defer()
        try:
            tokens = await self._fetch_tokens(limit=10)
            if not tokens:
                await ctx.respond("No tokens available right now.", ephemeral=True)
                return

            token_list = "\n".join(f"`{t}`" for t in tokens)
            await ctx.respond(f"{token_list}", ephemeral=True)
        except Exception as e:
            await ctx.respond(f"An error occurred: {e}")

    async def _fetch_tokens(self, limit: int):
        async with self._lock:
            await self._ensure_pool()
            async with self.main_pool.acquire() as conn:
                if self.issued_ids:
                    placeholders = ','.join(['%s'] * len(self.issued_ids))
                    query = f"""
                        SELECT id, otp_code FROM tokens
                        WHERE is_used = 0 AND is_perishable = 1
                          AND (expires_at IS NULL OR expires_at > NOW())
                          AND id NOT IN ({placeholders})
                        ORDER BY created_at ASC
                        LIMIT %s
                    """
                    params = list(self.issued_ids) + [limit]
                else:
                    query = """
                        SELECT id, otp_code FROM tokens
                        WHERE is_used = 0 AND is_perishable = 1
                          AND (expires_at IS NULL OR expires_at > NOW())
                        ORDER BY created_at ASC
                        LIMIT %s
                    """
                    params = [limit]

                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(query, params)
                    rows = await cursor.fetchall()

            if not rows:
                return []

            new_ids = {row['id'] for row in rows}
            self.issued_ids.update(new_ids)

            return [row['otp_code'] for row in rows]

    @commands.slash_command(name="flush_tokens", description="Reset the issued tokens tracking")
    async def flush_tokens(self, ctx):
        async with self._lock:
            self.issued_ids.clear()
        await ctx.respond("Issued tokens tracking has been reset.", ephemeral=True)

def setup(bot):
    bot.add_cog(ElectionTokenCog(bot))