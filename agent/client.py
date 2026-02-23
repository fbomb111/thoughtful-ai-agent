"""
Async Microsoft Foundry Client for the Foxen support agent.

Uses Microsoft Entra ID authentication for the Foundry API (2025-11-15-preview).
Handles conversation management, streaming SSE responses, and citation parsing.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from .models import Citation

logger = logging.getLogger(__name__)

FOUNDRY_AUTH_SCOPE = "https://ai.azure.com/.default"
FOUNDRY_API_VERSION = "2025-11-15-preview"


def get_credential() -> DefaultAzureCredential | ManagedIdentityCredential:
    """Get Azure credential. Uses Managed Identity if configured, otherwise DefaultAzureCredential."""
    client_id = os.getenv("MANAGED_IDENTITY_CLIENT_ID")
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


class FoundryClient:
    """
    Async client for Microsoft Foundry Agent API.

    Handles authentication, conversation management, and streaming responses
    via Server-Sent Events (SSE).
    """

    def __init__(
        self,
        endpoint: str,
        agent_name: str,
        agent_version: str = "latest",
        credential: Any | None = None,
    ):
        self.endpoint = endpoint
        self.agent_name = agent_name
        self.agent_version = agent_version
        self._credential = credential or get_credential()
        self._http_client: httpx.AsyncClient | None = None

    @classmethod
    def from_env(cls) -> FoundryClient:
        """Create client from environment variables.

        Reads: AZURE_AI_PROJECT_ENDPOINT, THOUGHTFUL_AGENT_NAME, THOUGHTFUL_AGENT_VERSION
        """
        from dotenv import load_dotenv

        load_dotenv()

        endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        agent_name = os.getenv("THOUGHTFUL_AGENT_NAME")
        agent_version = os.getenv("THOUGHTFUL_AGENT_VERSION", "latest")

        if not endpoint:
            raise ValueError("AZURE_AI_PROJECT_ENDPOINT environment variable is required")
        if not agent_name:
            raise ValueError("THOUGHTFUL_AGENT_NAME environment variable is required")

        return cls(endpoint=endpoint, agent_name=agent_name, agent_version=agent_version)

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get HTTP client with connection pooling (lazy init)."""
        if self._http_client is None:
            limits = httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            )
            self._http_client = httpx.AsyncClient(timeout=120.0, limits=limits)
        return self._http_client

    async def _get_headers(self) -> dict[str, str]:
        """Get headers with fresh auth token."""
        try:
            token = await self._credential.get_token(FOUNDRY_AUTH_SCOPE)
        except TypeError:
            token = self._credential.get_token(FOUNDRY_AUTH_SCOPE)
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str) -> str:
        """Build full API URL."""
        return f"{self.endpoint}/openai/{path}?api-version={FOUNDRY_API_VERSION}"

    # =========================================================================
    # Conversation Management
    # =========================================================================

    async def create_conversation(self) -> str:
        """Create a new conversation. Returns conversation ID."""
        client = await self._get_http_client()
        headers = await self._get_headers()
        url = self._api_url("conversations")

        response = await client.post(url, headers=headers, json={})
        if response.status_code >= 400:
            logger.error("Conversation creation failed %s: %s", response.status_code, response.text)
        response.raise_for_status()

        data = response.json()
        logger.info("Created conversation: %s", data.get("id"))
        return data["id"]

    # =========================================================================
    # Message Streaming
    # =========================================================================

    async def send_message(
        self,
        conversation_id: str,
        message: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Send a message and stream the response as event dicts.

        Yields event dicts with types: text_delta, text_done, error
        """
        client = await self._get_http_client()
        headers = await self._get_headers()
        headers["Accept"] = "text/event-stream"

        url = self._api_url("responses")
        body = {
            "agent": {
                "type": "agent_reference",
                "name": self.agent_name,
                "version": self.agent_version,
            },
            "conversation": conversation_id,
            "input": [{"type": "message", "role": "user", "content": message}],
            "stream": True,
        }

        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                error_body = await response.aread()
                logger.error("API Error %s: %s", response.status_code, error_body.decode())
                response.raise_for_status()

            accumulated_text = ""
            final_response_data = None

            async for line in response.aiter_lines():
                if not line or line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue

                try:
                    event_data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse SSE data: %s", data_str[:100])
                    continue

                event_type = event_data.get("type", "")

                if event_type == "response.output_text.delta":
                    text = event_data.get("delta", "")
                    if text:
                        accumulated_text += text
                        yield {"type": "text_delta", "content": text}

                elif event_type == "response.completed":
                    final_response_data = event_data.get("response", {})

                elif event_type == "response.failed":
                    error = event_data.get("error", {})
                    error_msg = (
                        error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    )
                    yield {"type": "error", "error": error_msg}

        # Process final response for citations
        if final_response_data:
            result = _extract_response_content(final_response_data)
            yield {
                "type": "text_done",
                "content": result["content"],
                "citations": [c.model_dump() for c in result["citations"]],
            }
        else:
            yield {
                "type": "text_done",
                "content": accumulated_text,
                "citations": [],
            }

    # =========================================================================
    # Cleanup
    # =========================================================================

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


# =============================================================================
# Response Content Extraction
# =============================================================================


def _extract_response_content(response_data: dict[str, Any]) -> dict[str, Any]:
    """Extract message content and citations from a completed response."""
    content = ""
    citations: list[Citation] = []

    for item in response_data.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for content_part in item.get("content", []):
                if content_part.get("type") == "output_text":
                    content += content_part.get("text", "")

                    for ann in content_part.get("annotations", []):
                        citation = _parse_annotation(ann)
                        if citation:
                            citations.append(citation)

    return {"content": content, "citations": citations}


def _parse_annotation(annotation: dict[str, Any]) -> Citation | None:
    """Parse a single annotation into a Citation."""
    ann_type = annotation.get("type", "")

    if ann_type == "url_citation":
        return Citation(
            source_url=annotation.get("url", ""),
            title=annotation.get("title", ""),
            marker=annotation.get("text", ""),
        )
    elif ann_type == "file_citation":
        return Citation(
            file_id=annotation.get("file_id", ""),
            quote=annotation.get("quote", ""),
            marker=annotation.get("text", ""),
        )
    elif ann_type == "mcp_citation":
        return Citation(
            source_url=annotation.get("url", ""),
            title=annotation.get("tool_name", "MCP Source"),
            marker=annotation.get("text", ""),
        )
    return None
