#!/usr/bin/env python3
"""
Remove AWS resources provisioned by deployment/create_harness.py.

Order: DeleteHarness → DeleteMemory → IAM roles (uninstaller.delete_iam_roles-style) → prune config.json.
"""

import json
import logging
import os
import sys
import time
import uuid
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("utils")

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "config.json")

# Keys written by create_harness.py
HARNESS_CONFIG_KEYS = (
    "HARNESS_ARN",
    "harnessId",
    "executionRoleArn",
    "agentcore_memory_role",
    "agent_memory_arn",
    "memoryId",
)

IAM_ROLE_NAME_MAX = 64

# Poll after DeleteHarness / DeleteMemory until the resource is gone (or terminal failure).
DELETE_WAIT_TIMEOUT_SEC = int(os.environ.get("AGENTCORE_DELETE_WAIT_TIMEOUT_SEC", "600"))
DELETE_POLL_INTERVAL_SEC = float(os.environ.get("AGENTCORE_DELETE_POLL_INTERVAL_SEC", "5"))


def load_config(config_path: str) -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config not found: {config_path}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in {config_path}: {e}")
        return {}


def _harness_id_from_arn(harness_arn: str) -> Optional[str]:
    if not harness_arn or "harness/" not in harness_arn:
        return None
    return harness_arn.split("harness/", 1)[-1].strip()


def _memory_id_from_arn(memory_arn: str) -> Optional[str]:
    if not memory_arn:
        return None
    for marker in ("memory/", ":memory/", "/memory/"):
        if marker in memory_arn:
            return memory_arn.split(marker, 1)[-1].strip()
    return memory_arn.split("/")[-1].strip()


