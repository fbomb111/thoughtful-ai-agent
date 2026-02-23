# Foxen Support

Welcome! I'm an AI-powered customer support agent for **Foxen**, a property insurance company serving property managers, lenders, and HOAs.
Learn more at `foxen.com`.

## How It Works

This agent uses **Retrieval-Augmented Generation (RAG)** to answer questions from a knowledge base of 77 documents scraped from the Foxen website — covering insurance tracking, compliance monitoring, and risk management.

Every question triggers a knowledge base search via **Azure AI Search** (vector embeddings with `text-embedding-3-large`). The agent cites its sources and explicitly discloses when falling back to general knowledge.

## Tech Stack

- **Azure Foundry Agent** — conversation management and tool orchestration
- **Azure AI Search** — semantic vector search over the knowledge base
- **Azure OpenAI (GPT-4.1)** — response generation
- **Chainlit** — real-time streaming chat UI
- **Azure Container Apps** — production hosting with Managed Identity auth

## Try It

Choose a starter topic below, or ask me anything about Foxen's property insurance solutions.
