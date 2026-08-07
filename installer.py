#!/usr/bin/env python3
"""
AWS Infrastructure Installer using boto3
This script provisions AgentCore Harness (S3, skills, VPC, CloudFront,
IAM roles, S3 Vectors Knowledge Base, CreateHarness) for local development.
"""

import boto3
import json
import time
import logging
import os
import re
import sys
import mimetypes
import uuid
from typing import Dict, List, Optional, Tuple
from botocore.exceptions import ClientError

# Configuration
project_name = "agentcore-harness"  # at least 3 characters
region = "us-west-2"
DEFAULT_MODEL_ID = "global.anthropic.claude-opus-4-7"
VPC_CIDR = "10.52.0.0/16"

# Shared S3 / CloudFront with agent-skills (rag-project) — reuse if already present.
SHARED_STORAGE_NAME = "rag-project"
cloudfront_comment = f"CloudFront-for-{SHARED_STORAGE_NAME}"
oai_comment = f"OAI for {SHARED_STORAGE_NAME}"

# CreateHarness harnessName: Pattern [a-zA-Z][a-zA-Z0-9_]{0,39} — no hyphens.
_HARNESS_NAME_API_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")

sts_client = boto3.client("sts", region_name=region)
account_id = str(sts_client.get_caller_identity()["Account"])

# Bedrock Knowledge Base + S3 Vectors (same pattern as power-runtime)
vector_index_name = project_name
vector_bucket_name = f"{project_name}-{account_id}"
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"
# Bedrock Knowledge Base requires these metadata keys as non-filterable on S3 Vectors index
BEDROCK_NON_FILTERABLE_METADATA_KEYS = [
    "AMAZON_BEDROCK_TEXT",
    "AMAZON_BEDROCK_METADATA",
]

iam_client = boto3.client("iam", region_name=region)
s3_client = boto3.client("s3", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
s3vectors_client = boto3.client("s3vectors", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=region,
)

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "application", "config.json")
SKILLS_DIR = os.path.join(WORKING_DIR, "skills")
SKILLS_S3_PREFIX = "skills"


def s3_vectors_bucket_arn(bucket_name: Optional[str] = None) -> str:
    """ARN for an S3 vector bucket."""
    name = bucket_name or vector_bucket_name
    return f"arn:aws:s3vectors:{region}:{account_id}:bucket/{name}"


def s3_vectors_index_arn(
    index_name: Optional[str] = None,
    bucket_name: Optional[str] = None,
) -> str:
    """ARN for a vector index within an S3 vector bucket."""
    idx = index_name or vector_index_name
    return f"{s3_vectors_bucket_arn(bucket_name)}/index/{idx}"


def _bucket_name() -> str:
    return f"storage-for-{SHARED_STORAGE_NAME}-{account_id}-{region}"


def _cloudfront_comment() -> str:
    return cloudfront_comment


def _oai_comment() -> str:
    return oai_comment


def setup_logging(log_level=logging.INFO):
    """Setup logging configuration."""
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(),
        ],
    )

    return logging.getLogger(__name__)


logger = setup_logging()


def harness_name_for_api(name: str) -> str:
    """
    Map projectName to CreateHarness harnessName.
    Only for harnessName: replace '-' with '_' (API disallows hyphens).
    """
    normalized = (name or "").replace("-", "_")
    if not _HARNESS_NAME_API_RE.fullmatch(normalized):
        logger.error(
            "CreateHarness harnessName must match [a-zA-Z][a-zA-Z0-9_]{0,39} "
            f"(after '-'→'_'): got {normalized!r} from projectName={name!r}"
        )
        sys.exit(1)
    return normalized


def get_max_output_tokens(model_id: str = "") -> int:
    """Return max output tokens (`max_tokens` cap) per Amazon Bedrock Anthropic Claude model cards."""
    mid = model_id.lower()
    if "claude-opus-4-7" in mid or "claude-opus-4-6" in mid:
        return 128000
    if "claude-opus-4-5" in mid:
        return 64000
    if "claude-opus-4" in mid or "claude-4-opus" in mid:
        return 128000
    if "claude-sonnet-4" in mid or "claude-4-sonnet" in mid or "claude-haiku-4" in mid:
        return 64000
    return 8192


def create_iam_role(
    role_name: str,
    assume_role_policy: Dict,
    managed_policies: Optional[List[str]] = None,
    description: Optional[str] = None,
) -> tuple[str, bool]:
    """Create IAM role (or update trust/policies if it already exists).

    Returns (role_arn, created) where created is True only for a newly created role.
    """
    logger.debug(f"Creating IAM role: {role_name}")

    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=description or f"Role for {role_name}",
        )
        role_arn = response["Role"]["Arn"]
        logger.debug(f"Role created: {role_arn}")

        if managed_policies:
            logger.debug(f"Attaching {len(managed_policies)} managed policies")
            for policy_arn in managed_policies:
                iam_client.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy_arn,
                )
                logger.debug(f"Attached policy: {policy_arn}")

        logger.info(f"✓ IAM role created: {role_name}")
        return role_arn, True

    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.warning(f"IAM role already exists: {role_name}")
            response = iam_client.get_role(RoleName=role_name)
            role_arn = response["Role"]["Arn"]

            try:
                logger.info(f"Updating trust policy for existing role: {role_name}")
                iam_client.update_assume_role_policy(
                    RoleName=role_name,
                    PolicyDocument=json.dumps(assume_role_policy),
                )
                logger.info(f"✓ Updated trust policy for role: {role_name}")
            except ClientError as trust_policy_error:
                logger.error(
                    f"✗ Failed to update trust policy for role {role_name}: "
                    f"{trust_policy_error}"
                )
                raise

            if managed_policies:
                try:
                    attached = iam_client.list_attached_role_policies(RoleName=role_name)
                    current = {
                        p["PolicyArn"] for p in attached["AttachedPolicies"]
                    }
                    for policy_arn in managed_policies:
                        if policy_arn not in current:
                            iam_client.attach_role_policy(
                                RoleName=role_name,
                                PolicyArn=policy_arn,
                            )
                            logger.debug(f"Attached missing policy: {policy_arn}")
                except ClientError as policy_error:
                    logger.warning(f"Could not update managed policies: {policy_error}")

            return role_arn, False
        logger.error(f"Failed to create IAM role {role_name}: {e}")
        raise


def attach_inline_policy(role_name: str, policy_name: str, policy_document: Dict):
    """Attach or update inline policy to IAM role."""
    logger.debug(f"Attaching/updating inline policy {policy_name} to {role_name}")

    try:
        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name[:128],
            PolicyDocument=json.dumps(policy_document),
        )
        logger.debug(f"Policy {policy_name} attached/updated successfully")
    except ClientError as e:
        logger.error(f"Error attaching/updating policy {policy_name}: {e}")
        raise


