import logging
import sys
import json
import traceback
import boto3
import os

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
favorite_tools_path = os.path.join(script_dir, "favorite_tools.json")


def load_config():
    config = None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = {}
        config["projectName"] = "agentcore"

        session = boto3.Session()
        bedrock_region = session.region_name
        config["region"] = bedrock_region

        sts = boto3.client("sts")
        accountId = sts.get_caller_identity()["Account"]
        config["accountId"] = accountId

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    return config


def load_favorite_tools() -> dict[str, list[str]]:
    try:
        with open(favorite_tools_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    favorites: dict[str, list[str]] = {}
    for key in ("MCP", "SKILL"):
        values = data.get(key, [])
        if isinstance(values, list):
            favorites[key] = [v for v in values if isinstance(v, str) and v.strip()]
        else:
            favorites[key] = []
    return favorites


def save_favorite_tools(
    *, skills: list[str] | None = None, mcp_servers: list[str] | None = None
) -> dict[str, list[str]]:
    """Persist favorite tool defaults in favorite_tools.json."""
    favorites = load_favorite_tools()
    if skills is not None:
        favorites["SKILL"] = [v for v in skills if isinstance(v, str) and v.strip()]
    if mcp_servers is not None:
        favorites["MCP"] = [v for v in mcp_servers if isinstance(v, str) and v.strip()]

    with open(favorite_tools_path, "w", encoding="utf-8") as f:
        json.dump(favorites, f, ensure_ascii=False, indent=2)
    return favorites


def get_initial_tool_defaults() -> tuple[list[str], list[str]]:
    """Return initial skill/MCP defaults from favorite_tools.json."""
    favorite_tools = load_favorite_tools()
    default_skills = favorite_tools.get("SKILL") or []
    default_mcp_servers = favorite_tools.get("MCP") or []
    return default_skills, default_mcp_servers


config = load_config()

bedrock_region = config["region"]
projectName = config["projectName"]
accountId = config["accountId"]
region = config.get("region", "us-west-2")
s3_bucket = config.get(
    "s3_bucket", f"storage-for-rag-project-{accountId}-{region}"
)
sharing_url = (config.get("sharing_url") or "").rstrip("/")
knowledge_base_id = config.get("knowledge_base_id")
data_source_id = config.get("data_source_id")


def get_contents_type(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith((".ppt", ".pptx")):
        return "application/vnd.ms-powerpoint"
    if lower.endswith((".doc", ".docx")):
        return "application/msword"
    if lower.endswith((".xls", ".xlsx")):
        return "application/vnd.ms-excel"
    if lower.endswith(".py"):
        return "text/x-python"
    if lower.endswith(".js"):
        return "application/javascript"
    if lower.endswith(".md"):
        return "text/markdown"
    if lower.endswith(".json"):
        return "application/json"
    return "no info"


def update_rag_info():
    """Resolve knowledge_base_id / data_source_id for this project and persist."""
    global knowledge_base_id, data_source_id

    kb_id = None
    ds_id = None
    try:
        client = boto3.client(service_name="bedrock-agent", region_name=region)

        response = client.list_knowledge_bases(maxResults=50)
        logger.info("(list_knowledge_bases) response: %s", response)

        knowledge_base_name = config.get("knowledge_base_name") or projectName
        for summary in response.get("knowledgeBaseSummaries") or []:
            if summary.get("name") == knowledge_base_name:
                kb_id = summary["knowledgeBaseId"]
                logger.info("knowledge_base_id: %s", kb_id)
                break

        if not kb_id:
            logger.warning(
                "Knowledge Base not found for project: %s", knowledge_base_name
            )
            return kb_id, ds_id

        if not s3_bucket:
            logger.warning("s3_bucket is not configured, skipping data source lookup")
            return kb_id, ds_id

        response = client.list_data_sources(
            knowledgeBaseId=kb_id,
            maxResults=10,
        )
        logger.info("(list_data_sources) response: %s", response)

        for data_source in response.get("dataSourceSummaries") or []:
            logger.info("data_source: %s", data_source)
            if data_source.get("name") == s3_bucket:
                ds_id = data_source["dataSourceId"]
                logger.info("data_source_id: %s", ds_id)
                break

        knowledge_base_id = kb_id
        data_source_id = ds_id
        config["knowledge_base_id"] = kb_id
        config["data_source_id"] = ds_id
        config["s3_bucket"] = s3_bucket
        config["region"] = region
        config["projectName"] = projectName
        config["accountId"] = accountId
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    except Exception:
        logger.info("error message: %s", traceback.format_exc())

    return kb_id, ds_id


if not knowledge_base_id or not data_source_id:
    knowledge_base_id, data_source_id = update_rag_info()


def sync_data_source():
    """Start a Bedrock Knowledge Base ingestion job (agent-plugins pattern)."""
    global knowledge_base_id, data_source_id

    if not knowledge_base_id or not data_source_id:
        knowledge_base_id, data_source_id = update_rag_info()

    if not (knowledge_base_id and data_source_id):
        logger.error(
            "knowledge_base_id or data_source_id is not configured; cannot sync"
        )
        return None

    try:
        bedrock_client = boto3.client(
            service_name="bedrock-agent",
            region_name=region,
        )
        response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
        )
        logger.info("(start_ingestion_job) response: %s", response)
        job = response.get("ingestionJob", {})
        return {
            "ingestion_job_id": job.get("ingestionJobId"),
            "status": job.get("status"),
        }
    except Exception:
        logger.info("error message: %s", traceback.format_exc())
        return None
