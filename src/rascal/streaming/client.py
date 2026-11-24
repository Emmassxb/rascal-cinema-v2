"""Cliente HTTP del microservicio de streaming, usado por la UI (Streamlit).

La UI nunca importa libtorrent: delega en el microservicio y sólo firma tokens de
reproducción con el secreto compartido.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from rascal.auth.tokens import playback_token
from rascal.config import Settings


class StreamerUnavailable(RuntimeError):
    pass


class StreamerClient:
    def __init__(self, settings: Settings, *, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.streamer_internal_url,
            timeout=timeout,
            headers={"X-Service-Key": settings.service_key.get_secret_value()},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise StreamerUnavailable(f"Servicio de streaming no disponible: {exc}") from exc
        if response.status_code >= 400:
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else response.text
            raise StreamerUnavailable(f"Streaming respondió {response.status_code}: {detail}")
        return response.json()

    def health(self) -> dict[str, Any] | None:
        try:
            return self._request("GET", "/health")
        except StreamerUnavailable:
            return None

    def start(self, source_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/sources/{source_id}/start")

    def stop(self, source_id: int, *, delete_files: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/api/sources/{source_id}/stop", params={"delete_files": delete_files})

    def status(self, source_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/sources/{source_id}/status")

    def probe(self, source_id: int, *, timeout: float = 300.0) -> dict[str, Any]:
        return self._request("POST", f"/api/sources/{source_id}/probe", timeout=timeout)

    def player_url(self, *, user_id: int, source_id: int) -> str:
        """URL del reproductor embebible con token firmado de corta duración."""
        token = playback_token(
            self.settings.secret_key.get_secret_value(),
            user_id=user_id,
            source_id=source_id,
            ttl_s=self.settings.playback_token_ttl_s,
        )
        return f"{self.settings.streamer_public_url}/player/{source_id}?token={quote(token)}"
