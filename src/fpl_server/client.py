import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import httpx

from .models import Player

if TYPE_CHECKING:
    from .state import SessionStore

logger = logging.getLogger("fpl_client")

_RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ServerClock:
    """What FPL's own response headers said about time and data freshness."""

    server_time: datetime | None  # parsed HTTP Date header (UTC)
    received_at: datetime  # our clock when it arrived (UTC)
    age_seconds: int  # CDN cache age of the response (0 if no Age header)


def _server_clock(response) -> ServerClock:
    server_time = None
    try:
        raw = response.headers.get("date")
        if raw:
            parsed = parsedate_to_datetime(raw)
            server_time = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        server_time = None
    try:
        age = int(response.headers.get("age", "0"))
    except (TypeError, ValueError):
        age = 10**6  # unparseable => treat as stale
    return ServerClock(server_time, datetime.now(timezone.utc), max(0, age))


class FPLAPIError(RuntimeError):
    """FPL returned an error. `body` is a short, token-free excerpt of the response."""

    def __init__(self, status: int, body: str):
        super().__init__(f"FPL API error {status}: {body}")
        self.status = status
        self.body = body


class FPLAuthExpired(FPLAPIError):
    """The stored FPL session was rejected - the user must log in again."""

class FPLClient:
    BASE_URL = "https://fantasy.premierleague.com/api/"
    
    def __init__(self, store: Optional['SessionStore'] = None):
        self.session: httpx.AsyncClient | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self.api_token = None
        self.team_id: Optional[int] = None
        self.user_info: Optional[Dict[str, Any]] = None  # Store user info from /me
        self._store = store
        self.bootstrap_clock: Optional[ServerClock] = None  # freshness of the last bootstrap fetch

    def set_api_token(self, token: str):
        if not token.startswith("Bearer "):
            token = f"Bearer {token}"
        self.api_token = token

    def _get_session(self) -> httpx.AsyncClient:
        current_loop = asyncio.get_running_loop()
        if self.session is not None and self._session_loop is not current_loop:
            raise RuntimeError(
                "FPLClient crossed an asyncio event-loop boundary without being closed."
            )
        if self.session is None:
            self.session = httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30.0,
            )
            self._session_loop = current_loop
        return self.session
        
    async def _request(self, method: str, endpoint: str, data: dict = None, params: dict = None) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        session = self._get_session()
        headers = {}
        if self.api_token:
            headers['x-api-authorization'] = self.api_token
            headers['Authorization'] = self.api_token

        # Reads are retried with backoff. Writes are NEVER retried: a retried POST could apply
        # a team change twice.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            if method == "GET":
                response = await session.get(url, headers=headers, params=params)
            else:
                response = await session.post(url, json=data, headers=headers)
            if response.status_code in _RETRY_STATUSES and attempt < attempts - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            break

        self._last_clock = _server_clock(response)
        if response.status_code in (401, 403) and self.api_token:
            raise FPLAuthExpired(response.status_code, "session rejected; log in again")
        if response.status_code >= 400:
            raise FPLAPIError(response.status_code, response.text[:300])
        if method != "GET":
            # The change was accepted. An empty / non-JSON body must not be reported as a failure.
            try:
                return response.json()
            except ValueError:
                return {"status_code": response.status_code, "note": "accepted; non-JSON body"}
        return response.json()

    async def get_bootstrap_data(self) -> Dict[str, Any]:
        """Fetch fresh bootstrap data from API"""
        data = await self._request("GET", "bootstrap-static/")
        self.bootstrap_clock = self._last_clock
        return data
    
    async def get_bootstrap_static(self) -> Dict[str, Any]:
        """Alias used by SessionStore.ensure_bootstrap_data (upstream called a missing name)."""
        return await self.get_bootstrap_data()

    async def get_fixtures(self) -> List[Dict[str, Any]]:
        """Fetch fixtures data from API"""
        return await self._request("GET", "fixtures/")
    
    async def get_element_summary(self, player_id: int) -> Dict[str, Any]:
        """
        Fetch detailed player summary including fixtures, history, and past seasons.
        
        Args:
            player_id: The FPL player ID (element ID)
            
        Returns:
            Dictionary containing fixtures, history, and history_past
        """
        return await self._request("GET", f"element-summary/{player_id}/")
    
    async def get_manager_entry(self, team_id: int) -> Dict[str, Any]:
        """
        Fetch FPL manager/team entry information.
        
        Args:
            team_id: The FPL manager's team ID (entry ID)
            
        Returns:
            Dictionary containing manager details, leagues, and team information
        """
        return await self._request("GET", f"entry/{team_id}/")
    
    async def get_league_standings(
        self,
        league_id: int,
        page_standings: int = 1,
        page_new_entries: int = 1,
        phase: int = 1
    ) -> Dict[str, Any]:
        """
        Fetch league standings for a classic league.
        
        Args:
            league_id: The league ID
            page_standings: Page number for standings (default: 1)
            page_new_entries: Page number for new entries (default: 1)
            phase: Phase/season number (default: 1)
            
        Returns:
            Dictionary containing league info and standings with entries
        """
        params = {
            'page_standings': page_standings,
            'page_new_entries': page_new_entries,
            'phase': phase
        }
        return await self._request(
            "GET",
            f"leagues-classic/{league_id}/standings/",
            params=params
        )
    
    async def get_manager_gameweek_picks(self, team_id: int, gameweek: int) -> Dict[str, Any]:
        """
        Fetch a manager's team picks for a specific gameweek.
        
        Args:
            team_id: The FPL manager's team ID (entry ID)
            gameweek: The gameweek number (event ID)
            
        Returns:
            Dictionary containing picks, automatic subs, and entry history for the gameweek
        """
        return await self._request("GET", f"entry/{team_id}/event/{gameweek}/picks/")
    
    async def get_me(self) -> Dict[str, Any]:
        """
        Fetch current user's information including their entry ID.
        This is called after authentication to get the user's team ID.
        
        Returns:
            Dictionary containing player info with entry ID
        """
        return await self._request("GET", "me/")

    async def get_players(self) -> List[Player]:
        """Get all players using in-memory bootstrap data"""
        # Use in-memory data if available
        if self._store and self._store.bootstrap_data:
            data = self._store.bootstrap_data
            teams = {t.id: t.name for t in data.teams}
            types = {t.id: t.singular_name_short for t in data.element_types}
            
            players = []
            for element in data.elements:
                # Convert ElementData to Player
                player = Player(
                    id=element.id,
                    web_name=element.web_name,
                    first_name=element.first_name,
                    second_name=element.second_name,
                    team=element.team,
                    element_type=element.element_type,
                    now_cost=element.now_cost,
                    form=element.form,
                    points_per_game=element.points_per_game,
                    news=element.news
                )
                player.team_name = teams.get(player.team, "Unknown")
                player.position = types.get(player.element_type, "Unk")
                players.append(player)
            return players
        
        # Fallback to API if in-memory data not available
        logger.warning("Bootstrap data not loaded, fetching from API")
        data = await self.get_bootstrap_data()
        teams = {t['id']: t['name'] for t in data['teams']}
        types = {t['id']: t['singular_name_short'] for t in data['element_types']}
        
        players = []
        for p in data['elements']:
            player = Player(**p)
            player.team_name = teams.get(player.team, "Unknown")
            player.position = types.get(player.element_type, "Unk")
            players.append(player)
        return players

    async def get_top_players_by_position(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get top players by position based on points per game.
        Returns: {
            'GKP': [top 5 goalkeepers],
            'DEF': [top 20 defenders],
            'MID': [top 20 midfielders],
            'FWD': [top 20 forwards]
        }
        """
        if not self._store or not self._store.bootstrap_data:
            logger.warning("Bootstrap data not available for top players")
            return {'GKP': [], 'DEF': [], 'MID': [], 'FWD': []}
        
        data = self._store.bootstrap_data
        teams = {t.id: t.name for t in data.teams}
        types = {t.id: t.singular_name_short for t in data.element_types}
        
        # Group players by position
        players_by_position = {'GKP': [], 'DEF': [], 'MID': [], 'FWD': []}
        
        for element in data.elements:
            # Only include available players
            #if element.status != 'a':
            #    continue
                
            position = types.get(element.element_type, 'UNK')
            if position not in players_by_position:
                continue
            
            # Convert to float for sorting, handle 0.0 as string
            try:
                ppg = float(element.points_per_game) if element.points_per_game else 0.0
            except ValueError:
                ppg = 0.0
            
            player_data = {
                'id': element.id,
                'name': element.web_name,
                'full_name': f"{element.first_name} {element.second_name}",
                'team': teams.get(element.team, 'Unknown'),
                'price': element.now_cost / 10,
                'points_per_game': ppg,
                'total_points': getattr(element, 'total_points', 0),
                'form': element.form,
                'status': element.status,
                'news': element.news if element.news else ''
            }
            players_by_position[position].append(player_data)
        
        # Sort by points_per_game and take top N
        result = {
            'GKP': sorted(players_by_position['GKP'], key=lambda x: x['points_per_game'], reverse=True)[:5],
            'DEF': sorted(players_by_position['DEF'], key=lambda x: x['points_per_game'], reverse=True)[:20],
            'MID': sorted(players_by_position['MID'], key=lambda x: x['points_per_game'], reverse=True)[:20],
            'FWD': sorted(players_by_position['FWD'], key=lambda x: x['points_per_game'], reverse=True)[:20]
        }
        
        return result

    async def get_my_team(self, team_id: int) -> Dict[str, Any]:
        return await self._request("GET", f"my-team/{team_id}/")

    async def get_current_gameweek(self) -> int:
        data = await self.get_bootstrap_data()
        for event in data['events']:
            if event['is_next']:
                return event['id']
        return 38

    async def post_transfers(self, payload: dict) -> Dict[str, Any]:
        """POST a transfer payload. Only ever called by the approval-gated executor."""
        return await self._request("POST", "transfers/", payload)

    async def post_my_team(self, entry_id: int, payload: dict) -> Dict[str, Any]:
        """POST lineup / captaincy / chip. Only ever called by the approval-gated executor."""
        return await self._request("POST", f"my-team/{int(entry_id)}/", payload)
        
    async def close(self):
        if self.session is not None:
            await self.session.aclose()
            self.session = None
            self._session_loop = None
