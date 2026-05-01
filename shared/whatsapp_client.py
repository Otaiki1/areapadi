from __future__ import annotations
import os
import httpx
from shared.logger import get_logger

logger = get_logger("whatsapp_client")

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"


class WhatsAppClient:
    """Unified WhatsApp Cloud API client used by all services."""

    def __init__(self) -> None:
        self.phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self.access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        self.base_url = f"{GRAPH_API_BASE}/{self.phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, to: str, body: str) -> bool:
        """Send a plain text message."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        return await self._send(payload, to)

    async def send_template(self, to: str, template_name: str, params: list[str]) -> bool:
        """Send a pre-approved template message (required after 24h session expiry)."""
        components = []
        if params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            })
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "en"},
                "components": components,
            },
        }
        return await self._send(payload, to)

    async def send_interactive_buttons(self, to: str, body: str, buttons: list[dict]) -> bool:
        """Send message with up to 3 quick-reply buttons."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                        for b in buttons[:3]
                    ]
                },
            },
        }
        return await self._send(payload, to)

    async def send_interactive_list(self, to: str, body: str, sections: list[dict]) -> bool:
        """Send a list message with sections and rows."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": "See options",
                    "sections": sections,
                },
            },
        }
        return await self._send(payload, to)

    async def send_location_request(self, to: str, body: str) -> bool:
        """Request the user's location using WhatsApp location message type."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "location_request_message",
                "body": {"text": body},
                "action": {"name": "send_location"},
            },
        }
        return await self._send(payload, to)

    async def _send(self, payload: dict, to: str) -> bool:
        """Execute the API call. Logs on failure but never raises."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.base_url, json=payload, headers=self._headers())
                if resp.status_code not in (200, 201):
                    logger.error(
                        "whatsapp_send_failed",
                        status=resp.status_code,
                        body=resp.text[:200],
                        to=to[-4:],
                    )
                    return False
                return True
        except Exception as exc:
            logger.error("whatsapp_send_exception", error=str(exc), to=to[-4:])
            return False


_client: WhatsAppClient | None = None


def get_whatsapp_client() -> WhatsAppClient:
    global _client
    if _client is None:
        _client = WhatsAppClient()
    return _client
