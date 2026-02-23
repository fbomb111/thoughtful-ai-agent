"""
Agent Service - high-level conversation management for the Foxen agent.

Wraps FoundryClient with conversation lifecycle and streaming helpers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from .client import FoundryClient
from .models import ConversationInfo

logger = logging.getLogger(__name__)


class AgentService:
    """High-level service for Foundry Agent conversations."""

    def __init__(self, client: FoundryClient):
        self.client = client

    async def create_conversation(self) -> ConversationInfo:
        """Create a new conversation with the agent."""
        conversation_id = await self.client.create_conversation()
        return ConversationInfo(conversation_id=conversation_id)

    async def send_message_stream(
        self,
        conversation_id: str,
        message: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Send a message and stream the response.

        Yields event dicts: text_delta, text_done, error
        """
        try:
            async for event in self.client.send_message(conversation_id, message):
                yield event
        except Exception as e:
            logger.exception("Streaming error")
            yield {"type": "error", "error": str(e)}

    async def close(self) -> None:
        """Release resources."""
        await self.client.close()
