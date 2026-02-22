import aiohttp
import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone
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

        async with aiohttp.ClientSession() as session:
            for attempt in range(3):
                try:
                    async with session.request(method, url, json=json, params=params, headers=headers) as response:
                        if response.status == 429:
                            retry_after = int(response.headers.get("Retry-After", 1))
                            self.logger.warning(f"Rate limited. Sleeping for {retry_after}s.")
                            await asyncio.sleep(retry_after)
                            continue

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
        
        return None, "Max retries exceeded."

    async def _make_graphql_request(self, query: str, variables: dict) -> Tuple[Optional[Any], Optional[str]]:
        url = f"{self.base_url}/graphql"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        payload = {"query": query, "variables": variables}

        async with aiohttp.ClientSession() as session:
            for attempt in range(3):
                try:
                    async with session.post(url, json=payload, headers=headers) as response:
                        if response.status == 429:
                            retry_after = int(response.headers.get("Retry-After", 1))
                            await asyncio.sleep(retry_after)
                            continue

                        data = await response.json()
                        if data.get('errors'):
                            return None, data['errors'][0]['message']
                        return data.get('data'), None
                except Exception as e:
                    return None, str(e)

        return None, "Max retries exceeded."

    async def get_visitor_stats(self, days: int = 30) -> Tuple[Optional[List[Dict]], Optional[str]]:
        if days > 30:
            days = 30

        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=days)

        query = """
        query GetUniqueVisitors($zoneTag: String!, $startDate: Date!, $endDate: Date!) {
          viewer {
            zones(filter: { zoneTag: $zoneTag }) {
              httpRequests1dGroups(
                limit: 31,
                filter: { date_geq: $startDate, date_leq: $endDate },
                orderBy: [date_ASC]
              ) {
                dimensions {
                  date
                }
                uniq {
                  uniques
                }
              }
            }
          }
        }
        """

        variables = {
            "zoneTag": self.zone_id,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat()
        }

        data, error = await self._make_graphql_request(query, variables)
        if error:
            return None, error

        try:
            zones = data.get('viewer', {}).get('zones', [])
            if not zones:
                return [], None

            stats = zones[0].get('httpRequests1dGroups', [])
            formatted_stats = [
                {
                    "date": item['dimensions']['date'],
                    "visitors": item['uniq']['uniques']
                }
                for item in stats
            ]
            return formatted_stats, None
        except (KeyError, IndexError) as e:
            return None, f"Data parsing error: {str(e)}"

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

    async def get_dynamic_analytics(self, days: int = 30) -> Tuple[Optional[Dict], Optional[str]]:
        if days > 30:
            days = 30
        
        start_time = datetime.now(timezone.utc) - timedelta(days=days)
        start_time_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')

        query = """
            query GetDynamicStats($zoneTag: String!, $startTime: DateTime!) {
            viewer {
                zones(filter: { zoneTag: $zoneTag }) {
                httpRequestsAdaptiveGroups(
                    limit: 1000,
                    filter: { datetime_geq: $startTime },
                    orderBy: [datetime_ASC]
                ) {
                    dimensions {
                    datetime
                    clientCountryName
                    userAgentOS
                    userAgentBrowser
                    deviceType
                    }
                    sum {
                    requests
                    edgeResponseBytes
                    visits
                    }
                }
                }
            }
            }
            """
        
        variables = {
            "zoneTag": self.zone_id,
            "startTime": start_time_str
        }

        data, error = await self._make_graphql_request(query, variables)
        if error:
            return None, error

        try:
            zones = data.get('viewer', {}).get('zones', [])
            if not zones:
                return {"data": [], "granularity": "adaptive"}, None

            raw_stats = zones[0].get('httpRequestsAdaptiveGroups', [])
            history = []

            for item in raw_stats:
                dims = item.get('dimensions', {})
                sums = item.get('sum', {})
                visits = sums.get('visits', 0)
                
                point = {
                    "timestamp": dims.get('datetime'),
                    "visitors": visits,
                    "bandwidth_gb": round(sums.get('edgeResponseBytes', 0) / (1024**3), 4),
                    "requests": sums.get('requests', 0),
                    "countries": {dims.get('clientCountryName', 'Unknown'): visits},
                    "devices": {dims.get('deviceType', 'Unknown'): visits},
                    "browsers": {dims.get('userAgentBrowser', 'Unknown'): visits},
                    "os": {dims.get('userAgentOS', 'Unknown'): visits}
                }
                history.append(point)

            return {"data": history, "granularity": "hourly" if days <= 3 else "daily"}, None
        except (KeyError, IndexError) as e:
            return None, f"Data parsing error: {str(e)}"

def setup(bot):
    bot.add_cog(CloudflareCog(bot))