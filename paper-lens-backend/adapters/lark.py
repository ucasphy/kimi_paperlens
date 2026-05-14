"""Lark (飞书) bot adapter - placeholder for 龙虾 integration.

Future implementation will:
1. Receive PDF via Lark webhook
2. Process using 龙虾's own API
3. Send results back as Lark messages/interactive cards
"""

from typing import AsyncIterator
from .base import SessionInterface, SessionEvent, EventType


class LarkAdapter(SessionInterface):
    """Placeholder adapter for Lark bot (龙虾) integration."""

    async def start(self, prompt: str) -> str:
        raise NotImplementedError("Lark adapter not yet implemented")

    async def send_message(self, message: str) -> None:
        raise NotImplementedError("Lark adapter not yet implemented")

    async def events(self) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.ERROR, data="Lark adapter not yet implemented")

    async def stop(self) -> None:
        pass
