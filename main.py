import discord
import os
import logging
import asyncio
from dotenv import load_dotenv
from database.db import init_db

load_dotenv()

if not os.path.exists('logs'):
    os.makedirs('logs')

logging.basicConfig(
    filename='logs/nydus.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s: %(message)s'
)

intents = discord.Intents.default()
bot = discord.Bot(intents=intents)

cogs_list = [
    'cogs.output_cog',
    'cogs.monitoring_cog',
    'cogs.nginx_cog',
    'cogs.deployment_cog',
    'cogs.api_cog'
]

@bot.event
async def on_ready():
    await init_db()
    logging.info(f'Nydus Tunnel active as {bot.user}')

for cog in cogs_list:
    try:
        bot.load_extension(cog)
    except Exception as e:
        logging.error(f"Failed to load cog {cog}: {e}")

if __name__ == "__main__":
    bot.run(os.getenv('NYDUS_BOT_TOKEN_ID'))