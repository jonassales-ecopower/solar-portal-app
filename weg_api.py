"""
WEG/SunWEG API Client for Solar Portal Integration

Provides async client for authenticating with WEG API and fetching
plant data and aggregated totals.
"""

import logging
from typing import Optional, Dict, Any
import aiohttp

_LOGGER = logging.getLogger(__name__)

# Constants
API_BASE_URL = "https://api.sunweg.net/v2"
PORTAL_BASE_URL = "https://sun.weg.net"
HEADER_USER_AGENT = "Mozilla/5.0"

AUTH_ERROR_STATUSES = {401, 403}


class WEGAPIError(Exception):
    """Raised when an unexpected response is returned from the API."""
    pass


class WEGAuthError(WEGAPIError):
    """Raised when authentication fails or the token has expired."""
    pass


class WEGClient:
    """Asynchronous client for interacting with WEG REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        """Initialize WEG client.

        Args:
            session: aiohttp ClientSession for making requests
            email: User email for authentication
            password: User password for authentication
            token: Pre-authenticated token (alternative to email/password)
        """
        self._session = session
        self._email = email
        self._password = password
        self._token: Optional[str] = token

    @property
    def token(self) -> Optional[str]:
        """Return the current API token."""
        return self._token

    @property
    def is_authenticated(self) -> bool:
        """Check if client has valid authentication."""
        return self._token is not None

    async def async_login(self) -> bool:
        """Authenticate with the API and store the returned token.

        Returns:
            True if authentication successful, False otherwise

        Raises:
            WEGAuthError: If credentials are missing or API returns error
        """
        if not self._email or not self._password:
            raise WEGAuthError("Email and password are required to request a new token")

        url = f"{API_BASE_URL}/login/autenticacao"
        payload = {
            "usuario": self._email,
            "senha": self._password,
            "rememberMe": True,
            "aceito": False,
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": HEADER_USER_AGENT,
            "Origin": PORTAL_BASE_URL,
            "Referer": f"{PORTAL_BASE_URL}/sign-in",
        }

        try:
            async with self._session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    _LOGGER.error(f"WEG Authentication failed: HTTP {resp.status} - {error_text}")
                    raise WEGAuthError(f"Authentication failed: HTTP {resp.status}")

                data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as err:
            raise WEGAPIError(f"Error communicating with WEG API: {err}") from err

        if not data.get("success"):
            _LOGGER.error(f"WEG Authentication response unsuccessful: {data}")
            raise WEGAuthError(f"Authentication failed: {data.get('error', 'Unknown error')}")

        if "token" not in data:
            _LOGGER.error("WEG Authentication response did not include a token")
            raise WEGAuthError("Invalid credentials or unexpected response")

        self._token = str(data["token"])
        _LOGGER.debug(f"WEG Login successful, token set")
        return True

    def _auth_headers(self) -> Dict[str, str]:
        """Construct headers for authenticated API calls."""
        if not self._token:
            raise WEGAuthError("Attempted to call API without a token")
        return {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": HEADER_USER_AGENT,
            "Origin": PORTAL_BASE_URL,
            "Referer": f"{PORTAL_BASE_URL}/",
            "X-Auth-Token-Update": self._token,
        }

    async def _get_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Internal helper to perform a GET request and return parsed JSON.

        Args:
            endpoint: Path portion of the API endpoint (starting with '/').
            params: Optional dictionary of query parameters.

        Returns:
            The parsed JSON response.

        Raises:
            WEGAuthError: If authentication is missing or token expired.
            WEGAPIError: For connection problems or non-JSON responses.
        """
        url = f"{API_BASE_URL}{endpoint}"
        headers = self._auth_headers()

        try:
            async with self._session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                # If unauthorized, attempt to refresh token if credentials available
                if resp.status in AUTH_ERROR_STATUSES:
                    if self._email and self._password:
                        _LOGGER.warning("WEG Token expired, attempting reauthentication")
                        try:
                            await self.async_login()
                            headers = self._auth_headers()
                            async with self._session.get(
                                url,
                                headers=headers,
                                params=params,
                                timeout=aiohttp.ClientTimeout(total=30)
                            ) as retry_resp:
                                if retry_resp.status in AUTH_ERROR_STATUSES:
                                    raise WEGAuthError(f"Authentication still failing after refresh")
                                if retry_resp.status >= 400:
                                    text = await retry_resp.text()
                                    raise WEGAPIError(f"HTTP {retry_resp.status}: {text[:200]}")
                                return await retry_resp.json()
                        except WEGAuthError:
                            raise
                    else:
                        raise WEGAuthError("Token expired and no credentials available to refresh")

                if resp.status >= 400:
                    text = await resp.text()
                    _LOGGER.warning(f"WEG API HTTP {resp.status} for {endpoint}: {text[:200]}")
                    raise WEGAPIError(f"HTTP {resp.status} when fetching {endpoint}")

                return await resp.json()

        except aiohttp.ClientError as err:
            raise WEGAPIError(f"Error fetching {endpoint}: {err}") from err
        except WEGAPIError:
            raise
        except Exception as ex:
            raise WEGAPIError(f"Unexpected error fetching {endpoint}: {ex}") from ex

    async def async_validate_token(self) -> bool:
        """Validate the current token with a lightweight authenticated endpoint.

        Returns:
            True if token is valid, False otherwise
        """
        try:
            data = await self._get_json("/get/version/activate")
            return data.get("success", True)
        except WEGAuthError:
            return False

    async def async_get_all_plants(self) -> list:
        """Retrieve all accessible plants/usinas.

        Returns:
            List of plant dictionaries containing id, nome, and metrics.
        """
        params = {
            "usina": "",
            "id": "",
            "situacao": "null",
            "limite": 12,
            "quantidade": 0,
            "paginaAtual": 1,
            "agrupado": "false",
            "gettotalizadores": "false",
        }

        try:
            data = await self._get_json("/getdadosresumo", params=params)
            if data.get("success"):
                return data.get("usinas", [])
            return []
        except WEGAPIError as e:
            _LOGGER.error(f"Failed to fetch WEG plants: {e}")
            return []

    async def async_get_plant_summary(self, plant_id: str) -> Dict[str, Any]:
        """Fetch summary data for a specific plant.

        Args:
            plant_id: Identifier of the plant (usina) to fetch.

        Returns:
            Dictionary containing the plant summary information.
        """
        params = {
            "usina": "",
            "id": str(plant_id),
            "situacao": "null",
            "limite": 12,
            "quantidade": 0,
            "paginaAtual": 1,
            "agrupado": "false",
            "gettotalizadores": "false",
        }

        try:
            data = await self._get_json("/getdadosresumo", params=params)
            if data.get("success"):
                usinas = data.get("usinas", [])
                # Find matching plant or return first
                for u in usinas:
                    if str(u.get("id")) == str(plant_id):
                        return u
                return usinas[0] if usinas else {}
            return {}
        except WEGAPIError as e:
            _LOGGER.error(f"Failed to fetch WEG plant summary for {plant_id}: {e}")
            return {}

    async def async_get_totalizadores(self) -> Dict[str, Any]:
        """Fetch aggregated totals across all plants.

        Returns:
            Dictionary containing aggregated metrics like total energy,
            power, carbon reduction, and financial savings.
        """
        try:
            data = await self._get_json("/gettotalizadores")
            if data.get("success"):
                return data.get("dados", {})
            return {}
        except WEGAPIError as e:
            _LOGGER.error(f"Failed to fetch WEG totalizadores: {e}")
            return {}


