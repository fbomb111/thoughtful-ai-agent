"""
One-command Azure provisioning for the Thoughtful AI support agent.

Provisions all resources needed for a Foundry agent with RAG:
1. Blob Container - stores knowledge base documents
2. Upload Docs - uploads markdown files to blob storage
3. Knowledge Source - auto-creates AI Search datasource, index, skillset, indexer
4. Knowledge Base - references knowledge source for RAG retrieval
5. RemoteTool Connection - enables agent to access knowledge base via MCP
6. Agent Version - creates Foundry agent with MCPTool for knowledge_base_retrieve
7. Update .env - writes agent version back to .env file

Usage:
    poetry run python scripts/setup_agent.py
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

AI_SEARCH_KB_API = "2025-11-01-Preview"
FOUNDRY_API_VERSION = "2025-11-15-preview"
MGMT_API_VERSION = "2025-10-01-preview"
AUTH_SCOPE = "https://ai.azure.com/.default"
EMBEDDING_MODEL = "text-embedding-3-large"
CHAT_MODEL = "gpt-4.1"


@dataclass
class AppConfig:
    """Configuration for the Thoughtful AI agent."""

    app_name: str = "thoughtful-ai"
    agent_name: str = "thoughtful-support"
    agent_description: str = "Thoughtful AI Customer Support Agent"
    docs_dir: str = "data"
    env_var_version_key: str = "THOUGHTFUL_AGENT_VERSION"
    agent_model: str = "gpt-4.1"
    agent_temperature: float = 0.4

    # Derived names
    container_name: str = field(init=False)
    ks_name: str = field(init=False)
    kb_name: str = field(init=False)
    index_name: str = field(init=False)
    connection_name: str = field(init=False)

    def __post_init__(self):
        self.container_name = self.app_name
        self.ks_name = f"{self.app_name}-ks"
        self.kb_name = f"{self.app_name}-kb"
        self.index_name = f"{self.ks_name}-index"
        self.connection_name = f"{self.app_name}-connection"


class AzureEnv:
    """Azure environment configuration loaded from .env."""

    def __init__(self):
        self.storage_account = os.getenv("DOC_CHAT_STORAGE_ACCOUNT", "docchatstorageeastus")
        self.ai_search_service = os.getenv("AI_SEARCH_SERVICE_NAME", "doc-chat-ai-search")
        self.ai_search_admin_key = os.getenv("AI_SEARCH_ADMIN_KEY")
        self.project_endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        self.managed_identity_client_id = os.getenv("MANAGED_IDENTITY_CLIENT_ID")
        self.azure_subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
        self.foundry_resource_group = os.getenv("FOUNDRY_RESOURCE_GROUP", "rg-doc-chat")

        # Parse hub/project from endpoint
        hub_match = re.search(r"https://([^.]+)\.services\.ai\.azure\.com", self.project_endpoint or "")
        self.foundry_hub_name = hub_match.group(1) if hub_match else "doc-chat"

        project_match = re.search(r"/projects/([^/]+)$", self.project_endpoint or "")
        self.foundry_project_name = project_match.group(1) if project_match else "doc-chat"

        # OpenAI
        self.openai_resource_uri = os.getenv(
            "OPENAI_RESOURCE_URI", f"https://{self.foundry_hub_name}.openai.azure.com"
        )
        self.openai_api_key = os.getenv("AZURE_OPENAI_API_KEY", "")

        # Derived
        self.ai_search_url = f"https://{self.ai_search_service}.search.windows.net"
        self.project_resource_id = (
            f"/subscriptions/{self.azure_subscription_id}"
            f"/resourceGroups/{self.foundry_resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.foundry_hub_name}"
            f"/projects/{self.foundry_project_name}"
        )


# =============================================================================
# Helpers
# =============================================================================


def get_credential(env: AzureEnv) -> TokenCredential:
    """Get Azure credential."""
    if env.managed_identity_client_id:
        try:
            cred = ManagedIdentityCredential(client_id=env.managed_identity_client_id)
            cred.get_token("https://storage.azure.com/.default")
            print(f"  Using ManagedIdentityCredential (client_id={env.managed_identity_client_id})")
            return cred
        except Exception:
            print("  (Managed identity unavailable, using az cli credentials)")
    return DefaultAzureCredential()


def get_foundry_token(credential: TokenCredential) -> str:
    return credential.get_token(AUTH_SCOPE).token


def get_management_token(credential: TokenCredential) -> str:
    return credential.get_token("https://management.azure.com/.default").token


def search_headers(env: AzureEnv) -> dict:
    return {"Content-Type": "application/json", "api-key": env.ai_search_admin_key}


# =============================================================================
# Setup Steps
# =============================================================================


def step_1_create_blob_container(config: AppConfig, env: AzureEnv, credential):
    """Create blob container in shared storage account."""
    print("[Step 1/7] Creating blob container...")
    blob_service = BlobServiceClient(
        account_url=f"https://{env.storage_account}.blob.core.windows.net",
        credential=credential,
    )
    container_client = blob_service.get_container_client(config.container_name)
    if container_client.exists():
        print(f"  Container '{config.container_name}' already exists")
    else:
        container_client.create_container()
        print(f"  Container '{config.container_name}' created")
    return blob_service


def step_2_upload_docs(config: AppConfig, blob_service, docs_dir: Path):
    """Upload markdown files to blob container."""
    print("[Step 2/7] Uploading docs to blob storage...")
    if not docs_dir.exists():
        print(f"  WARNING: Docs directory not found: {docs_dir}")
        return 0

    container_client = blob_service.get_container_client(config.container_name)
    md_files = list(docs_dir.rglob("*.md"))

    if not md_files:
        print("  No .md files found")
        return 0

    uploaded = 0
    for md_file in md_files:
        blob_name = str(md_file.relative_to(docs_dir))
        blob_client = container_client.get_blob_client(blob_name)
        with open(md_file, "rb") as f:
            blob_client.upload_blob(f, overwrite=True)
        uploaded += 1
        print(f"  Uploaded: {blob_name}")

    print(f"  {uploaded} files uploaded")
    return uploaded


def step_3_create_knowledge_source(config: AppConfig, env: AzureEnv):
    """Create knowledge source that auto-provisions AI Search infrastructure."""
    print("[Step 3/7] Creating knowledge source...")

    resp = requests.get(
        f"{env.ai_search_url}/knowledgesources/{config.ks_name}?api-version={AI_SEARCH_KB_API}",
        headers=search_headers(env),
    )
    if resp.status_code == 200:
        print(f"  Knowledge source '{config.ks_name}' already exists")
        return

    storage_rg = os.getenv("STORAGE_RESOURCE_GROUP", "rg-doc-chat")
    connection_string = (
        f"ResourceId=/subscriptions/{env.azure_subscription_id}"
        f"/resourceGroups/{storage_rg}"
        f"/providers/Microsoft.Storage/storageAccounts/{env.storage_account};"
    )

    identity_rg = os.getenv("IDENTITY_RESOURCE_GROUP", "rg-doc-chat")
    identity_name = os.getenv("IDENTITY_NAME", "doc-chat-identity")
    identity_resource_id = (
        f"/subscriptions/{env.azure_subscription_id}"
        f"/resourcegroups/{identity_rg}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identity_name}"
    )

    body = {
        "name": config.ks_name,
        "kind": "azureBlob",
        "azureBlobParameters": {
            "connectionString": connection_string,
            "containerName": config.container_name,
            "ingestionParameters": {
                "contentExtractionMode": "minimal",
                "identity": {
                    "@odata.type": "#Microsoft.Azure.Search.DataUserAssignedIdentity",
                    "userAssignedIdentity": identity_resource_id,
                },
                "embeddingModel": {
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": env.openai_resource_uri,
                        "deploymentId": EMBEDDING_MODEL,
                        "apiKey": env.openai_api_key,
                        "modelName": EMBEDDING_MODEL,
                    },
                },
                "chatCompletionModel": {
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": env.openai_resource_uri,
                        "deploymentId": CHAT_MODEL,
                        "apiKey": env.openai_api_key,
                        "modelName": CHAT_MODEL,
                    },
                },
            },
        },
    }

    resp = requests.put(
        f"{env.ai_search_url}/knowledgesources/{config.ks_name}?api-version={AI_SEARCH_KB_API}",
        headers=search_headers(env),
        json=body,
    )

    if resp.status_code in [200, 201, 202]:
        print(f"  Knowledge source '{config.ks_name}' created")
        result = resp.json()
        created = result.get("azureBlobParameters", {}).get("createdResources", {})
        if created:
            print(f"    Auto-created: datasource={created.get('datasource')}")
            print(f"                  index={created.get('index')}")
            print(f"                  skillset={created.get('skillset')}")
            print(f"                  indexer={created.get('indexer')}")
    else:
        raise Exception(f"Failed to create knowledge source: {resp.status_code} - {resp.text}")


def step_4_create_knowledge_base(config: AppConfig, env: AzureEnv):
    """Create knowledge base that references the knowledge source."""
    print("[Step 4/7] Creating knowledge base...")

    resp = requests.get(
        f"{env.ai_search_url}/knowledgebases/{config.kb_name}?api-version={AI_SEARCH_KB_API}",
        headers=search_headers(env),
    )
    if resp.status_code == 200:
        print(f"  Knowledge base '{config.kb_name}' already exists")
        return

    body = {
        "name": config.kb_name,
        "description": f"Knowledge base for {config.agent_description}",
        "knowledgeSources": [{"name": config.ks_name}],
        "outputMode": "extractiveData",
        "retrievalReasoningEffort": {"kind": "low"},
        "models": [
            {
                "kind": "azureOpenAI",
                "azureOpenAIParameters": {
                    "resourceUri": env.openai_resource_uri,
                    "deploymentId": CHAT_MODEL,
                    "apiKey": env.openai_api_key,
                    "modelName": CHAT_MODEL,
                },
            }
        ],
    }

    resp = requests.put(
        f"{env.ai_search_url}/knowledgebases/{config.kb_name}?api-version={AI_SEARCH_KB_API}",
        headers=search_headers(env),
        json=body,
    )

    if resp.status_code in [200, 201]:
        print(f"  Knowledge base '{config.kb_name}' created")
    else:
        raise Exception(f"Failed to create knowledge base: {resp.status_code} - {resp.text}")


def step_5_create_remotetool_connection(config: AppConfig, env: AzureEnv):
    """Create RemoteTool connection pointing to KB MCP endpoint."""
    print("[Step 5/7] Creating RemoteTool connection...")

    mcp_endpoint = (
        f"{env.ai_search_url}/knowledgebases/{config.kb_name}/mcp?api-version={AI_SEARCH_KB_API}"
    )

    connection_url = (
        f"https://management.azure.com{env.project_resource_id}"
        f"/connections/{config.connection_name}?api-version={MGMT_API_VERSION}"
    )

    credential = get_credential(env)
    mgmt_token = get_management_token(credential)
    headers = {"Authorization": f"Bearer {mgmt_token}", "Content-Type": "application/json"}

    body = {
        "name": config.connection_name,
        "type": "Microsoft.MachineLearningServices/workspaces/connections",
        "properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": mcp_endpoint,
            "isSharedToAll": True,
            "audience": "https://search.azure.com/",
            "metadata": {"ApiType": "Azure"},
        },
    }

    resp = requests.put(connection_url, headers=headers, json=body)

    if resp.status_code in [200, 201]:
        print(f"  RemoteTool connection '{config.connection_name}' created")
    elif resp.status_code == 409:
        print(f"  Connection already exists, updating...")
        resp = requests.put(connection_url, headers=headers, json=body)
        if resp.status_code in [200, 201]:
            print(f"  RemoteTool connection '{config.connection_name}' updated")
        else:
            raise Exception(f"Failed to update connection: {resp.status_code} - {resp.text}")
    else:
        raise Exception(f"Failed to create connection: {resp.status_code} - {resp.text}")


def step_6_create_agent_version(config: AppConfig, env: AzureEnv, token: str):
    """Create agent version with MCPTool for knowledge base retrieval."""
    print("[Step 6/7] Creating agent version...")

    instructions_file = PROJECT_ROOT / "agent_config" / "instructions.md"
    if not instructions_file.exists():
        raise FileNotFoundError(f"Instructions file not found: {instructions_file}")
    instructions = instructions_file.read_text()
    print(f"  Instructions loaded: {len(instructions)} characters")

    mcp_endpoint = (
        f"{env.ai_search_url}/knowledgebases/{config.kb_name}/mcp?api-version={AI_SEARCH_KB_API}"
    )

    url = (
        f"{env.project_endpoint}/agents/{config.agent_name}/versions"
        f"?api-version={FOUNDRY_API_VERSION}"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    body = {
        "description": config.agent_description,
        "definition": {
            "kind": "prompt",
            "model": config.agent_model,
            "instructions": instructions,
            "temperature": config.agent_temperature,
            "tools": [
                {
                    "type": "mcp",
                    "server_label": "knowledge-base",
                    "server_url": mcp_endpoint,
                    "require_approval": "never",
                    "allowed_tools": ["knowledge_base_retrieve"],
                    "project_connection_id": config.connection_name,
                }
            ],
        },
    }

    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code in [200, 201]:
        result = resp.json()
        print(f"  Agent version created!")
        print(f"    Name: {result.get('name')}")
        print(f"    Version: {result.get('version')}")
        return result
    else:
        raise Exception(f"Failed to create agent version: {resp.status_code} - {resp.text}")


def step_7_update_env_file(config: AppConfig, agent_version):
    """Update .env with the new agent version."""
    print("[Step 7/7] Updating .env with agent version...")

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        print(f"  WARNING: .env not found, manually set: {config.env_var_version_key}={agent_version}")
        return

    content = env_file.read_text()
    pattern = rf"^{config.env_var_version_key}=.*$"

    if re.search(pattern, content, re.MULTILINE):
        new_content = re.sub(
            pattern, f"{config.env_var_version_key}={agent_version}", content, flags=re.MULTILINE
        )
        env_file.write_text(new_content)
        print(f"  Updated {config.env_var_version_key}={agent_version}")
    else:
        new_content = content.rstrip() + f"\n{config.env_var_version_key}={agent_version}\n"
        env_file.write_text(new_content)
        print(f"  Added {config.env_var_version_key}={agent_version}")


# =============================================================================
# Main
# =============================================================================


def main():
    load_dotenv(PROJECT_ROOT / ".env")
    config = AppConfig()
    env = AzureEnv()

    docs_dir = PROJECT_ROOT / config.docs_dir

    print("=" * 60)
    print(f"{config.agent_description} - Agent Setup")
    print("=" * 60)
    print()

    # Validate required env vars
    missing = []
    if not env.project_endpoint:
        missing.append("AZURE_AI_PROJECT_ENDPOINT")
    if not env.ai_search_admin_key:
        missing.append("AI_SEARCH_ADMIN_KEY")
    if not env.openai_api_key:
        missing.append("AZURE_OPENAI_API_KEY")
    if not env.azure_subscription_id:
        missing.append("AZURE_SUBSCRIPTION_ID")
    if missing:
        print(f"ERROR: Missing required env vars: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the values.")
        return

    print(f"App Name:         {config.app_name}")
    print(f"Agent Name:       {config.agent_name}")
    print(f"Project Endpoint: {env.project_endpoint}")
    print(f"Search Service:   {env.ai_search_service}")
    print(f"Storage Account:  {env.storage_account}")
    print()

    try:
        credential = get_credential(env)

        blob_service = step_1_create_blob_container(config, env, credential)
        print()
        step_2_upload_docs(config, blob_service, docs_dir)
        print()
        step_3_create_knowledge_source(config, env)
        print()
        step_4_create_knowledge_base(config, env)
        print()
        step_5_create_remotetool_connection(config, env)
        print()

        print("Authenticating with Foundry...")
        token = get_foundry_token(credential)
        print()
        result = step_6_create_agent_version(config, env, token)
        agent_version = result.get("version")
        print()

        step_7_update_env_file(config, agent_version)

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback

        traceback.print_exc()
        return

    print()
    print("=" * 60)
    print("SETUP COMPLETE!")
    print("=" * 60)
    print()
    print(f"Agent: {config.agent_name} v{agent_version}")
    print()
    print("To start the chatbot:")
    print("  poetry run chainlit run app.py")
    print()
    print("To clean up resources:")
    print("  poetry run python scripts/teardown_agent.py")


if __name__ == "__main__":
    main()
