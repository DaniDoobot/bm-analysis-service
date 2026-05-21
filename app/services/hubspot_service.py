"""
HubSpot service — wraps HubSpot CRM API v3.
"""
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

HUBSPOT_API_BASE = "https://api.hubapi.com"


class HubSpotService:
    def __init__(self):
        self.token = settings.hubspot_access_token
        self.portal_id = settings.hubspot_portal_id
        if not self.token:
            logger.warning("HUBSPOT_ACCESS_TOKEN not set — HubSpot calls will fail")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def build_hubspot_url(self, call_id: str) -> str:
        if self.portal_id:
            return f"https://app.hubspot.com/calls/{self.portal_id}/review/{call_id}"
        return f"https://app.hubspot.com/calls/review/{call_id}"

    async def get_call(self, call_id: str) -> dict[str, Any]:
        """
        Fetch call engagement from HubSpot CRM API.
        Returns normalized metadata dict.
        """
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/calls/{call_id}"
        params = {
            "properties": ",".join([
                "hs_call_direction",
                "hs_call_duration",
                "hs_call_recording_url",
                "hs_timestamp",
                "hs_createdate",
                "hubspot_owner_id",
                "hs_call_status",
                "hs_call_title",
            ])
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            data = response.json()

        props = data.get("properties", {})
        owner_id = props.get("hubspot_owner_id")

        return {
            "call_id": call_id,
            "hubspot_url": self.build_hubspot_url(call_id),
            "call_direction": props.get("hs_call_direction"),
            "call_duration": props.get("hs_call_duration"),
            "recording_url": props.get("hs_call_recording_url"),
            "call_timestamp": props.get("hs_timestamp") or props.get("hs_createdate"),
            "hs_timestamp": props.get("hs_timestamp"),
            "hs_createdate": props.get("hs_createdate"),
            "hubspot_owner_id": owner_id,
            "agente_telefonico": owner_id,  # Will be enriched if owner lookup is added
            "status": props.get("hs_call_status"),
        }

    async def get_owner_name(self, owner_id: str) -> str | None:
        """Optionally resolve owner_id to a display name."""
        if not owner_id:
            return None
        url = f"{HUBSPOT_API_BASE}/crm/v3/owners/{owner_id}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers=self._headers())
                response.raise_for_status()
                data = response.json()
                first = data.get("firstName", "")
                last = data.get("lastName", "")
                return f"{first} {last}".strip() or owner_id
        except Exception as e:
            logger.warning("Could not resolve owner %s: %s", owner_id, e)
            return owner_id

    async def search_calls_for_mass_evaluation(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Search calls in HubSpot based on provided filters.
        Supports pagination to retrieve all matched calls up to the job's max_calls limit.
        Apply post-filtering by local call timestamp in job's timezone.
        """
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/calls/search"
        
        # Build query filters
        hs_filters = []
        
        # 1. Date filters (hs_timestamp)
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        if date_from and date_to:
            from_ms = int(date_from.timestamp() * 1000)
            to_ms = int(date_to.timestamp() * 1000)
            hs_filters.append({
                "propertyName": "hs_timestamp",
                "operator": "BETWEEN",
                "value": str(from_ms),
                "highValue": str(to_ms)
            })
        elif date_from:
            from_ms = int(date_from.timestamp() * 1000)
            hs_filters.append({
                "propertyName": "hs_timestamp",
                "operator": "GTE",
                "value": str(from_ms)
            })
        elif date_to:
            to_ms = int(date_to.timestamp() * 1000)
            hs_filters.append({
                "propertyName": "hs_timestamp",
                "operator": "LTE",
                "value": str(to_ms)
            })
            
        # 2. Agent owner IDs filters
        agent_owner_ids = filters.get("agent_owner_ids")
        if not agent_owner_ids:
            from app.utils.hubspot_owners import OWNER_TO_NAME
            agent_owner_ids = list(OWNER_TO_NAME.keys())
            
        if agent_owner_ids:
            if len(agent_owner_ids) == 1:
                hs_filters.append({
                    "propertyName": "hubspot_owner_id",
                    "operator": "EQ",
                    "value": str(agent_owner_ids[0])
                })
            else:
                hs_filters.append({
                    "propertyName": "hubspot_owner_id",
                    "operator": "IN",
                    "values": [str(x) for x in agent_owner_ids]
                })
                
        # 3. Call duration filters
        duration_min = filters.get("duration_min_seconds")
        duration_max = filters.get("duration_max_seconds")
        if duration_min is not None and duration_max is not None:
            hs_filters.append({
                "propertyName": "hs_call_duration",
                "operator": "BETWEEN",
                "value": str(duration_min * 1000),
                "highValue": str(duration_max * 1000)
            })
        elif duration_min is not None:
            hs_filters.append({
                "propertyName": "hs_call_duration",
                "operator": "GTE",
                "value": str(duration_min * 1000)
            })
        elif duration_max is not None:
            hs_filters.append({
                "propertyName": "hs_call_duration",
                "operator": "LTE",
                "value": str(duration_max * 1000)
            })
            
        # 4. Call direction filters
        direction = filters.get("direction")
        if direction and direction.lower() in ["inbound", "outbound"]:
            hs_filters.append({
                "propertyName": "hs_call_direction",
                "operator": "EQ",
                "value": direction.upper()
            })
            
        # 5. Recording presence
        only_with_recording = filters.get("only_with_recording", True)
        if only_with_recording:
            hs_filters.append({
                "propertyName": "hs_call_recording_url",
                "operator": "HAS_PROPERTY"
            })
            
        # Compile into filterGroups
        filter_groups = []
        if hs_filters:
            filter_groups.append({"filters": hs_filters})
            
        max_calls = filters.get("max_calls", 100) or 100
        limit_to_fetch = max(1000, max_calls)
        
        # Parse time window start / end filters
        import datetime
        from datetime import time as dt_time
        import pytz
        
        time_window_start = filters.get("time_window_start")
        time_window_end = filters.get("time_window_end")
        timezone_name = filters.get("timezone") or "Europe/Madrid"
        
        def parse_time_str(t_val: Any) -> Any:
            if not t_val:
                return None
            if isinstance(t_val, dt_time):
                return t_val
            if isinstance(t_val, str):
                parts = [int(x) for x in t_val.split(":")[:3]]
                if len(parts) == 1:
                    return dt_time(parts[0], 0, 0)
                elif len(parts) == 2:
                    return dt_time(parts[0], parts[1], 0)
                elif len(parts) >= 3:
                    return dt_time(parts[0], parts[1], parts[2])
            return None

        time_window_start_parsed = parse_time_str(time_window_start)
        time_window_end_parsed = parse_time_str(time_window_end)
        
        properties = [
            "hs_call_recording_url",
            "hs_timestamp",
            "hs_createdate",
            "hs_object_id",
            "hubspot_owner_id",
            "hs_call_duration",
            "hs_call_direction",
            "hs_call_status"
        ]
        
        results = []
        after = None
        
        async with httpx.AsyncClient(timeout=60) as client:
            while len(results) < limit_to_fetch:
                payload = {
                    "filterGroups": filter_groups,
                    "properties": properties,
                    "limit": 100  # always request max page size (100) for API call efficiency
                }
                if after:
                    payload["after"] = after
                    
                response = await client.post(url, headers=self._headers(), json=payload)
                response.raise_for_status()
                data = response.json()
                
                hits = data.get("results", [])
                if not hits:
                    break
                    
                for h in hits:
                    props = h.get("properties", {})
                    
                    # Apply time window post-filtering
                    if time_window_start_parsed or time_window_end_parsed:
                        call_ts = props.get("hs_timestamp") or props.get("hs_createdate")
                        if not call_ts:
                            # Si no hay timestamp, la llamada no pasa el filtro activo
                            continue
                        try:
                            # parse call_ts to datetime
                            if isinstance(call_ts, str):
                                try:
                                    dt = datetime.datetime.fromisoformat(call_ts.replace("Z", "+00:00"))
                                except ValueError:
                                    dt = datetime.datetime.strptime(call_ts, "%Y-%m-%dT%H:%M:%S.%fZ")
                            else:
                                dt = datetime.datetime.fromtimestamp(float(call_ts) / 1000.0, tz=datetime.timezone.utc)
                            
                            # Convert to job's timezone
                            try:
                                tz = pytz.timezone(timezone_name)
                            except Exception:
                                tz = pytz.timezone("Europe/Madrid")
                            
                            dt_local = dt.astimezone(tz)
                            local_time = dt_local.time()
                            
                            # check if within window (with cross-midnight support)
                            start = time_window_start_parsed or dt_time(0, 0, 0)
                            end = time_window_end_parsed or dt_time(23, 59, 59)
                            
                            if start <= end:
                                if not (start <= local_time <= end):
                                    continue
                            else:
                                # cross midnight range (e.g. 22:00 to 02:00)
                                if not (local_time >= start or local_time <= end):
                                    continue
                        except Exception as e_time:
                            logger.warning("Filtro horario falló para llamada %s: %s", h.get("id"), e_time)
                            continue
                    
                    dur_ms = props.get("hs_call_duration")
                    dur_sec = int(float(dur_ms) / 1000.0) if dur_ms else None
                    
                    results.append({
                        "call_id": h.get("id"),
                        "hs_object_id": props.get("hs_object_id") or h.get("id"),
                        "recording_url": props.get("hs_call_recording_url"),
                        "hubspot_owner_id": props.get("hubspot_owner_id"),
                        "call_timestamp": props.get("hs_timestamp") or props.get("hs_createdate"),
                        "call_duration_seconds": dur_sec,
                        "direction": props.get("hs_call_direction"),
                        "status": props.get("hs_call_status")
                    })
                    
                paging = data.get("paging", {})
                next_page = paging.get("next", {})
                after = next_page.get("after")
                if not after:
                    break
                    
        return results[:max_calls]