def load_config(config_path: str) -> Dict:
    """Load application config, creating defaults when missing."""
    global project_name, region, account_id
    global vector_index_name, vector_bucket_name
    global agentcore_control_client, s3_client, cloudfront_client, ec2_client
    global s3vectors_client

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.warning(f"Error loading config ({config_path}): {e}; creating defaults")
        config = {
            "projectName": project_name,
            "region": region,
            "accountId": account_id,
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    project_name = config.get("projectName") or project_name
    region = config.get("region") or region
    raw_account = config.get("accountId")
    if raw_account is not None and str(raw_account).strip() != "":
        account_id = str(raw_account).strip()
    else:
        account_id = str(sts_client.get_caller_identity()["Account"])
        config["accountId"] = account_id

    config["projectName"] = project_name
    config["region"] = region
    config["accountId"] = account_id

    vector_index_name = project_name
    vector_bucket_name = f"{project_name}-{account_id}"

    agentcore_control_client = boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
    )
    s3_client = boto3.client("s3", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    s3vectors_client = boto3.client("s3vectors", region_name=region)
    cloudfront_client = boto3.client("cloudfront", region_name=region)
    return config


def _bedrock_knowledge_base_trust_policy() -> Dict:
    """Trust policy for Bedrock Knowledge Base service role (AWS recommended)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"
                        )
                    },
                },
            }
        ],
    }


def wait_for_iam_role_propagation(role_name: str, wait_seconds: int = 15) -> None:
    """Wait for IAM role and inline policies to propagate."""
    logger.info(f"  Waiting {wait_seconds}s for IAM role propagation: {role_name}")
    time.sleep(wait_seconds)

    expected_policies = {
        f"kb-bedrock-policy-for-{project_name}",
        f"kb-s3-policy-for-{project_name}",
        f"kb-opensearch-policy-for-{project_name}",
        f"kb-s3vectors-policy-for-{project_name}",
    }
    for attempt in range(3):
        try:
            attached = iam_client.list_role_policies(RoleName=role_name)
            missing = expected_policies - set(attached.get("PolicyNames", []))
            if not missing:
                logger.info("  ✓ Knowledge Base role inline policies are attached")
                return
            logger.debug(
                f"  Waiting for inline policies (attempt {attempt + 1}/3): {sorted(missing)}"
            )
        except ClientError as e:
            logger.debug(f"  Could not list role policies yet: {e}")
        time.sleep(5)

    logger.warning(
        "  Some Knowledge Base role inline policies may not be visible yet; continuing"
    )


def _project_s3_bucket_arns() -> Tuple[str, str]:
    """Return (bucket ARN, object ARN) for the project storage bucket."""
    bucket_arn = f"arn:aws:s3:::{_bucket_name()}"
    return bucket_arn, f"{bucket_arn}/*"


def create_knowledge_base_role() -> str:
    """Create Knowledge Base IAM role with least-privilege policies."""
    logger.info("[3/11] Creating Knowledge Base IAM role")
    role_name = f"role-knowledge-base-for-{project_name}-{region}"

    assume_role_policy = _bedrock_knowledge_base_trust_policy()
    role_arn, role_created = create_iam_role(
        role_name,
        assume_role_policy,
        description="Bedrock Knowledge Base service role",
    )
    bucket_arn, object_arn = _project_s3_bucket_arns()

    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeEmbeddingModels",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                    f"arn:aws:bedrock:{region}:*:inference-profile/*",
                ],
            }
        ],
    }
    attach_inline_policy(role_name, f"kb-bedrock-policy-for-{project_name}", bedrock_policy)

    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListKnowledgeBaseBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [bucket_arn],
            },
            {
                "Sid": "ReadKnowledgeBaseObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [object_arn],
            },
        ],
    }
    attach_inline_policy(role_name, f"kb-s3-policy-for-{project_name}", s3_policy)

    opensearch_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OpenSearchServerlessAccess",
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": [f"arn:aws:aoss:{region}:{account_id}:collection/*"],
            }
        ],
    }
    attach_inline_policy(role_name, f"kb-opensearch-policy-for-{project_name}", opensearch_policy)

    vector_arn = s3_vectors_bucket_arn()
    s3vectors_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3VectorsAccess",
                "Effect": "Allow",
                "Action": [
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListVectorBuckets",
                    "s3vectors:GetIndex",
                    "s3vectors:ListIndexes",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:ListVectors",
                ],
                "Resource": [
                    vector_arn,
                    f"{vector_arn}/index/*",
                ],
            }
        ],
    }
    attach_inline_policy(
        role_name, f"kb-s3vectors-policy-for-{project_name}", s3vectors_policy
    )

    if role_created:
        wait_for_iam_role_propagation(role_name)
    else:
        logger.info(f"  Skipping IAM wait (KB role already exists: {role_name})")
    logger.info(f"✓ Knowledge Base role ready: {role_arn}")
    return role_arn


def check_knowledge_base_exists() -> Optional[str]:
    """Check if Knowledge Base exists and return its ID if found."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.debug(f"Knowledge Base found: {kb['knowledgeBaseId']}")
                return kb["knowledgeBaseId"]
        return None
    except Exception as e:
        logger.debug(f"Error checking Knowledge Base existence: {e}")
        return None


def delete_knowledge_base(knowledge_base_id: str) -> None:
    """Delete Knowledge Base and its data sources."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        try:
            data_sources = bedrock_agent_client.list_data_sources(
                knowledgeBaseId=knowledge_base_id,
                maxResults=100,
            )
            for ds in data_sources.get("dataSourceSummaries", []):
                try:
                    bedrock_agent_client.delete_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=ds["dataSourceId"],
                    )
                    logger.debug(f"Deleted data source: {ds['dataSourceId']}")
                except Exception as e:
                    logger.warning(f"Failed to delete data source {ds['dataSourceId']}: {e}")
        except Exception as e:
            logger.debug(f"Error listing/deleting data sources: {e}")

        bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=knowledge_base_id)
        logger.info(f"Deleted Knowledge Base: {knowledge_base_id}")

        logger.debug("Waiting for Knowledge Base deletion to complete...")
        max_wait = 60
        waited = 0
        while waited < max_wait:
            try:
                kb_response = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=knowledge_base_id
                )
                status = kb_response["knowledgeBase"]["status"]
                if status == "DELETED":
                    break
                time.sleep(5)
                waited += 5
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    logger.debug("Knowledge Base deletion confirmed")
                    break
                raise

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.debug(f"Knowledge Base {knowledge_base_id} already deleted")
        else:
            logger.error(f"Failed to delete Knowledge Base {knowledge_base_id}: {e}")
            raise


def create_s3_vectors_store() -> Dict[str, str]:
    """Create S3 vector bucket and index for Bedrock Knowledge Base."""
    logger.info("[4/11] Creating S3 Vectors store (vector bucket + index)")

    vector_bucket_arn = s3_vectors_bucket_arn()
    index_arn = s3_vectors_index_arn()

    try:
        s3vectors_client.create_vector_bucket(vectorBucketName=vector_bucket_name)
        logger.info(f"  ✓ Vector bucket created: {vector_bucket_name}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ResourceAlreadyExistsException"):
            logger.warning(f"  Vector bucket already exists: {vector_bucket_name}")
            try:
                existing = s3vectors_client.get_vector_bucket(
                    vectorBucketName=vector_bucket_name
                )
                vector_bucket_arn = existing["vectorBucket"]["vectorBucketArn"]
            except ClientError:
                pass
        else:
            logger.error(f"Failed to create vector bucket: {e}")
            raise

    try:
        response = s3vectors_client.create_index(
            vectorBucketName=vector_bucket_name,
            indexName=vector_index_name,
            dataType=embedding_data_type,
            dimension=embedding_dimensions,
            distanceMetric=distance_metric,
            metadataConfiguration={
                "nonFilterableMetadataKeys": BEDROCK_NON_FILTERABLE_METADATA_KEYS,
            },
        )
        index_arn = response.get("indexArn", index_arn)
        logger.info(f"  ✓ Vector index created: {vector_index_name}")
        logger.info("  Waiting for vector index to be ready...")
        time.sleep(15)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ResourceAlreadyExistsException"):
            logger.warning(f"  Vector index already exists: {vector_index_name}")
            try:
                existing = s3vectors_client.get_index(
                    vectorBucketName=vector_bucket_name,
                    indexName=vector_index_name,
                )
                index_arn = existing["index"]["indexArn"]
            except ClientError:
                pass
        else:
            logger.error(f"Failed to create vector index: {e}")
            raise

    logger.info("✓ S3 Vectors store ready")
    logger.info(f"  Vector bucket ARN: {vector_bucket_arn}")
    logger.info(f"  Vector index ARN: {index_arn}")

    return {
        "vectorBucketName": vector_bucket_name,
        "vectorBucketArn": vector_bucket_arn,
        "indexName": vector_index_name,
        "indexArn": index_arn,
    }


def ensure_data_source(
    bedrock_agent_client,
    knowledge_base_id: str,
    s3_bucket_name: str,
) -> str:
    """Create S3 data source with default parser when missing."""
    data_sources = bedrock_agent_client.list_data_sources(
        knowledgeBaseId=knowledge_base_id,
        maxResults=100,
    )
    for ds in data_sources.get("dataSourceSummaries", []):
        if ds["name"] == s3_bucket_name:
            logger.info(f"  Data source already exists: {ds['dataSourceId']}")
            return ds["dataSourceId"]

    logger.info("  Creating data source with default parser...")
    data_source_response = bedrock_agent_client.create_data_source(
        knowledgeBaseId=knowledge_base_id,
        name=s3_bucket_name,
        description=f"S3 data source: {s3_bucket_name}",
        dataDeletionPolicy="RETAIN",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{s3_bucket_name}",
                "inclusionPrefixes": ["docs/"],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": 300,
                    "overlapPercentage": 20,
                },
            },
        },
    )
    data_source_id = data_source_response["dataSource"]["dataSourceId"]
    logger.info(f"  ✓ Data source created: {data_source_id}")
    return data_source_id


def create_knowledge_base_with_s3_vectors(
    s3_vectors_info: Dict[str, str], knowledge_base_role_arn: str, s3_bucket_name: str
) -> Tuple[str, str]:
    """Create Knowledge Base with S3 Vectors as the vector store."""
    logger.info("[4.5/11] Creating Knowledge Base with S3 Vectors")

    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        logger.info("  Checking if Knowledge Base already exists...")
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.warning(f"Knowledge Base already exists: {kb['knowledgeBaseId']}")

                kb_details = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=kb["knowledgeBaseId"]
                )
                storage = kb_details["knowledgeBase"]["storageConfiguration"]
                s3_cfg = storage.get("s3VectorsConfiguration", {})
                kb_index_arn = s3_cfg.get("indexArn")
                storage_type = storage.get("type")

                if storage_type != "S3_VECTORS" or kb_index_arn != s3_vectors_info["indexArn"]:
                    logger.warning("Knowledge Base is not using the expected S3 Vectors index:")
                    logger.warning(f"  Storage type: {storage_type}")
                    logger.warning(f"  Current index ARN: {kb_index_arn}")
                    logger.warning(f"  Expected index ARN: {s3_vectors_info['indexArn']}")

                    delete_knowledge_base(kb["knowledgeBaseId"])
                    break

                logger.info("Knowledge Base is using correct S3 Vectors index")
                data_source_id = ensure_data_source(
                    bedrock_agent_client, kb["knowledgeBaseId"], s3_bucket_name
                )
                return kb["knowledgeBaseId"], data_source_id
        logger.info("  Knowledge Base does not exist. Creating new one...")
    except Exception as e:
        logger.debug(f"Error checking existing Knowledge Base: {e}")

    logger.info("  Verifying Knowledge Base role configuration...")
    try:
        role_response = iam_client.get_role(
            RoleName=f"role-knowledge-base-for-{project_name}-{region}"
        )
        policy_doc = role_response["Role"]["AssumeRolePolicyDocument"]
        if isinstance(policy_doc, str):
            trust_policy = json.loads(policy_doc)
        else:
            trust_policy = policy_doc
        logger.debug(f"  Role trust policy: {json.dumps(trust_policy, indent=2)}")

        statements = trust_policy.get("Statement", [])
        bedrock_allowed = False
        for statement in statements:
            if statement.get("Effect") == "Allow":
                principal = statement.get("Principal", {})
                if principal.get("Service") == "bedrock.amazonaws.com":
                    bedrock_allowed = True
                    break

        if not bedrock_allowed:
            logger.error(
                "  ✗ Knowledge Base role trust policy does not allow bedrock.amazonaws.com"
            )
            logger.error(
                "  Please update the role trust policy manually or delete and recreate the role"
            )
            raise Exception("Knowledge Base role trust policy is incorrect")

        logger.info("  ✓ Knowledge Base role trust policy is correct")
    except ClientError as role_error:
        logger.error(f"  ✗ Failed to verify Knowledge Base role: {role_error}")
        raise

    logger.debug(
        f"Creating Knowledge Base with S3 Vectors index: {s3_vectors_info['indexArn']}"
    )
    response = bedrock_agent_client.create_knowledge_base(
        name=project_name,
        description="Knowledge base with default parser (S3 Vectors)",
        roleArn=knowledge_base_role_arn,
        tags={project_name: "true"},
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": (
                    f"arn:aws:bedrock:{region}::foundation-model/"
                    "amazon.titan-embed-text-v2:0"
                ),
                "embeddingModelConfiguration": {
                    "bedrockEmbeddingModelConfiguration": {
                        "dimensions": embedding_dimensions,
                        "embeddingDataType": "FLOAT32",
                    }
                },
            },
        },
        storageConfiguration={
            "type": "S3_VECTORS",
            "s3VectorsConfiguration": {
                "vectorBucketArn": s3_vectors_info["vectorBucketArn"],
                "indexArn": s3_vectors_info["indexArn"],
            },
        },
    )

    knowledge_base_id = response["knowledgeBase"]["knowledgeBaseId"]
    logger.info(f"✓ Knowledge Base created: {knowledge_base_id}")

    logger.info("  Waiting for Knowledge Base to be active...")
    while True:
        kb_response = bedrock_agent_client.get_knowledge_base(
            knowledgeBaseId=knowledge_base_id
        )
        status = kb_response["knowledgeBase"]["status"]

        if status == "ACTIVE":
            logger.info("  Knowledge Base is now active")
            break
        if status == "FAILED":
            raise Exception("Knowledge Base creation failed")

        logger.debug(f"  Knowledge Base status: {status} (waiting...)")
        time.sleep(10)

    data_source_id = ensure_data_source(
        bedrock_agent_client, knowledge_base_id, s3_bucket_name
    )
    return knowledge_base_id, data_source_id


def _vpc_name() -> str:
    return f"vpc-for-{project_name}"


def _enable_vpc_dns(vpc_id: str) -> None:
    ec2_client.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2_client.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})


def _subnet_is_public(subnet_id: str) -> bool:
    rts = ec2_client.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    ).get("RouteTables", [])
    if not rts:
        subnet = ec2_client.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        rts = ec2_client.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [subnet["VpcId"]]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        ).get("RouteTables", [])
    for rt in rts:
        for route in rt.get("Routes", []):
            if str(route.get("GatewayId", "")).startswith("igw-"):
                return True
    return False


def _classify_subnets(vpc_id: str) -> tuple[List[str], List[str]]:
    subnets = ec2_client.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    ).get("Subnets", [])
    public_subnets: List[str] = []
    private_subnets: List[str] = []
    for subnet in subnets:
        if subnet.get("State") != "available":
            continue
        name = ""
        for tag in subnet.get("Tags") or []:
            if tag["Key"] == "Name":
                name = tag["Value"]
                break
        sid = subnet["SubnetId"]
        if "private" in name.lower():
            private_subnets.append(sid)
        elif "public" in name.lower():
            public_subnets.append(sid)
        elif _subnet_is_public(sid):
            public_subnets.append(sid)
        else:
            private_subnets.append(sid)
    return public_subnets, private_subnets


def _create_vpc() -> Dict[str, object]:
    azs = [
        z["ZoneName"]
        for z in ec2_client.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )["AvailabilityZones"]
    ][:2]
    if len(azs) < 2:
        raise RuntimeError("Need at least 2 availability zones for Harness VPC")

    vpc_id = ec2_client.create_vpc(
        CidrBlock=VPC_CIDR,
        TagSpecifications=[
            {
                "ResourceType": "vpc",
                "Tags": [{"Key": "Name", "Value": _vpc_name()}],
            }
        ],
    )["Vpc"]["VpcId"]
    ec2_client.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    _enable_vpc_dns(vpc_id)
    logger.info(f"  Created VPC: {vpc_id}")

    igw_id = ec2_client.create_internet_gateway(
        TagSpecifications=[
            {
                "ResourceType": "internet-gateway",
                "Tags": [{"Key": "Name", "Value": f"igw-for-{project_name}"}],
            }
        ]
    )["InternetGateway"]["InternetGatewayId"]
    ec2_client.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    public_rt = ec2_client.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "route-table",
                "Tags": [{"Key": "Name", "Value": f"public-rt-for-{project_name}"}],
            }
        ],
    )["RouteTable"]["RouteTableId"]
    ec2_client.create_route(
        RouteTableId=public_rt,
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=igw_id,
    )

    public_subnets: List[str] = []
    private_subnets: List[str] = []
    for i, az in enumerate(azs):
        pub = ec2_client.create_subnet(
            VpcId=vpc_id,
            CidrBlock=f"10.52.{i}.0/24",
            AvailabilityZone=az,
            TagSpecifications=[
                {
                    "ResourceType": "subnet",
                    "Tags": [
                        {"Key": "Name", "Value": f"public-{i}-for-{project_name}"}
                    ],
                }
            ],
        )["Subnet"]["SubnetId"]
        ec2_client.modify_subnet_attribute(
            SubnetId=pub, MapPublicIpOnLaunch={"Value": True}
        )
        ec2_client.associate_route_table(SubnetId=pub, RouteTableId=public_rt)
        public_subnets.append(pub)

        priv = ec2_client.create_subnet(
            VpcId=vpc_id,
            CidrBlock=f"10.52.{10 + i}.0/24",
            AvailabilityZone=az,
            TagSpecifications=[
                {
                    "ResourceType": "subnet",
                    "Tags": [
                        {"Key": "Name", "Value": f"private-{i}-for-{project_name}"}
                    ],
                }
            ],
        )["Subnet"]["SubnetId"]
        private_subnets.append(priv)

    # One NAT for outbound from private subnets (MCP / Bedrock APIs).
    eip = ec2_client.allocate_address(Domain="vpc")["AllocationId"]
    nat_id = ec2_client.create_nat_gateway(
        SubnetId=public_subnets[0],
        AllocationId=eip,
        TagSpecifications=[
            {
                "ResourceType": "natgateway",
                "Tags": [{"Key": "Name", "Value": f"nat-for-{project_name}"}],
            }
        ],
    )["NatGateway"]["NatGatewayId"]
    logger.info(f"  Waiting for NAT Gateway: {nat_id}")
    ec2_client.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat_id])

    private_rt = ec2_client.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "route-table",
                "Tags": [{"Key": "Name", "Value": f"private-rt-for-{project_name}"}],
            }
        ],
    )["RouteTable"]["RouteTableId"]
    ec2_client.create_route(
        RouteTableId=private_rt,
        DestinationCidrBlock="0.0.0.0/0",
        NatGatewayId=nat_id,
    )
    for subnet_id in private_subnets:
        ec2_client.associate_route_table(SubnetId=subnet_id, RouteTableId=private_rt)

    logger.info(
        f"✓ VPC ready: {vpc_id} "
        f"(public={public_subnets}, private={private_subnets})"
    )
    return {
        "vpc_id": vpc_id,
        "public_subnets": public_subnets,
        "private_subnets": private_subnets,
    }


def ensure_vpc() -> Dict[str, object]:
    """Create or reuse a project VPC with public + private subnets and NAT."""
    logger.info(f"Ensuring VPC for Harness: {_vpc_name()}")
    resp = ec2_client.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [_vpc_name()]}]
    )
    vpcs = resp.get("Vpcs") or []
    if vpcs:
        vpc_id = vpcs[0]["VpcId"]
        logger.info(f"  Reusing VPC: {vpc_id}")
        _enable_vpc_dns(vpc_id)
        public_subnets, private_subnets = _classify_subnets(vpc_id)
        if len(private_subnets) < 1:
            raise RuntimeError(
                f"VPC {vpc_id} has no private subnets; "
                "create private subnets or delete the VPC and re-run installer."
            )
        return {
            "vpc_id": vpc_id,
            "public_subnets": public_subnets,
            "private_subnets": private_subnets,
        }
    return _create_vpc()


def _create_or_get_security_group(
    vpc_id: str, group_name: str, description: str
) -> str:
    try:
        return ec2_client.create_security_group(
            GroupName=group_name,
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "Name", "Value": group_name}],
                }
            ],
        )["GroupId"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        sgs = ec2_client.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [group_name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )
        return sgs["SecurityGroups"][0]["GroupId"]


def prepare_harness_vpc_network(vpc_info: Dict[str, object]) -> Dict[str, object]:
    """Ensure harness runtime SG in the project VPC."""
    vpc_id = str(vpc_info["vpc_id"])
    private_subnets = list(vpc_info.get("private_subnets") or [])
    if not private_subnets:
        raise RuntimeError(
            "At least one private subnet is required for Harness VPC mode"
        )

    group_name = f"harness-runtime-sg-for-{project_name}"
    harness_sg_id = _create_or_get_security_group(
        vpc_id=vpc_id,
        group_name=group_name,
        description=f"Security group for AgentCore Harness ({project_name})",
    )
    try:
        ec2_client.authorize_security_group_egress(
            GroupId=harness_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "-1",
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            logger.debug(f"  harness SG egress: {e}")

    logger.info("✓ Harness VPC network ready")
    logger.info(f"  VPC: {vpc_id}")
    logger.info(f"  Subnets: {', '.join(private_subnets)}")
    logger.info(f"  Security group: {harness_sg_id}")

    return {
        "vpc_id": vpc_id,
        "subnets": private_subnets,
        "security_groups": [harness_sg_id],
    }


def build_harness_runtime_environment(
    vpc_runtime: Optional[Dict[str, object]] = None,
) -> Dict:
    """Build CreateHarness/UpdateHarness environment (VPC network)."""
    lifecycle = {
        "idleRuntimeSessionTimeout": 600,
        "maxLifetime": 14400,
    }
    subnets = list((vpc_runtime or {}).get("subnets") or [])
    security_groups = list((vpc_runtime or {}).get("security_groups") or [])
    if not subnets or not security_groups:
        return {
            "agentCoreRuntimeEnvironment": {
                "lifecycleConfiguration": lifecycle,
                "networkConfiguration": {"networkMode": "PUBLIC"},
            }
        }

    return {
        "agentCoreRuntimeEnvironment": {
            "lifecycleConfiguration": lifecycle,
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {
                    "subnets": subnets,
                    "securityGroups": security_groups,
                },
            },
        }
    }


def create_s3_bucket() -> str:
    """Create S3 bucket with CORS configuration."""
    bucket_name = _bucket_name()
    logger.info(f"[1/9] Creating S3 bucket: {bucket_name}")

    try:
        logger.debug(f"Creating bucket in region: {region}")
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        logger.debug("Bucket created successfully")

        logger.debug("Configuring public access block")
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )

        logger.debug("Setting CORS configuration")
        cors_configuration = {
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "POST", "PUT"],
                    "AllowedOrigins": ["*"],
                }
            ]
        }
        s3_client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration=cors_configuration,
        )

        logger.debug("Creating docs and artifacts folders")
        for folder in ["docs/", "artifacts/"]:
            try:
                s3_client.put_object(Bucket=bucket_name, Key=folder, Body=b"")
                logger.debug(f"{folder} folder created successfully")
            except ClientError as e:
                logger.warning(f"Failed to create {folder} folder: {e}")

        logger.info(f"✓ S3 bucket created successfully: {bucket_name}")
        return bucket_name

    except ClientError as e:
        if e.response["Error"]["Code"] in ["BucketAlreadyExists", "BucketAlreadyOwnedByYou"]:
            logger.warning(f"S3 bucket already exists (reusing shared): {bucket_name}")
            logger.debug("Creating docs and artifacts folders in existing bucket")
            for folder in ["docs/", "artifacts/"]:
                try:
                    s3_client.put_object(Bucket=bucket_name, Key=folder, Body=b"")
                    logger.debug(f"{folder} folder created successfully")
                except ClientError as folder_error:
                    if folder_error.response["Error"]["Code"] != "NoSuchBucket":
                        logger.warning(
                            f"Failed to create {folder} folder: {folder_error}"
                        )
            return bucket_name
        logger.error(f"Failed to create S3 bucket: {e}")
        raise


def _should_skip_skill_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    skip_dirs = {"__pycache__", ".git", ".DS_Store", "node_modules"}
    if any(p in skip_dirs for p in parts):
        return True
    basename = parts[-1] if parts else ""
    if basename.endswith((".pyc", ".pyo", ".DS_Store")):
        return True
    return False


def _list_s3_keys(bucket: str, prefix: str) -> List[str]:
    """List all object keys under prefix (paginated)."""
    keys: List[str] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            key = obj.get("Key")
            if key:
                keys.append(key)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def upload_skills_to_s3(s3_bucket_name: str) -> int:
    """Sync skills/ to s3://{bucket}/skills/ (upload + prune extras)."""
    logger.info(f"[2/9] Syncing skills to s3://{s3_bucket_name}/{SKILLS_S3_PREFIX}/")

    if not os.path.isdir(SKILLS_DIR):
        logger.warning(f"Skills directory not found: {SKILLS_DIR}; skipping sync")
        return 0

    prefix = f"{SKILLS_S3_PREFIX}/"
    local_keys: set[str] = set()
    uploaded = 0
    failed = 0

    for root, dirs, files in os.walk(SKILLS_DIR):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", "node_modules"}]
        for filename in files:
            local_path = os.path.join(root, filename)
            rel_path = os.path.relpath(local_path, SKILLS_DIR)
            if _should_skip_skill_path(rel_path):
                continue
            s3_key = f"{SKILLS_S3_PREFIX}/{rel_path.replace(os.sep, '/')}"
            local_keys.add(s3_key)
            content_type, _ = mimetypes.guess_type(local_path)
            upload_kwargs = {}
            if content_type:
                upload_kwargs["ExtraArgs"] = {"ContentType": content_type}
            try:
                s3_client.upload_file(
                    local_path,
                    s3_bucket_name,
                    s3_key,
                    **upload_kwargs,
                )
                uploaded += 1
                logger.debug(f"  uploaded: s3://{s3_bucket_name}/{s3_key}")
            except ClientError as e:
                failed += 1
                logger.error(f"  failed: {rel_path}: {e}")

    if failed:
        raise RuntimeError(
            f"Skills sync incomplete: {uploaded} ok, {failed} failed "
            f"(from {SKILLS_DIR})"
        )

    deleted = 0
    try:
        remote_keys = _list_s3_keys(s3_bucket_name, prefix)
    except ClientError as e:
        logger.error(f"Failed to list remote skills for prune: {e}")
        raise

    stale = [k for k in remote_keys if k not in local_keys]
    for s3_key in stale:
        try:
            s3_client.delete_object(Bucket=s3_bucket_name, Key=s3_key)
            deleted += 1
            logger.debug(f"  deleted: s3://{s3_bucket_name}/{s3_key}")
        except ClientError as e:
            failed += 1
            logger.error(f"  delete failed: {s3_key}: {e}")

    if failed:
        raise RuntimeError(
            f"Skills sync prune incomplete: {deleted} deleted, {failed} failed "
            f"(from s3://{s3_bucket_name}/{prefix})"
        )

    logger.info(
        f"✓ Synced skills to s3://{s3_bucket_name}/{SKILLS_S3_PREFIX}/ "
        f"({uploaded} uploaded, {deleted} deleted)"
    )
    return uploaded


def create_cloudfront_distribution(s3_bucket_name: str) -> Dict[str, str]:
    """Create CloudFront distribution with S3 origin (shared RAG project)."""
    logger.info("[9/9] Creating CloudFront distribution")
    comment = _cloudfront_comment()
    oai_cmt = _oai_comment()

    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if comment in dist.get("Comment", ""):
                if dist.get("Enabled", False):
                    logger.warning(
                        f"CloudFront distribution already exists (reusing): "
                        f"{dist['DomainName']}"
                    )
                    return {"id": dist["Id"], "domain": dist["DomainName"]}
                logger.warning(
                    f"CloudFront distribution exists but is disabled: {dist['DomainName']}"
                )
                dist_config_response = cloudfront_client.get_distribution_config(
                    Id=dist["Id"]
                )
                dist_config = dist_config_response["DistributionConfig"]
                dist_config["Enabled"] = True
                cloudfront_client.update_distribution(
                    Id=dist["Id"],
                    DistributionConfig=dist_config,
                    IfMatch=dist_config_response["ETag"],
                )
                return {"id": dist["Id"], "domain": dist["DomainName"]}
    except Exception as e:
        logger.debug(f"Error checking existing CloudFront distributions: {e}")

    oai_id = None
    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get(
            "Items", []
        ):
            if oai_cmt in oai.get("Comment", ""):
                oai_id = oai["Id"]
                logger.info(f"  Using existing Origin Access Identity: {oai_id}")
                break
        if not oai_id:
            oai_response = cloudfront_client.create_cloud_front_origin_access_identity(
                CloudFrontOriginAccessIdentityConfig={
                    "CallerReference": (
                        f"{SHARED_STORAGE_NAME}-s3-oai-{int(time.time())}"
                    ),
                    "Comment": oai_cmt,
                }
            )
            oai_id = oai_response["CloudFrontOriginAccessIdentity"]["Id"]
            logger.info(f"  Created Origin Access Identity: {oai_id}")
    except ClientError as e:
        logger.error(f"Failed to handle Origin Access Identity: {e}")
        raise

    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontAccess",
                "Effect": "Allow",
                "Principal": {
                    "AWS": (
                        f"arn:aws:iam::cloudfront:user/"
                        f"CloudFront Origin Access Identity {oai_id}"
                    )
                },
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{s3_bucket_name}/*",
            }
        ],
    }
    try:
        time.sleep(10)
        s3_client.put_bucket_policy(
            Bucket=s3_bucket_name, Policy=json.dumps(bucket_policy)
        )
        logger.info("  Updated S3 bucket policy for CloudFront access")
    except ClientError as e:
        logger.error(f"Failed to update S3 bucket policy: {e}")
        raise

    origin_id = f"s3-{SHARED_STORAGE_NAME}"
    distribution_config = {
        "CallerReference": f"{SHARED_STORAGE_NAME}-{int(time.time())}",
        "Comment": comment,
        "DefaultRootObject": "index.html",
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 2,
                "Items": ["GET", "HEAD"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
            "Compress": True,
        },
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": origin_id,
                    "DomainName": f"{s3_bucket_name}.s3.{region}.amazonaws.com",
                    "S3OriginConfig": {
                        "OriginAccessIdentity": (
                            f"origin-access-identity/cloudfront/{oai_id}"
                        )
                    },
                }
            ],
        },
        "Enabled": True,
        "PriceClass": "PriceClass_200",
    }

    response = cloudfront_client.create_distribution(
        DistributionConfig=distribution_config
    )
    distribution_id = response["Distribution"]["Id"]
    distribution_domain = response["Distribution"]["DomainName"]
    logger.info(f"CloudFront distribution created: {distribution_domain}")
    logger.info(f"  S3 origin: {s3_bucket_name}")
    return {"id": distribution_id, "domain": distribution_domain}


def create_harness_execution_role() -> str:
    """Create IAM execution role for Bedrock AgentCore harness."""
    logger.info("[3/9] Creating Harness execution IAM role")
    role_name = f"role-harness-for-{project_name}-{region}"
    if len(role_name) > 64:
        logger.error(
            f"IAM RoleName exceeds 64 characters ({len(role_name)}): {role_name!r}. "
            "Shorten projectName or region in config."
        )
        sys.exit(1)

    # Trust: bedrock-agentcore.amazonaws.com only (no SourceArn).
    # CreateHarness validates AssumeRole against this shape.
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreAssumeHarness",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    role_arn, role_created = create_iam_role(
        role_name,
        assume_role_policy,
        description="Execution role for Bedrock AgentCore harness",
    )

    harness_execution_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockModelInvocation",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                ],
            },
            {
                "Sid": "KnowledgeBaseRetrieve",
                "Effect": "Allow",
                "Action": [
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:ListIngestionJobs",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*",
                ],
            },
            {
                "Sid": "AgentCoreAccess",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:*"],
                "Resource": ["*"],
            },
            {
                "Sid": "CloudWatchLogsAgentCore",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                ],
            },
            # VPC-mode harness pulls the managed image from AWS ECR
            # (e.g. 796669927364.dkr.ecr.<region>.amazonaws.com/harness-<region>).
            # Without these, InvokeHarness fails with Runtime health check timeout.
            {
                "Sid": "EcrManagedImagePull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": [f"arn:aws:ecr:{region}:*:repository/harness-*"],
            },
            {
                "Sid": "EcrManagedImageToken",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": ["*"],
            },
            {
                "Sid": "AgentCoreSkillS3ListBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{_bucket_name()}"],
            },
            {
                "Sid": "AgentCoreSkillS3GetObject",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{_bucket_name()}/*"],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"harness-exec-inline-for-{role_name}",
        harness_execution_policy,
    )
    # CreateHarness validates AssumeRole immediately after a brand-new role.
    if role_created:
        wait_seconds = 20
        logger.info(
            f"  Waiting {wait_seconds}s for IAM role/policy propagation "
            f"before CreateHarness..."
        )
        time.sleep(wait_seconds)
    logger.info(f"✓ Harness execution role ready: {role_arn}")
    return role_arn


BASE_SYSTEM_PROMPT = (
    "당신의 이름은 서연이고, 질문에 친근한 방식으로 대답하도록 설계된 대화형 AI입니다.\n"
    "상황에 맞는 구체적인 세부 정보를 충분히 제공합니다.\n"
    "모르는 질문을 받으면 솔직히 모른다고 말합니다.\n"
    "한국어로 답변하세요.\n"
    "\n"
    "An agent orchestrates the following workflow:\n"
    "1. Receives user input\n"
    "2. Processes the input using a language model\n"
    "3. Decides whether to use tools to gather information or perform actions\n"
    "4. Executes those tools and receives results\n"
    "5. Continues reasoning with the new information\n"
    "6. Produces a final response\n"
)


def _paginate_list_harnesses() -> List[Dict]:
    items: List[Dict] = []
    token = None
    while True:
        kw: Dict = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = agentcore_control_client.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def find_harness_by_api_name(harness_api_name: str) -> Optional[Dict]:
    for h in _paginate_list_harnesses():
        if h.get("harnessName") == harness_api_name:
            return h
    return None


def wait_for_harness_ready(harness_id: str, timeout_seconds: int = 300) -> str:
    """Poll until harness reaches READY; return harness ARN."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        res = agentcore_control_client.get_harness(harnessId=harness_id)
        h = res["harness"]
        status = h["status"]
        if status == "READY":
            harness_arn = h["arn"]
            logger.info(f"✓ Harness ready: {harness_arn}")
            return harness_arn
        if status in (
            "FAILED",
            "CREATE_FAILED",
            "UPDATE_FAILED",
            "DELETING",
            "DELETE_UNSUCCESSFUL",
            "DELETE_FAILED",
        ):
            reason = h.get("failureReason") or h.get("statusReason") or ""
            raise RuntimeError(
                f"Harness {harness_id} entered terminal status: {status}"
                + (f" — {reason}" if reason else "")
            )
        logger.info(f"  Waiting for harness ({harness_id}) status: {status}")
        time.sleep(5)
    raise TimeoutError(f"Harness {harness_id} did not reach READY within {timeout_seconds}s")


def _harness_environment_variables(
    *,
    s3_bucket_name: Optional[str] = None,
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
) -> Dict[str, str]:
    """Env vars for Harness runtime (S3 bucket + Knowledge Base)."""
    env: Dict[str, str] = {"LOG_LEVEL": "info"}
    bucket = s3_bucket_name or _bucket_name()
    if bucket:
        env["S3_BUCKET"] = bucket
    if knowledge_base_id:
        env["KNOWLEDGE_BASE_ID"] = knowledge_base_id
    if data_source_id:
        env["DATA_SOURCE_ID"] = data_source_id
    return env


def ensure_harness_kb_env(
    harness_id: str,
    s3_bucket_name: str,
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
) -> None:
    """Inject S3_BUCKET / KNOWLEDGE_BASE_ID / DATA_SOURCE_ID into harness env."""
    if not harness_id:
        return
    env_vars = _harness_environment_variables(
        s3_bucket_name=s3_bucket_name,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
    )
    logger.info(
        "  Updating harness environmentVariables: "
        f"S3_BUCKET={s3_bucket_name or '(none)'}, "
        f"KNOWLEDGE_BASE_ID={knowledge_base_id or '(none)'}, "
        f"DATA_SOURCE_ID={data_source_id or '(none)'}"
    )
    agentcore_control_client.update_harness(
        harnessId=harness_id,
        environmentVariables=env_vars,
    )


def ensure_harness_environment(
    harness_id: str,
    environment: Dict,
    *,
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
) -> None:
    """Update harness environment when VPC network config differs from desired."""
    h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
    current_env = h.get("environment") or {}
    desired = environment or {}
    current_rt = current_env.get("agentCoreRuntimeEnvironment") or {}
    desired_rt = desired.get("agentCoreRuntimeEnvironment") or {}

    current_net = current_rt.get("networkConfiguration") or {}
    desired_net = desired_rt.get("networkConfiguration") or {}
    current_mode = current_net.get("networkMode")
    desired_mode = desired_net.get("networkMode")
    current_cfg = current_net.get("networkModeConfig") or {}
    desired_cfg = desired_net.get("networkModeConfig") or {}

    same_vpc = (
        current_mode == desired_mode
        and sorted(current_cfg.get("subnets") or [])
        == sorted(desired_cfg.get("subnets") or [])
        and sorted(current_cfg.get("securityGroups") or [])
        == sorted(desired_cfg.get("securityGroups") or [])
    )
    if same_vpc:
        logger.info(
            f"  Harness environment already matches (networkMode={desired_mode})"
        )
        return

    logger.info(
        f"  Updating harness environment: networkMode {current_mode!r} -> {desired_mode!r}"
    )
    agentcore_control_client.update_harness(
        harnessId=harness_id,
        environment=desired,
        environmentVariables=_harness_environment_variables(
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
        ),
    )


def create_or_get_harness(
    execution_role_arn: str,
    vpc_runtime: Optional[Dict[str, object]] = None,
    *,
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
) -> Dict[str, str]:
    """Create AgentCore Harness or reuse an existing one by API name."""
    logger.info("[5/6] Creating AgentCore Harness")

    harness_api_name = harness_name_for_api(project_name)
    logger.info(f"  harnessName: {harness_api_name} (from projectName={project_name!r})")

    model_id = DEFAULT_MODEL_ID
    system_prompt = [{"text": BASE_SYSTEM_PROMPT}]
    environment = build_harness_runtime_environment(vpc_runtime)
    env_vars = _harness_environment_variables(
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
    )

    existing = find_harness_by_api_name(harness_api_name)
    if existing:
        harness_id = existing["harnessId"]
        try:
            status = agentcore_control_client.get_harness(harnessId=harness_id)[
                "harness"
            ].get("status")
        except ClientError:
            status = None
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED", "DELETE_FAILED"):
            reason = ""
            try:
                reason = (
                    agentcore_control_client.get_harness(harnessId=harness_id)[
                        "harness"
                    ].get("failureReason")
                    or ""
                )
            except ClientError:
                pass
            logger.warning(
                f"Harness {harness_api_name!r} is {status} "
                f"(harnessId={harness_id}); deleting to recreate."
                + (f" reason={reason!r}" if reason else "")
            )
            try:
                agentcore_control_client.delete_harness(
                    harnessId=harness_id,
                    clientToken=str(uuid.uuid4()),
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    agentcore_control_client.get_harness(harnessId=harness_id)
                    time.sleep(5)
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        break
                    raise
            else:
                raise TimeoutError(f"Timed out deleting failed harness {harness_id}")
            existing = None
        else:
            logger.warning(
                f"Harness {harness_api_name!r} already exists (harnessId={harness_id}); "
                "skipping CreateHarness."
            )

    if not existing:
        try:
            response = agentcore_control_client.create_harness(
                harnessName=harness_api_name,
                executionRoleArn=execution_role_arn,
                model={
                    "bedrockModelConfig": {
                        "modelId": model_id,
                        "maxTokens": get_max_output_tokens(model_id),
                    }
                },
                systemPrompt=system_prompt,
                tools=[
                    {
                        "type": "remote_mcp",
                        "name": "exa",
                        "config": {"remoteMcp": {"url": "https://mcp.exa.ai/mcp"}},
                    },
                    {
                        "type": "remote_mcp",
                        "name": "aws_knowledge",
                        "config": {
                            "remoteMcp": {
                                "url": "https://knowledge-mcp.global.api.aws",
                            }
                        },
                    },
                    {
                        "type": "agentcore_browser",
                        "name": "browser",
                        "config": {"agentCoreBrowser": {}},
                    },
                    {
                        "type": "agentcore_code_interpreter",
                        "name": "code",
                        "config": {"agentCoreCodeInterpreter": {}},
                    },
                ],
                truncation={
                    "strategy": "sliding_window",
                    "config": {"slidingWindow": {"messagesCount": 50}},
                },
                maxIterations=20,
                maxTokens=50000,
                timeoutSeconds=300,
                environment=environment,
                environmentVariables=env_vars,
                tags={"Project": project_name, "Env": "dev"},
            )
            harness_id = response["harness"]["harnessId"]
            logger.info(f"  ✓ Harness created: {harness_id}")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConflictException":
                raise
            rerun = find_harness_by_api_name(harness_api_name)
            if not rerun:
                logger.error(
                    "CreateHarness ConflictException but harness not found by name "
                    f"{harness_api_name!r}. Re-run after checking console."
                )
                raise
            harness_id = rerun["harnessId"]
            logger.info(
                f"CreateHarness conflict; using existing harnessId={harness_id} "
                f"({harness_api_name!r})."
            )

    ensure_harness_environment(
        harness_id,
        environment,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
    )
    harness_arn = wait_for_harness_ready(harness_id)

    return {
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "harness_name": harness_api_name,
    }


def write_config(config_path: str, config_data: Dict, *, merge_existing: bool = True) -> bool:
    """Write config JSON, optionally merging with existing contents."""
    existing: Dict = {}
    if merge_existing:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not read existing {config_path}: {e}")

    existing.update({k: v for k, v in config_data.items() if v is not None})
    for k, v in config_data.items():
        if v is None:
            existing.pop(k, None)
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Could not write {config_path}: {e}")
        return False


def build_config_from_deployment_state(
    execution_role_arn: Optional[str] = None,
    harness_info: Optional[Dict[str, str]] = None,
    s3_bucket_name: Optional[str] = None,
    cloudfront_info: Optional[Dict[str, str]] = None,
    vpc_info: Optional[Dict[str, object]] = None,
    vpc_runtime: Optional[Dict[str, object]] = None,
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    knowledge_base_role_arn: Optional[str] = None,
    s3_vectors_info: Optional[Dict[str, str]] = None,
) -> Dict:
    config_data: Dict = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
    }
    if execution_role_arn:
        config_data["executionRoleArn"] = execution_role_arn
    if harness_info:
        if harness_info.get("harness_id"):
            config_data["harnessId"] = harness_info["harness_id"]
        if harness_info.get("harness_arn"):
            config_data["HARNESS_ARN"] = harness_info["harness_arn"]
    if s3_bucket_name:
        config_data["s3_bucket"] = s3_bucket_name
        config_data["s3_arn"] = f"arn:aws:s3:::{s3_bucket_name}"
    if cloudfront_info:
        config_data["sharing_url"] = f"https://{cloudfront_info.get('domain', '')}"
    if vpc_info:
        config_data["vpc_id"] = vpc_info.get("vpc_id", "")
    if vpc_runtime:
        config_data["agent_runtime_vpc_subnets"] = vpc_runtime.get("subnets", [])
        config_data["agent_runtime_security_groups"] = vpc_runtime.get(
            "security_groups", []
        )
    if knowledge_base_id:
        config_data["knowledge_base_id"] = knowledge_base_id
    if data_source_id:
        config_data["data_source_id"] = data_source_id
    if knowledge_base_role_arn:
        config_data["knowledge_base_role"] = knowledge_base_role_arn
    if s3_vectors_info:
        config_data["vector_bucket_name"] = s3_vectors_info.get("vectorBucketName", "")
        config_data["vector_bucket_arn"] = s3_vectors_info.get("vectorBucketArn", "")
        config_data["vector_index_name"] = s3_vectors_info.get("indexName", "")
        config_data["vector_index_arn"] = s3_vectors_info.get("indexArn", "")
    # Remove legacy Memory / S3 Files keys on write
    for legacy in (
        "agentcore_memory_role",
        "agent_memory_arn",
        "memory_id",
        "memoryId",
        "s3_files_file_system_id",
        "s3_files_access_point_arn",
        "s3_files_mount_path",
    ):
        config_data[legacy] = None
    return config_data


def main():
    global project_name, region, account_id

    logger.info("=" * 60)
    logger.info("Starting AgentCore Harness Infrastructure Deployment")
    logger.info("=" * 60)

    load_config(CONFIG_PATH)

    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Bucket Name: {_bucket_name()}")
    logger.info(f"Vector Bucket: {vector_bucket_name}")
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info("=" * 60)

    start_time = time.time()
    s3_bucket_name = None
    knowledge_base_role_arn = None
    s3_vectors_info = None
    knowledge_base_id = None
    data_source_id = None
    execution_role_arn = None
    vpc_info = None
    vpc_runtime = None
    harness_info = None
    cloudfront_info = None
    deployment_success = False

    try:
        s3_bucket_name = create_s3_bucket()
        upload_skills_to_s3(s3_bucket_name)

        knowledge_base_role_arn = create_knowledge_base_role()
        s3_vectors_info = create_s3_vectors_store()
        knowledge_base_id, data_source_id = create_knowledge_base_with_s3_vectors(
            s3_vectors_info, knowledge_base_role_arn, s3_bucket_name
        )

        execution_role_arn = create_harness_execution_role()

        logger.info("[4/6] Ensuring VPC for Harness")
        vpc_info = ensure_vpc()
        logger.info("[5/6] Preparing Harness VPC network (security group)")
        vpc_runtime = prepare_harness_vpc_network(vpc_info)

        harness_info = create_or_get_harness(
            execution_role_arn,
            vpc_runtime=vpc_runtime,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
        )
        cloudfront_info = create_cloudfront_distribution(s3_bucket_name)
        ensure_harness_kb_env(
            harness_info["harness_id"],
            s3_bucket_name,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
        )
        deployment_success = True

        elapsed_time = time.time() - start_time
        logger.info("")
        logger.info("=" * 60)
        logger.info("Infrastructure Deployment Completed Successfully!")
        logger.info("=" * 60)
        logger.info(f"  S3 Bucket: {s3_bucket_name}")
        logger.info(f"  CloudFront Domain: https://{cloudfront_info['domain']}")
        logger.info(f"  Knowledge Base ID: {knowledge_base_id}")
        logger.info(f"  Data Source ID: {data_source_id}")
        logger.info(f"  Knowledge Base Role: {knowledge_base_role_arn}")
        logger.info(f"  Vector Bucket: {s3_vectors_info.get('vectorBucketName')}")
        logger.info(f"  Vector Index: {s3_vectors_info.get('indexName')}")
        logger.info(f"  VPC: {vpc_info.get('vpc_id')}")
        logger.info(f"  Subnets: {vpc_runtime.get('subnets')}")
        logger.info(f"  Security groups: {vpc_runtime.get('security_groups')}")
        logger.info(f"  Execution Role: {execution_role_arn}")
        logger.info(f"  Harness ID: {harness_info['harness_id']}")
        logger.info(f"  Harness ARN: {harness_info['harness_arn']}")
        logger.info(f"Total deployment time: {elapsed_time / 60:.2f} minutes")
        logger.info("=" * 60)
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Deployment Failed after {elapsed_time / 60:.2f} minutes: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise
    finally:
        config_data = build_config_from_deployment_state(
            execution_role_arn=execution_role_arn,
            harness_info=harness_info,
            s3_bucket_name=s3_bucket_name,
            cloudfront_info=cloudfront_info,
            vpc_info=vpc_info,
            vpc_runtime=vpc_runtime,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
            knowledge_base_role_arn=knowledge_base_role_arn,
            s3_vectors_info=s3_vectors_info,
        )
        if write_config(CONFIG_PATH, config_data):
            if deployment_success:
                logger.info(f"Updated {CONFIG_PATH}")
            else:
                logger.info(f"Saved partial deployment info to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
