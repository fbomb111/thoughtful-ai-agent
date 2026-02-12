"""Pydantic models for Microsoft Foundry Agent conversations."""

from typing import List, Optional

from pydantic import BaseModel


class ConversationInfo(BaseModel):
    """Conversation creation response."""

    conversation_id: str


class Citation(BaseModel):
    """Citation from knowledge base search."""

    file_id: str = ""
    quote: str = ""
    marker: str = ""
    source_url: Optional[str] = None
    title: Optional[str] = None


class ChatResponse(BaseModel):
    """Chat message response."""

    conversation_id: str
    message: str
    citations: List[Citation] = []
