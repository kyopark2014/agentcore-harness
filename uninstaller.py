#!/usr/bin/env python3
"""
AWS Infrastructure Uninstaller for agentcore-harness.

Deletes resources created by installer.py:
  Harness, VPC/NAT, CloudFront, S3, IAM roles.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# Configuration (must match installer.py)
project_name = "agentcore-harness"
region = "us-west-2"

# Shared S3 / CloudFront with agent-skills (rag-project) — retain by default.
SHARED_STORAGE_NAME = "rag-project"
cloudfront_comment = f"CloudFront-for-{SHARED_STORAGE_NAME}"
oai_comment = f"OAI for {SHARED_STORAGE_NAME}"

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "application", "config.json")

DELETE_WAIT_TIMEOUT_SEC = int(os.environ.get("AGENTCORE_DELETE_WAIT_TIMEOUT_SEC", "600"))
DELETE_POLL_INTERVAL_SEC = float(os.environ.get("AGENTCORE_DELETE_POLL_INTERVAL_SEC", "5"))

sts_client = boto3.client("sts", region_name=region)
account_id = str(sts_client.get_caller_identity()["Account"])

s3_client = boto3.client("s3", region_name=region)
iam_client = boto3.client("iam", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=region,
)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


logger = setup_logging()


def _bucket_name() -> str:
    return f"storage-for-{SHARED_STORAGE_NAME}-{account_id}-{region}"


def _cloudfront_comment() -> str:
    return cloudfront_comment


def _oai_comment() -> str:
    return oai_comment


def _vpc_name() -> str:
    return f"vpc-for-{project_name}"


def load_config() -> Dict:
    global project_name, region, account_id
    global s3_client, iam_client, ec2_client
    global cloudfront_client, agentcore_control_client

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.warning(f"Could not load {CONFIG_PATH}: {e}")
        cfg = {}

    project_name = cfg.get("projectName") or project_name
    region = cfg.get("region") or region
    raw = cfg.get("accountId")
    if raw is not None and str(raw).strip():
        account_id = str(raw).strip()
    else:
        account_id = str(sts_client.get_caller_identity()["Account"])

    s3_client = boto3.client("s3", region_name=region)
    iam_client = boto3.client("iam", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    cloudfront_client = boto3.client("cloudfront", region_name=region)
    agentcore_control_client = boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
    )
    return cfg


def prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


# --- CloudFront --------------------------------------------------------------

def _matches_cloudfront(dist: dict) -> bool:
    return _cloudfront_comment() in dist.get("Comment", "")


def disable_cloudfront_distributions():
    logger.info("[1/8] Disabling CloudFront distributions")
    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if not _matches_cloudfront(dist):
                continue
            if not dist.get("Enabled", True):
                logger.info(f"  Already disabled: {dist['Id']}")
                continue
            dist_id = dist["Id"]
            logger.info(f"  Disabling: {dist_id}")
            cfg_resp = cloudfront_client.get_distribution_config(Id=dist_id)
            cfg = cfg_resp["DistributionConfig"]
            cfg["Enabled"] = False
            cloudfront_client.update_distribution(
                Id=dist_id,
                DistributionConfig=cfg,
                IfMatch=cfg_resp["ETag"],
            )
        logger.info("✓ CloudFront disable requested")
    except Exception as e:
        logger.error(f"Error disabling CloudFront: {e}")


def wait_for_cloudfront_disabled(max_wait: int = 900, poll_interval: int = 30) -> bool:
    logger.info("  Waiting for CloudFront to become disabled...")
    waited = 0
    while waited < max_wait:
        still = []
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if _matches_cloudfront(dist) and dist.get("Enabled", True):
                still.append(dist["Id"])
        if not still:
            logger.info("  ✓ Matching CloudFront distributions disabled")
            return True
        logger.info(f"  Still enabled: {still} ({waited}s/{max_wait}s)")
        time.sleep(poll_interval)
        waited += poll_interval
    logger.warning("  Timed out waiting for CloudFront disable")
    return False


def delete_cloudfront_distributions():
    logger.info("[7/8] Deleting CloudFront distributions")
    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if not _matches_cloudfront(dist):
                continue
            if dist.get("Enabled", True):
                logger.info(f"  Skipping enabled distribution: {dist['Id']}")
                continue
            dist_id = dist["Id"]
            try:
                cfg_resp = cloudfront_client.get_distribution_config(Id=dist_id)
                cloudfront_client.delete_distribution(
                    Id=dist_id, IfMatch=cfg_resp["ETag"]
                )
                logger.info(f"  ✓ Deleted distribution: {dist_id}")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in {"DistributionNotDisabled", "NoSuchDistribution"}:
                    logger.info(f"  Skip {dist_id}: {code}")
                else:
                    logger.warning(f"  Could not delete {dist_id}: {e}")
        logger.info("✓ CloudFront distributions processed")
    except Exception as e:
        logger.error(f"Error deleting CloudFront: {e}")


def delete_cloudfront_oai():
    logger.info("  Deleting CloudFront Origin Access Identities")
    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get(
            "Items", []
        ):
            if _oai_comment() not in oai.get("Comment", ""):
                continue
            oai_id = oai["Id"]
            try:
                cfg = cloudfront_client.get_cloud_front_origin_access_identity_config(
                    Id=oai_id
                )
                cloudfront_client.delete_cloud_front_origin_access_identity(
                    Id=oai_id, IfMatch=cfg["ETag"]
                )
                logger.info(f"  ✓ Deleted OAI: {oai_id}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchCloudFrontOriginAccessIdentity":
                    logger.warning(f"  Could not delete OAI {oai_id}: {e}")
    except Exception as e:
        logger.warning(f"  Error deleting OAI: {e}")


# --- Harness / Memory --------------------------------------------------------

def _paginate_list_harnesses() -> list:
    items = []
    token = None
    while True:
        kw = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = agentcore_control_client.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def harness_name_for_api(name: str) -> str:
    """Same as installer: projectName → harnessName ('-' → '_')."""
    return (name or "").replace("-", "_")


def resolve_harness_id(cfg: dict) -> Optional[str]:
    if cfg.get("harnessId"):
        return cfg["harnessId"]
    arn = cfg.get("HARNESS_ARN") or ""
    if "harness/" in arn:
        return arn.split("harness/", 1)[-1].strip()

    # Fallback: match CreateHarness harnessName (installer harness_name_for_api)
    api_name = harness_name_for_api(cfg.get("projectName") or project_name)
    for h in _paginate_list_harnesses():
        if h.get("harnessName") == api_name:
            return h.get("harnessId")
    return None



def delete_harness(harness_id: str) -> bool:
    logger.info(f"[2/8] Deleting Harness: {harness_id}")
    try:
        agentcore_control_client.delete_harness(
            harnessId=harness_id,
            clientToken=str(uuid.uuid4()),
        )
        logger.info(f"  DeleteHarness accepted: {harness_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(f"  Harness already gone: {harness_id}")
            return True
        logger.error(f"  DeleteHarness failed: {e}")
        return False

    deadline = time.monotonic() + DELETE_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
            status = h.get("status")
            if status == "DELETE_FAILED":
                logger.error(
                    f"  Harness DELETE_FAILED: {h.get('failureReason')!r}"
                )
                return False
            logger.info(f"  Waiting… status={status!r}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(f"✓ Harness deleted: {harness_id}")
                return True
            raise
        time.sleep(DELETE_POLL_INTERVAL_SEC)
    logger.error("  Timed out waiting for harness deletion")
    return False








def _resolve_vpc_id(cfg: dict) -> Optional[str]:
    if cfg.get("vpc_id"):
        return cfg["vpc_id"]
    resp = ec2_client.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [_vpc_name()]}]
    )
    vpcs = resp.get("Vpcs") or []
    return vpcs[0]["VpcId"] if vpcs else None


def delete_vpc(cfg: dict):
    logger.info("[4/8] Deleting VPC resources")
    vpc_id = _resolve_vpc_id(cfg)
    if not vpc_id:
        logger.info("  No project VPC found")
        return

    logger.info(f"  VPC: {vpc_id}")

    # Detach/delete ENIs that are available
    try:
        enis = ec2_client.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces", [])
        for eni in enis:
            if eni.get("Status") == "available":
                try:
                    ec2_client.delete_network_interface(
                        NetworkInterfaceId=eni["NetworkInterfaceId"]
                    )
                    logger.info(f"  ✓ Deleted ENI: {eni['NetworkInterfaceId']}")
                except ClientError as e:
                    logger.warning(f"  Could not delete ENI: {e}")
    except ClientError as e:
        logger.warning(f"  ENI cleanup: {e}")

    # NAT gateways + routes — capture EIP allocation IDs before delete
    nat_ids = []
    eip_alloc_ids = set()
    try:
        nats = ec2_client.describe_nat_gateways(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NatGateways", [])
        for nat in nats:
            for addr in nat.get("NatGatewayAddresses", []):
                if addr.get("AllocationId"):
                    eip_alloc_ids.add(addr["AllocationId"])
            if nat["State"] in {"deleted", "deleting"}:
                continue
            nat_id = nat["NatGatewayId"]
            rts = ec2_client.describe_route_tables(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("RouteTables", [])
            for rt in rts:
                for route in rt.get("Routes", []):
                    if route.get("NatGatewayId") == nat_id:
                        try:
                            ec2_client.delete_route(
                                RouteTableId=rt["RouteTableId"],
                                DestinationCidrBlock=route["DestinationCidrBlock"],
                            )
                        except ClientError:
                            pass
            try:
                ec2_client.delete_nat_gateway(NatGatewayId=nat_id)
                nat_ids.append(nat_id)
                logger.info(f"  ✓ Delete NAT requested: {nat_id}")
            except ClientError as e:
                logger.warning(f"  Could not delete NAT {nat_id}: {e}")
    except ClientError as e:
        logger.warning(f"  NAT cleanup: {e}")

    if nat_ids:
        logger.info("  Waiting for NAT gateways to delete...")
        deadline = time.time() + 600
        while time.time() < deadline:
            remaining = []
            for nid in nat_ids:
                st = ec2_client.describe_nat_gateways(NatGatewayIds=[nid])[
                    "NatGateways"
                ][0]["State"]
                if st != "deleted":
                    remaining.append(nid)
            if not remaining:
                break
            time.sleep(15)

    for alloc_id in eip_alloc_ids:
        try:
            ec2_client.release_address(AllocationId=alloc_id)
            logger.info(f"  ✓ Released EIP: {alloc_id}")
        except ClientError as e:
            logger.debug(f"  EIP release {alloc_id}: {e}")

    # Detach/delete IGWs
    try:
        igws = ec2_client.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        ).get("InternetGateways", [])
        for igw in igws:
            igw_id = igw["InternetGatewayId"]
            try:
                ec2_client.detach_internet_gateway(
                    InternetGatewayId=igw_id, VpcId=vpc_id
                )
            except ClientError:
                pass
            try:
                ec2_client.delete_internet_gateway(InternetGatewayId=igw_id)
                logger.info(f"  ✓ Deleted IGW: {igw_id}")
            except ClientError as e:
                logger.warning(f"  Could not delete IGW {igw_id}: {e}")
    except ClientError as e:
        logger.warning(f"  IGW cleanup: {e}")

    # Subnets
    try:
        subnets = ec2_client.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets", [])
        for sn in subnets:
            try:
                ec2_client.delete_subnet(SubnetId=sn["SubnetId"])
                logger.info(f"  ✓ Deleted subnet: {sn['SubnetId']}")
            except ClientError as e:
                logger.warning(f"  Could not delete subnet {sn['SubnetId']}: {e}")
    except ClientError as e:
        logger.warning(f"  Subnet cleanup: {e}")

    # Route tables (non-main)
    try:
        rts = ec2_client.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("RouteTables", [])
        for rt in rts:
            is_main = any(a.get("Main") for a in rt.get("Associations", []))
            if is_main:
                continue
            for assoc in rt.get("Associations", []):
                if assoc.get("RouteTableAssociationId") and not assoc.get("Main"):
                    try:
                        ec2_client.disassociate_route_table(
                            AssociationId=assoc["RouteTableAssociationId"]
                        )
                    except ClientError:
                        pass
            try:
                ec2_client.delete_route_table(RouteTableId=rt["RouteTableId"])
                logger.info(f"  ✓ Deleted route table: {rt['RouteTableId']}")
            except ClientError as e:
                logger.warning(f"  Could not delete RT {rt['RouteTableId']}: {e}")
    except ClientError as e:
        logger.warning(f"  Route table cleanup: {e}")

    # Security groups (non-default) — revoke cross refs then delete
    try:
        sgs = ec2_client.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("SecurityGroups", [])
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            try:
                if sg.get("IpPermissions"):
                    ec2_client.revoke_security_group_ingress(
                        GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"]
                    )
                if sg.get("IpPermissionsEgress"):
                    ec2_client.revoke_security_group_egress(
                        GroupId=sg["GroupId"],
                        IpPermissions=sg["IpPermissionsEgress"],
                    )
            except ClientError:
                pass
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            try:
                ec2_client.delete_security_group(GroupId=sg["GroupId"])
                logger.info(f"  ✓ Deleted SG: {sg['GroupId']} ({sg.get('GroupName')})")
            except ClientError as e:
                logger.warning(f"  Could not delete SG {sg['GroupId']}: {e}")
    except ClientError as e:
        logger.warning(f"  SG cleanup: {e}")

    try:
        ec2_client.delete_vpc(VpcId=vpc_id)
        logger.info(f"✓ Deleted VPC: {vpc_id}")
    except ClientError as e:
        logger.warning(f"  Could not delete VPC {vpc_id}: {e}")


# --- S3 / IAM / config -------------------------------------------------------

def _empty_s3_bucket(bucket: str):
    delete_keys = []
    try:
        paginator = s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Versions", []):
                delete_keys.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            for obj in page.get("DeleteMarkers", []):
                delete_keys.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
    except ClientError:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                delete_keys.append({"Key": obj["Key"]})

    if not delete_keys:
        return
    for i in range(0, len(delete_keys), 1000):
        batch = delete_keys[i : i + 1000]
        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
    logger.info(f"  ✓ Emptied {len(delete_keys)} object(s) from {bucket}")


def delete_s3_bucket():
    logger.info("[6/8] Deleting S3 bucket")
    bucket = _bucket_name()
    try:
        s3_client.head_bucket(Bucket=bucket)
    except ClientError:
        logger.info(f"  Bucket not found: {bucket}")
        return
    try:
        try:
            s3_client.delete_bucket_policy(Bucket=bucket)
        except ClientError:
            pass
        _empty_s3_bucket(bucket)
        s3_client.delete_bucket(Bucket=bucket)
        logger.info(f"✓ Deleted S3 bucket: {bucket}")
    except ClientError as e:
        logger.warning(f"  Could not delete bucket {bucket}: {e}")


def delete_iam_role(role_name: str):
    try:
        for p in iam_client.list_attached_role_policies(RoleName=role_name).get(
            "AttachedPolicies", []
        ):
            iam_client.detach_role_policy(
                RoleName=role_name, PolicyArn=p["PolicyArn"]
            )
        for pname in iam_client.list_role_policies(RoleName=role_name).get(
            "PolicyNames", []
        ):
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=pname)
        iam_client.delete_role(RoleName=role_name)
        logger.info(f"  ✓ Deleted IAM role: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            logger.info(f"  IAM role not found: {role_name}")
        else:
            logger.warning(f"  Could not delete role {role_name}: {e}")


def delete_iam_roles():
    logger.info("  Deleting IAM roles")
    harness_role = f"role-harness-for-{project_name}-{region}"
    delete_iam_role(harness_role)
    # Best-effort cleanup of legacy roles from older installer versions
    delete_iam_role(f"role-agentcore-memory-for-{project_name}-{region}")
    delete_iam_role(f"role-s3files-sync-for-{project_name}")
    logger.info("✓ IAM roles processed")


INSTALLER_CONFIG_KEYS = [
    "executionRoleArn",
    "agentcore_memory_role",
    "agent_memory_arn",
    "memory_id",
    "memoryId",
    "harnessId",
    "HARNESS_ARN",
    "s3_bucket",
    "s3_arn",
    "sharing_url",
    "vpc_id",
    "s3_files_file_system_id",
    "s3_files_access_point_arn",
    "s3_files_mount_path",
    "agent_runtime_vpc_subnets",
    "agent_runtime_security_groups",
]


def clear_config_json():
    logger.info("[8/8] Clearing installer fields from config.json")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.warning(f"  Could not read config: {e}")
        return

    for key in INSTALLER_CONFIG_KEYS:
        cfg.pop(key, None)

    # Keep projectName / region / accountId for reinstall
    cfg["projectName"] = project_name
    cfg["region"] = region
    cfg["accountId"] = account_id

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"✓ Updated {CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"  Could not write config: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Uninstall AgentCore Harness infrastructure from installer.py"
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompts",
    )
    parser.add_argument(
        "--delete-s3-bucket",
        action="store_true",
        help="Also delete the shared S3 bucket (default: retain)",
    )
    parser.add_argument(
        "--delete-cloudfront",
        action="store_true",
        help="Also delete the shared CloudFront / OAI (default: retain)",
    )
    args = parser.parse_args()

    cfg = load_config()

    logger.info("=" * 60)
    logger.info("Starting AgentCore Harness Infrastructure Uninstall")
    logger.info("=" * 60)
    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Shared Bucket: {_bucket_name()}")
    logger.info(f"Shared CloudFront: {_cloudfront_comment()}")
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info("=" * 60)

    if not args.yes:
        if not prompt_yes_no(
            "Delete installer-managed harness resources for this project?",
            default=False,
        ):
            logger.info("Aborted.")
            sys.exit(0)
        delete_s3_bucket_flag = prompt_yes_no(
            f"Delete shared S3 bucket ({_bucket_name()})?",
            default=False,
        )
        delete_cloudfront_flag = prompt_yes_no(
            f"Delete shared CloudFront ({_cloudfront_comment()})?",
            default=False,
        )
    else:
        delete_s3_bucket_flag = args.delete_s3_bucket
        delete_cloudfront_flag = args.delete_cloudfront

    start = time.time()
    try:
        if delete_cloudfront_flag:
            disable_cloudfront_distributions()

        harness_id = resolve_harness_id(cfg)
        if harness_id:
            if not delete_harness(harness_id):
                logger.warning(
                    "Harness delete incomplete; continuing with remaining cleanup"
                )
        else:
            logger.info("[2/8] No harness id found; skipping DeleteHarness")

        delete_vpc(cfg)

        delete_iam_roles()

        if delete_s3_bucket_flag:
            delete_s3_bucket()
        else:
            logger.info(
                f"[6/8] S3 bucket retained (shared resource): {_bucket_name()}"
            )

        if delete_cloudfront_flag:
            wait_for_cloudfront_disabled(max_wait=600, poll_interval=20)
            delete_cloudfront_distributions()
            delete_cloudfront_oai()
        else:
            logger.info(
                f"[7/8] CloudFront retained (shared resource): {_cloudfront_comment()}"
            )

        clear_config_json()

        elapsed = time.time() - start
        logger.info("")
        logger.info("=" * 60)
        logger.info("Uninstall completed")
        logger.info(f"Total time: {elapsed / 60:.2f} minutes")
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"Uninstall failed: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
