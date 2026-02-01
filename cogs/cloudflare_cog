import aiohttp
import os
import logging
from discord.ext import commands

class CloudflareCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_token = os.getenv('CLOUDFLARE_API_TOKEN')
        self.zone_id = os.getenv('CLOUDFLARE_ZONE_ID')
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.logger = logging.getLogger('nydus')

    async def create_dns_record(self, subdomain, ip_address):
        if subdomain.lower() == 'nydus':
            return None, "Reserved subdomain."

        url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        data = {
            "type": "A",
            "name": subdomain, 
            "content": ip_address,
            "ttl": 1,
            "proxied": True
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                result = await response.json()
                if result.get('success'):
                    return result['result']['id'], None
                else:
                    error = result['errors'][0]['message'] if result.get('errors') else "Unknown Error"
                    self.logger.error(f"CF Error: {error}")
                    return None, error

    async def delete_dns_record(self, record_id):
        if not record_id: return
        
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{record_id}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers) as response:
                return response.status == 200

def setup(bot):
    bot.add_cog(CloudflareCog(bot))