# Smarter Technologies Customer Support Agent

AI-powered customer support chatbot for Smarter Technologies, built with **Azure Foundry Agents**, **RAG** (Retrieval-Augmented Generation), and **Chainlit**.

The agent answers questions about Smarter Technologies' healthcare automation products using a knowledge base of 77 documents indexed in Azure AI Search. It streams responses in real-time and cites sources from the knowledge base. For questions outside the KB, it falls back to general knowledge with an explicit disclaimer.

## Live Demo

**Try it now**: [ca-thoughtful-ai.kinddesert-00223816.eastus.azurecontainerapps.io](https://ca-thoughtful-ai.kinddesert-00223816.eastus.azurecontainerapps.io/)

![Chainlit UI](https://img.shields.io/badge/UI-Chainlit-blue) ![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green) ![Azure Foundry](https://img.shields.io/badge/Azure-Foundry_Agents-0078D4)

**Sample questions** (click a starter chip or type your own):
- "What does EVA do?"
- "How does CAM help with claims processing?"
- "Tell me about PHIL"
- "What is healthcare revenue cycle management?" *(general knowledge fallback)*

## Architecture

```
User ─── Chainlit UI ─── AgentService ─── FoundryClient ─── Azure Foundry Agent
                                                                    │
                                                              MCPTool (RAG)
                                                                    │
                                                        AI Search Knowledge Base
                                                                    │
                                                        Azure Blob Storage (77 docs)
```

### Azure Stack

| Service | Resource | Role |
|---------|----------|------|
| **Azure AI Foundry** | `doc-chat` project | Hosts the agent, handles conversation threads and response streaming |
| **Azure OpenAI** | `gpt-4.1` deployment | LLM powering the agent's responses |
| **Azure AI Search** | `doc-chat-ai-search` | Vector index over the knowledge base (`text-embedding-3-large` embeddings) |
| **Azure Blob Storage** | `docchatstorageeastus` | Stores the 77 markdown documents that make up the knowledge base |
| **Azure Container Apps** | `ca-thoughtful-ai` | Hosts the Chainlit app (Docker container) with Managed Identity auth |
| **Managed Identity** | `doc-chat-identity` | Passwordless authentication from the container to Foundry and AI Search |

### Application Layers

| Layer | File | Responsibility |
|-------|------|---------------|
| UI | `app.py` | Chainlit handlers: session init, streaming display, error states |
| Service | `agent/service.py` | Conversation lifecycle, error-to-event conversion |
| Client | `agent/client.py` | Foundry API auth, SSE streaming, citation parsing |
| Models | `agent/models.py` | Pydantic models: `ConversationInfo`, `Citation`, `ChatResponse` |

**Key design decisions:**
- **Async throughout** — `httpx.AsyncClient` with connection pooling for non-blocking I/O
- **SSE streaming** — Tokens stream to the UI as they arrive via `response.output_text.delta` events
- **Citation extraction** — Parses `url_citation`, `file_citation`, and `mcp_citation` annotations from completed responses
- **Credential flexibility** — Uses `ManagedIdentityCredential` when available, falls back to `DefaultAzureCredential` for local dev

## Approach

Rather than hardcoding Q&A pairs with simple string matching, I built a production-style RAG pipeline:

1. **Scraped the Smarter Technologies website** (`scripts/scrape_website.py`) — used the sitemap to discover all 76 pages (blog posts, product pages, solutions, about page), fetched the HTML, extracted article content with BeautifulSoup, and converted to clean markdown using `markdownify`. This created a comprehensive knowledge base covering EVA, CAM, PHIL, SmarterPrebill, SmarterReceivables, and all published blog content.

2. **Built a knowledge base** — uploaded 77 markdown documents to Azure Blob Storage, then created an AI Search knowledge source that auto-provisions a vector index. Documents are chunked and embedded with `text-embedding-3-large` for semantic retrieval, so the agent can find relevant content even when the user's question doesn't match exact keywords.

3. **Connected via MCP** — the Foundry agent accesses the knowledge base through a RemoteTool connection using the Model Context Protocol (MCP). Authentication uses `ProjectManagedIdentity` so the agent authenticates to AI Search without any API keys at runtime — just Azure RBAC.

4. **Tuned the agent prompt** (`agent_config/instructions.md`) — mandatory KB tool usage on every query, no fabrication, explicit fallback disclosure when KB has no answer, concise response style appropriate for healthcare executives.

5. **Built the chat UI** — Chainlit app with branded Smarter Technologies logo, starter chips for common questions, real-time streaming responses, and comprehensive error handling for empty inputs, failed connections, and agent errors.

6. **Deployed to Azure** — Dockerized the app and deployed to Azure Container Apps with Managed Identity authentication, GitHub Actions CI/CD for automated deployments on push.

The entire provisioning pipeline is automated in a single script (`scripts/setup_agent.py`) — one command creates the blob container, uploads docs, provisions the search infrastructure, creates the agent, and updates the local config.

## Project Structure

```
├── app.py                      # Chainlit entry point (~100 lines)
├── Dockerfile                  # Container image (Python 3.12 + Chainlit)
├── agent/
│   ├── client.py               # Foundry API client (async SSE streaming)
│   ├── service.py              # Conversation management wrapper
│   └── models.py               # Pydantic data models
├── agent_config/
│   └── instructions.md         # Agent system prompt
├── data/
│   └── thoughtful-ai-kb.md     # Seed knowledge base content
├── scripts/
│   ├── setup_agent.py          # One-command Azure provisioning (7 steps)
│   ├── teardown_agent.py       # Resource cleanup
│   └── scrape_website.py       # Website scraper (used to build KB)
├── .chainlit/config.toml       # UI branding and settings
├── .github/workflows/deploy.yaml  # CI/CD pipeline
├── chainlit.md                 # Welcome screen content
├── pyproject.toml              # Dependencies (Poetry)
└── .env.example                # Environment variable template
```

## Setup

### Prerequisites
- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation)
- Azure account with: AI Foundry project, AI Search service, Blob Storage, OpenAI deployment (gpt-4.1 + text-embedding-3-large)

### Install and run

```bash
poetry install
cp .env.example .env            # Fill in Azure resource details
poetry run python scripts/setup_agent.py   # Provisions everything
poetry run chainlit run app.py             # http://localhost:8000
```

### Deploy to Azure

```bash
az acr login --name <your-acr>
docker build -t <acr>.azurecr.io/thoughtful-ai-agent:latest .
docker push <acr>.azurecr.io/thoughtful-ai-agent:latest
az containerapp update --name <app> --resource-group <rg> --image <acr>.azurecr.io/thoughtful-ai-agent:latest
```

Or push to `main` — GitHub Actions will build, push, and deploy automatically.

### Cleanup

```bash
poetry run python scripts/teardown_agent.py   # Removes all app-specific Azure resources
```

## Agent Behavior

The agent prompt enforces three rules:

1. **Always use the tool** — `knowledge_base_retrieve` is called on every question, no exceptions
2. **Never fabricate** — if the KB doesn't have the answer, the agent says so rather than guessing
3. **Transparent fallback** — when answering from general knowledge, the agent explicitly states that the information isn't from the KB