def parse_numeric(value: Any, multipliers: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Extract a float from a value potentially containing units.

    Handles formats like "5.23 kWh", "R$ 100.50", "15 arvore(s)", etc.

    Args:
        value: The value to parse (string with number+unit or numeric)
        multipliers: Dict mapping unit suffixes to multipliers
                    (e.g. {"MWh": 1000.0, "kWh": 1.0})

    Returns:
        Parsed numeric value or None if unparseable

    Examples:
        >>> parse_numeric("5.23 kWh")
        5.23
        >>> parse_numeric("1.5 MWh", {"MWh": 1000.0, "kWh": 1.0})
        1500.0
        >>> parse_numeric("R$ 100.50")
        100.5
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        # Strip currency symbols
        cleaned = value.strip()
        for prefix in ("R$", "$", "€", "£"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()

        # Split into parts
        parts = cleaned.split()
        if not parts:
            return None

        try:
            # Handle Brazilian decimal format (comma instead of dot)
            number_str = parts[0].replace(".", "").replace(",", ".")
            number = float(number_str)
        except ValueError:
            return None

        # Apply multiplier if unit is present
        if len(parts) > 1 and multipliers:
            unit = parts[1]
            multiplier = multipliers.get(unit)
            if multiplier is not None:
                return number * multiplier

        return number

    return None
