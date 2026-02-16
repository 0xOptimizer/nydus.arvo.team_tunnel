import aiohttp
import os
import logging
from typing import Optional, List, Dict, Any, Tuple
from discord.ext import commands

class CloudflareCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_token = os.getenv('CLOUDFLARE_API_TOKEN')
        self.zone_id = os.getenv('CLOUDFLARE_ZONE_ID')
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.logger = logging.getLogger('nydus')

    async def _make_request(self, method: str, endpoint: str, json: dict = None, params: dict = None) -> Tuple[Optional[Any], Optional[str]]:
        url = f"{self.base_url}/zones/{self.zone_id}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

        # Retry loop for rate limits
        for attempt in range(3): 
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.request(method, url, json=json, params=params, headers=headers) as response:
                        # Cloudflare Rate Limit Handling
                        if response.status == 429:
                            retry_after = int(response.headers.get("Retry-After", 1))
                            self.logger.warning(f"Rate limited. Sleeping for {retry_after}s.")
                            import asyncio
                            await asyncio.sleep(retry_after)
                            continue # Retry the request

                        data = await response.json()
                        
                        if data.get('success'):
                            return data.get('result'), None
                        
                        error_msg = "Unknown Error"
                        if data.get('errors') and len(data['errors']) > 0:
                            error_msg = data['errors'][0]['message']
                        
                        self.logger.error(f"Cloudflare API Error: {error_msg}")
                        return None, error_msg

                except Exception as e:
                    self.logger.error(f"Request Exception: {str(e)}")
                    return None, str(e)
        
        return None, "Max retries exceeded due to rate limiting."

    async def list_dns_records(self, type: str = None, name: str = None, page: int = 1, per_page: int = 100) -> Tuple[Optional[List[Dict]], Optional[str]]:
        params = {
            "page": page,
            "per_page": per_page,
            "order": "type",
            "direction": "asc"
        }
        
        if type:
            params['type'] = type
        if name:
            params['name'] = name

        return await self._make_request("GET", "dns_records", params=params)

    async def create_dns_record(self, type: str, name: str, content: str, ttl: int = 1, proxied: bool = True, comment: str = "") -> Tuple[Optional[Dict], Optional[str]]:
        if name.lower() == 'nydus':
            return None, "Reserved subdomain."

        data = {
            "type": type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
            "comment": comment
        }
        
        return await self._make_request("POST", "dns_records", json=data)

    async def update_dns_record(self, record_id: str, type: str, name: str, content: str, ttl: int = 1, proxied: bool = True, comment: str = "") -> Tuple[Optional[Dict], Optional[str]]:
        if not record_id:
            return None, "Record ID is required."

        data = {
            "type": type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
            "comment": comment
        }
        
        return await self._make_request("PUT", f"dns_records/{record_id}", json=data)

    async def delete_dns_record(self, record_id: str) -> Tuple[bool, Optional[str]]:
        if not record_id:
            return False, "Record ID is required."
        
        result, error = await self._make_request("DELETE", f"dns_records/{record_id}")
        
        if error:
            return False, error
        
        return True, None

def setup(bot):
    bot.add_cog(CloudflareCog(bot))