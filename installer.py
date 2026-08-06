#!/usr/bin/env python3
"""
AWS Infrastructure Installer using boto3
This script provisions AgentCore Harness (S3, skills, VPC, CloudFront,
IAM roles, CreateHarness) for local development.
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
from typing import Dict, List, Optional
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

iam_client = boto3.client("iam", region_name=region)
s3_client = boto3.client("s3", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=region,
)

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "application", "config.json")
SKILLS_DIR = os.path.join(WORKING_DIR, "skills")
SKILLS_S3_PREFIX = "skills"


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
    global agentcore_control_client, s3_client, cloudfront_client, ec2_client

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

    agentcore_control_client = boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
    )
    s3_client = boto3.client("s3", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    cloudfront_client = boto3.client("cloudfront", region_name=region)
    return config


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


def upload_skills_to_s3(s3_bucket_name: str) -> int:
    """Upload skills/ to s3://{bucket}/skills/ (AgentCore S3 skill layout)."""
    logger.info(f"[2/9] Uploading skills to s3://{s3_bucket_name}/{SKILLS_S3_PREFIX}/")

    if not os.path.isdir(SKILLS_DIR):
        logger.warning(f"Skills directory not found: {SKILLS_DIR}; skipping upload")
        return 0

    prefix = f"{SKILLS_S3_PREFIX}/"
    try:
        existing = s3_client.list_objects_v2(
            Bucket=s3_bucket_name,
            Prefix=prefix,
            MaxKeys=1,
        )
        if existing.get("KeyCount", 0) > 0 or existing.get("Contents"):
            logger.warning(
                f"Skills already exist at s3://{s3_bucket_name}/{prefix}; skipping upload"
            )
            return 0
    except ClientError as e:
        logger.error(f"Failed to check existing skills prefix: {e}")
        raise

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
            f"Skills upload incomplete: {uploaded} ok, {failed} failed "
            f"(from {SKILLS_DIR})"
        )

    logger.info(
        f"✓ Uploaded {uploaded} skill file(s) to "
        f"s3://{s3_bucket_name}/{SKILLS_S3_PREFIX}/"
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


def ensure_harness_environment(
    harness_id: str,
    environment: Dict,
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
        environmentVariables={"LOG_LEVEL": "info"},
    )


def create_or_get_harness(
    execution_role_arn: str,
    vpc_runtime: Optional[Dict[str, object]] = None,
) -> Dict[str, str]:
    """Create AgentCore Harness or reuse an existing one by API name."""
    logger.info("[5/6] Creating AgentCore Harness")

    harness_api_name = harness_name_for_api(project_name)
    logger.info(f"  harnessName: {harness_api_name} (from projectName={project_name!r})")

    model_id = DEFAULT_MODEL_ID
    system_prompt = [{"text": BASE_SYSTEM_PROMPT}]
    environment = build_harness_runtime_environment(vpc_runtime)

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
                environmentVariables={"LOG_LEVEL": "info"},
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

    ensure_harness_environment(harness_id, environment)
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
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info("=" * 60)

    start_time = time.time()
    s3_bucket_name = None
    execution_role_arn = None
    vpc_info = None
    vpc_runtime = None
    harness_info = None
    cloudfront_info = None
    deployment_success = False

    try:
        s3_bucket_name = create_s3_bucket()
        upload_skills_to_s3(s3_bucket_name)
        execution_role_arn = create_harness_execution_role()

        logger.info("[4/6] Ensuring VPC for Harness")
        vpc_info = ensure_vpc()
        logger.info("[5/6] Preparing Harness VPC network (security group)")
        vpc_runtime = prepare_harness_vpc_network(vpc_info)

        harness_info = create_or_get_harness(
            execution_role_arn,
            vpc_runtime=vpc_runtime,
        )
        cloudfront_info = create_cloudfront_distribution(s3_bucket_name)
        deployment_success = True

        elapsed_time = time.time() - start_time
        logger.info("")
        logger.info("=" * 60)
        logger.info("Infrastructure Deployment Completed Successfully!")
        logger.info("=" * 60)
        logger.info(f"  S3 Bucket: {s3_bucket_name}")
        logger.info(f"  CloudFront Domain: https://{cloudfront_info['domain']}")
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
        )
        if write_config(CONFIG_PATH, config_data):
            if deployment_success:
                logger.info(f"Updated {CONFIG_PATH}")
            else:
                logger.info(f"Saved partial deployment info to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