def _paginate_list_harnesses(control) -> list:
    items = []
    token = None
    while True:
        kw = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = control.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def _paginate_list_memories(control) -> list:
    items = []
    token = None
    while True:
        kw = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = control.list_memories(**kw)
        items.extend(resp.get("memories") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def resolve_harness_id(control, cfg: dict, project_name: str) -> Optional[str]:
    if cfg.get("harnessId"):
        return cfg["harnessId"]
    arn = cfg.get("HARNESS_ARN")
    hid = _harness_id_from_arn(arn) if arn else None
    if hid:
        return hid

    logger.info(
        "No harnessId/HARNESS_ARN in config; listing harnesses to match harnessName..."
    )
    name_candidates = {project_name.replace("-", "_"), project_name}
    for h in _paginate_list_harnesses(control):
        if h.get("harnessName") in name_candidates:
            hid = h.get("harnessId")
            logger.info(
                f"Matched harness by harnessName {h.get('harnessName')!r}: {hid}"
            )
            return hid
    return None


def resolve_memory_id(control, cfg: dict, project_name: str) -> Optional[str]:
    if cfg.get("memoryId"):
        return cfg["memoryId"]
    arn = cfg.get("agent_memory_arn")
    mid = _memory_id_from_arn(arn) if arn else None
    if mid:
        return mid

    memory_token = project_name.replace("-", "_")
    logger.info(
        f"No agent_memory_arn in config; listing memories for id prefix={memory_token!r}..."
    )
    for m in _paginate_list_memories(control):
        mem_id = m.get("id") or ""
        if mem_id.split("-")[0] == memory_token:
            logger.info(f"Matched memory: {mem_id}")
            return mem_id
    return None


def delete_harness_resource(control, harness_id: str) -> bool:
    try:
        control.delete_harness(
            harnessId=harness_id,
            clientToken=str(uuid.uuid4()),
        )
        logger.info(f"DeleteHarness accepted: {harness_id}")
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            logger.info(f"Harness already gone: {harness_id}")
            return True
        logger.error(f"DeleteHarness failed: {e}")
        return False


def wait_until_harness_deleted(control, harness_id: str) -> bool:
    """Poll get_harness until ResourceNotFoundException or timeout."""
    deadline = time.monotonic() + DELETE_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            h = control.get_harness(harnessId=harness_id)["harness"]
            status = h.get("status")
            if status == "DELETE_FAILED":
                reason = h.get("failureReason")
                logger.error(
                    f"Harness deletion failed: harnessId={harness_id}, "
                    f"failureReason={reason!r}"
                )
                return False
            logger.info(
                f"Harness delete in progress: harnessId={harness_id}, status={status!r}"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(f"Harness delete complete (not found): {harness_id}")
                return True
            raise
        time.sleep(DELETE_POLL_INTERVAL_SEC)
    logger.error(
        f"Timeout waiting for harness deletion after {DELETE_WAIT_TIMEOUT_SEC}s: {harness_id}"
    )
    return False


def delete_memory_resource(control, memory_id: str) -> bool:
    try:
        control.delete_memory(
            memoryId=memory_id,
            clientToken=str(uuid.uuid4()),
        )
        logger.info(f"DeleteMemory accepted: {memory_id}")
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            logger.info(f"Memory already gone: {memory_id}")
            return True
        logger.error(f"DeleteMemory failed: {e}")
        return False


def wait_until_memory_deleted(control, memory_id: str) -> bool:
    """Poll get_memory until ResourceNotFoundException or timeout."""
    deadline = time.monotonic() + DELETE_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            m = control.get_memory(memoryId=memory_id)["memory"]
            status = m.get("status")
            if status == "DELETE_FAILED":
                reason = m.get("failureReason")
                logger.error(
                    f"Memory deletion failed: memoryId={memory_id}, "
                    f"failureReason={reason!r}"
                )
                return False
            logger.info(
                f"Memory delete in progress: memoryId={memory_id}, status={status!r}"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(f"Memory delete complete (not found): {memory_id}")
                return True
            raise
        time.sleep(DELETE_POLL_INTERVAL_SEC)
    logger.error(
        f"Timeout waiting for memory deletion after {DELETE_WAIT_TIMEOUT_SEC}s: {memory_id}"
    )
    return False


def delete_iam_role_like_uninstaller(iam, role_name: str) -> None:
    """
    Detach managed policies, delete inline policies, delete role.
    Same flow as uninstaller.delete_iam_roles.
    """
    try:
        attached = iam.list_attached_role_policies(RoleName=role_name)
        for p in attached.get("AttachedPolicies") or []:
            iam.detach_role_policy(
                RoleName=role_name, PolicyArn=p["PolicyArn"]
            )

        inline = iam.list_role_policies(RoleName=role_name)
        for pname in inline.get("PolicyNames") or []:
            iam.delete_role_policy(RoleName=role_name, PolicyName=pname)

        iam.delete_role(RoleName=role_name)
        logger.info(f"Deleted IAM role: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            logger.info(f"IAM role not found (skip): {role_name}")
            return
        logger.warning(f"Could not delete IAM role {role_name}: {e}")


def prune_harness_keys_from_config(cfg: dict, config_path: str) -> None:
    changed = False
    for k in HARNESS_CONFIG_KEYS:
        if k in cfg:
            del cfg[k]
            changed = True
    if not changed:
        return
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"Removed harness-related keys from {config_path}")
    except OSError as e:
        logger.warning(f"Could not update config.json: {e}")


def expected_iam_role_names(project_name: str, region: str) -> tuple[str, str]:
    harness_role = f"role-harness-for-{project_name}-{region}"
    memory_role = f"role-agentcore-memory-for-{project_name}-{region}"
    for label, rn in (("harness execution", harness_role), ("AgentCore Memory", memory_role)):
        if len(rn) > IAM_ROLE_NAME_MAX:
            logger.error(
                f"IAM RoleName for {label} exceeds {IAM_ROLE_NAME_MAX} characters ({len(rn)}): "
                f"{rn!r}. Shorten projectName or region so names match create_harness.py."
            )
            sys.exit(1)
    return harness_role, memory_role


def main() -> None:
    cfg = load_config(CONFIG_PATH)
    if not cfg:
        logger.error("Aborting: empty or missing configuration.")
        sys.exit(1)

    region = cfg.get("region", "us-west-2")
    project_name = cfg.get("projectName", "agent-harness")
    logger.info(f"region={region}, projectName={project_name}")

    harness_role_name, memory_role_name = expected_iam_role_names(project_name, region)

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    iam = boto3.client("iam")

    harness_id = resolve_harness_id(control, cfg, project_name)
    memory_id = resolve_memory_id(control, cfg, project_name)

    harness_ok = (
        delete_harness_resource(control, harness_id) if harness_id else True
    )
    if harness_id and harness_ok:
        harness_ok = wait_until_harness_deleted(control, harness_id)
    if not harness_id:
        logger.info("No harness id resolved; skipping DeleteHarness.")

    memory_ok = delete_memory_resource(control, memory_id) if memory_id else True
    if memory_id and memory_ok:
        memory_ok = wait_until_memory_deleted(control, memory_id)
    if not memory_id:
        logger.info("No memory id resolved; skipping DeleteMemory.")

    delete_iam_role_like_uninstaller(iam, harness_role_name)
    delete_iam_role_like_uninstaller(iam, memory_role_name)

    if harness_ok and memory_ok:
        prune_harness_keys_from_config(cfg, CONFIG_PATH)
    else:
        logger.warning(
            "Skipped config prune because DeleteHarness and/or DeleteMemory failed. "
            "Fix errors and re-run."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
