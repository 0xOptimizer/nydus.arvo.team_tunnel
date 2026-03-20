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
        self.issued_ids = {}

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

    @commands.slash_command(name="get_tokens", description="Get up to 10 unused perishable tokens per company")
    async def get_tokens(self, ctx):
        await ctx.defer(ephemeral=True)
        try:
            results = await self._fetch_tokens_all_companies()
            if not results:
                await ctx.respond("No companies or tokens found.")
                return

            sections = []
            for company_name, tokens in results.items():
                if tokens:
                    token_list = "\n".join(f"`{t}`" for t in tokens)
                    sections.append(f"**{company_name}**\n{token_list}")
                else:
                    sections.append(f"**{company_name}**\nNo tokens available.")

            await ctx.respond("\n\n".join(sections))
        except Exception as e:
            await ctx.respond(f"An error occurred: {e}")

    async def _fetch_tokens_all_companies(self):
        async with self._lock:
            await self._ensure_pool()
            async with self.main_pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute("SELECT company_uuid, company_name FROM companies ORDER BY company_name ASC")
                    companies = await cursor.fetchall()

                if not companies:
                    return {}

                results = {}
                for company in companies:
                    uuid = company['company_uuid']
                    name = company['company_name']
                    issued = self.issued_ids.get(uuid, set())

                    async with conn.cursor(aiomysql.DictCursor) as cursor:
                        if issued:
                            placeholders = ','.join(['%s'] * len(issued))
                            query = f"""
                                SELECT id, otp_code FROM tokens
                                WHERE company_uuid = %s
                                  AND is_used = 0 AND is_perishable = 1
                                  AND (expires_at IS NULL OR expires_at > NOW())
                                  AND id NOT IN ({placeholders})
                                ORDER BY created_at ASC
                                LIMIT %s
                            """
                            params = [uuid] + list(issued) + [10]
                        else:
                            query = """
                                SELECT id, otp_code FROM tokens
                                WHERE company_uuid = %s
                                  AND is_used = 0 AND is_perishable = 1
                                  AND (expires_at IS NULL OR expires_at > NOW())
                                ORDER BY created_at ASC
                                LIMIT %s
                            """
                            params = [uuid, 10]

                        await cursor.execute(query, params)
                        rows = await cursor.fetchall()

                    if rows:
                        new_ids = {row['id'] for row in rows}
                        self.issued_ids.setdefault(uuid, set()).update(new_ids)
                        results[name] = [row['otp_code'] for row in rows]
                    else:
                        results[name] = []

                return results

    @commands.slash_command(name="flush_tokens", description="Reset all issued tokens tracking globally")
    async def flush_tokens(self, ctx):
        async with self._lock:
            self.issued_ids.clear()
        await ctx.respond("Issued tokens tracking has been reset.", ephemeral=True)

def setup(bot):
    bot.add_cog(ElectionTokenCog(bot))