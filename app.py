"""Foxen Customer Support Agent - Chainlit UI."""

import logging

import chainlit as cl
from dotenv import load_dotenv

from agent import AgentService, FoundryClient

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Shared client (connection-pooled, reused across sessions)
client = FoundryClient.from_env()
service = AgentService(client)


@cl.set_starters
async def set_starters():
    """Show starter suggestions on the landing page."""
    return [
        cl.Starter(
            label="Insurance compliance tracking",
            message="How does Foxen track insurance compliance across a property portfolio?",
        ),
        cl.Starter(
            label="Compliance documents",
            message="What insurance documents does Foxen monitor for compliance?",
        ),
        cl.Starter(
            label="HOA risk management",
            message="How does Foxen help HOAs manage risk and compliance?",
        ),
        cl.Starter(
            label="Why Foxen?",
            message="What are the key benefits of using Foxen for property insurance compliance monitoring?",
        ),
    ]


@cl.on_chat_start
async def on_chat_start():
    """Create a new conversation when a user connects."""
    try:
        conv = await service.create_conversation()
        cl.user_session.set("conversation_id", conv.conversation_id)
    except Exception as e:
        logger.exception("Failed to create conversation")
        await cl.Message(content=f"Failed to connect to the AI agent: {e}").send()


@cl.on_message
async def on_message(message: cl.Message):
    """Handle user messages with streaming responses."""
    conversation_id = cl.user_session.get("conversation_id")

    if not conversation_id:
        await cl.Message(
            content="No active conversation. Please refresh the page to start a new session."
        ).send()
        return

    # Guard against empty or whitespace-only input
    user_input = message.content.strip()
    if not user_input:
        await cl.Message(
            content="It looks like your message was empty. Please type a question and I'll be happy to help!"
        ).send()
        return

    response_msg = cl.Message(content="")

    try:
        async for event in service.send_message_stream(conversation_id, user_input):
            event_type = event.get("type")

            if event_type == "text_delta":
                await response_msg.stream_token(event.get("content", ""))

            elif event_type == "text_done":
                # Use the final content (may differ from streamed deltas due to citation cleanup)
                response_msg.content = event.get("content", response_msg.content)

            elif event_type == "error":
                error_text = event.get("error", "Unknown error")
                logger.error("Agent error: %s", error_text)
                if not response_msg.content:
                    response_msg.content = (
                        f"I encountered an error processing your request: {error_text}"
                    )

        # Guard against empty responses (e.g., agent returned no content)
        if not response_msg.content.strip():
            response_msg.content = (
                "I wasn't able to generate a response for that. "
                "Could you try rephrasing your question?"
            )

        await response_msg.send()

    except Exception as e:
        logger.exception("Failed to process message")
        await cl.Message(
            content=f"Sorry, something went wrong: {e}"
        ).send()
