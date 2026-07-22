import boto3
import time
import logging
import sys
import os
import json
import re
from botocore.exceptions import ClientError
from bedrock_agentcore.memory import MemoryClient

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("utils")

# CreateHarness harnessName: Pattern [a-zA-Z][a-zA-Z0-9_]{0,39} — no hyphens.
_HARNESS_NAME_API_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")


def harness_name_for_api(project_name: str) -> str:
    """
    Map config projectName to a valid Harness name.
    Hyphens are invalid in the API; they are replaced with underscores.
    """
    normalized = project_name.replace("-", "_")
    if not _HARNESS_NAME_API_RE.fullmatch(normalized):
        logger.error(
            "CreateHarness harnessName must match [a-zA-Z][a-zA-Z0-9_]{0,39} "
            f"(after '-'→'_'): got {normalized!r} from projectName={project_name!r}"
        )
        sys.exit(1)
    return normalized


def _memory_id_from_arn(memory_arn: str) -> str | None:
    if not memory_arn:
        return None
    for marker in ("memory/", ":memory/", "/memory/"):
        if marker in memory_arn:
            return memory_arn.split(marker, 1)[-1].strip()
    return memory_arn.split("/")[-1].strip()


def resolve_account_id(cfg: dict) -> str:
    """
    Always return a string account id. JSON may store accountId as a number; IAM
    trust conditions require quoted string values in policy JSON.
    """
    account_id = cfg.get("accountId")
    if account_id is not None and str(account_id).strip() != "":
        return str(account_id).strip()
    sts = boto3.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    cfg["accountId"] = account_id
    return str(account_id).strip()


def create_or_get_harness_execution_role(
    iam, role_name: str, region: str, account_id: str
) -> str:
    """
    Create IAM execution role for Bedrock AgentCore harness.

    Trust: bedrock-agentcore.amazonaws.com only (no SourceArn condition).
    CreateHarness validates AssumeRole against this shape; tight SourceArn
    conditions often fail that check even when runtime assumption works.

    Policies: Bedrock invoke, AgentCore APIs, CloudWatch Logs for AgentCore.
    """
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

    role_arn = None
    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description="Execution role for Bedrock AgentCore harness",
        )
        role_arn = resp["Role"]["Arn"]
        logger.info(f"Created harness execution IAM role: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(assume_role_policy),
        )
        logger.info(f"Using existing harness execution IAM role: {role_name}")

    policy_name = f"harness-exec-inline-for-{role_name}"[:128]
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
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(harness_execution_policy),
    )
    logger.info(f"Attached/updated harness execution inline policy on {role_name}")

    return role_arn


def create_or_get_agentcore_memory_role(
    iam, project_name: str, region: str, account_id: str
) -> str:
    """
    AgentCore Memory execution role.

    Trust must match AWS docs: bedrock-agentcore.amazonaws.com with
    aws:SourceAccount and aws:SourceArn (ArnLike); CreateMemory rejects
    a role without this trust shape (ValidationException).
    """
    account_id = str(account_id).strip()
    role_name = f"role-agentcore-memory-for-{project_name}-{region}"
    iam_role_name_max = 64
    if len(role_name) > iam_role_name_max:
        logger.error(
            f"IAM RoleName exceeds {iam_role_name_max} characters ({len(role_name)}): "
            f"{role_name!r}. Shorten projectName or region in config."
        )
        sys.exit(1)

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreAssumeMemory",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"
                        ),
                    },
                },
            },
        ],
    }

    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description="Execution role for Amazon Bedrock AgentCore Memory",
        )
        role_arn = resp["Role"]["Arn"]
        logger.info(f"Created AgentCore Memory IAM role: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(assume_role_policy),
        )
        logger.info(f"Using existing AgentCore Memory IAM role: {role_name}")

    policy_name = f"agentcore-memory-inline-for-{role_name}"[:128]
    memory_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ListMemories",
                    "bedrock:CreateMemory",
                    "bedrock:DeleteMemory",
                    "bedrock:DescribeMemory",
                    "bedrock:UpdateMemory",
                    "bedrock:ListMemoryRecords",
                    "bedrock:CreateMemoryRecord",
                    "bedrock:DeleteMemoryRecord",
                    "bedrock:DescribeMemoryRecord",
                    "bedrock:UpdateMemoryRecord",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:*:inference-profile/*",
                ],
            }
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(memory_policy),
    )
    logger.info(f"Attached/updated AgentCore Memory inline policy on {role_name}")

    # CreateMemory validates the role soon after IAM updates; brief pause helps propagation.
    time.sleep(2)

    return role_arn


WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "config.json")


def load_config(config_path: str) -> dict:
    config = None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        config = {}

        projectName = "agent-harness"
        session = boto3.Session()
        region = session.region_name
        config["region"] = region
        config["projectName"] = projectName

        sts = boto3.client("sts")
        response = sts.get_caller_identity()
        accountId = response["Account"]
        config["accountId"] = accountId

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    return config


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


def ensure_agent_memory_arn(
    cfg: dict,
    bedrock_region: str,
    project_name: str,
    config_path: str,
) -> str:
    """
    Load AgentCore memory ARN from config, or find/create memory like ref/chat.py via agentcore_memory.

    Memory name token matches agentcore_memory.retrieve_memory_id: projectName with '-' -> '_'.
    """
    existing = cfg.get("agent_memory_arn")
    if existing:
        mid = _memory_id_from_arn(existing)
        control_probe = boto3.client(
            "bedrock-agentcore-control", region_name=bedrock_region
        )
        if not mid:
            logger.warning(
                f"Cannot parse memory id from agent_memory_arn={existing!r}; rediscovering"
            )
            cfg.pop("agent_memory_arn", None)
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except OSError as e:
                logger.warning(
                    "Could not persist config after stripping bad agent_memory_arn: %s", e
                )
        else:
            try:
                control_probe.get_memory(memoryId=mid)
                logger.info(
                    f"Using agent_memory_arn from config (verified): {existing}"
                )
                return existing
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
                logger.warning(
                    f"agent_memory_arn points to deleted memory ({mid}); "
                    "finding or creating a replacement."
                )
                cfg.pop("agent_memory_arn", None)
                try:
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                except OSError as e:
                    logger.warning(
                        "Could not persist removal of stale agent_memory_arn: %s", e
                    )

    memory_client = MemoryClient(region_name=bedrock_region)
    memory_token = project_name.replace("-", "_")

    memories = memory_client.list_memories()
    logger.info(f"Listing AgentCore memories (project token={memory_token!r})")
    for memory in memories or []:
        mid = memory.get("id") or ""
        if mid.split("-")[0] == memory_token:
            arn = memory.get("arn")
            if not arn:
                account_id = resolve_account_id(cfg)
                arn = (
                    f"arn:aws:bedrock-agentcore:{bedrock_region}:{account_id}:memory/{mid}"
                )
            logger.info(f"Found existing memory id={mid}, arn={arn}")
            cfg["agent_memory_arn"] = arn
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except OSError as e:
                logger.warning(f"Could not persist agent_memory_arn to config.json: {e}")
            else:
                logger.info(f"Saved agent_memory_arn to {config_path}")
            return arn

    account_id_mem = resolve_account_id(cfg)
    iam_local = boto3.client("iam")
    # Always (re)provision default memory role ARN and trust policy; CreateMemory rejects
    # trust without aws:SourceAccount / aws:SourceArn (see AgentCore Memory docs).
    memory_exec_role = create_or_get_agentcore_memory_role(
        iam=iam_local,
        project_name=project_name,
        region=bedrock_region,
        account_id=account_id_mem,
    )
    if cfg.get("agentcore_memory_role") != memory_exec_role:
        cfg["agentcore_memory_role"] = memory_exec_role
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except OSError as e:
            logger.warning(f"Could not persist agentcore_memory_role to config.json: {e}")
    logger.info(f"Using agentcore_memory_role: {memory_exec_role}")

    from agentcore_memory import shared_memory_strategies

    _memory_creation_attempts = 6
    result = None
    for attempt in range(_memory_creation_attempts):
        try:
            result = memory_client.create_memory_and_wait(
                name=memory_token,
                description=f"Memory for {project_name}",
                event_expiry_days=365,
                strategies=shared_memory_strategies(),
                memory_execution_role_arn=memory_exec_role,
            )
            break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            msg = (e.response.get("Error", {}).get("Message") or "").lower()
            if code != "ValidationException" or not (
                "trust" in msg or "valid trust" in msg
            ):
                raise
            if attempt >= _memory_creation_attempts - 1:
                raise
            wait_sec = 3 * (attempt + 1)
            logger.warning(
                "CreateMemory trust validation failed (attempt %s/%s); "
                "re-syncing memory role and waiting %ss. Error: %s",
                attempt + 1,
                _memory_creation_attempts,
                wait_sec,
                e.response.get("Error", {}).get("Message"),
            )
            time.sleep(wait_sec)
            memory_exec_role = create_or_get_agentcore_memory_role(
                iam=iam_local,
                project_name=project_name,
                region=bedrock_region,
                account_id=account_id_mem,
            )
    if result is None:
        raise RuntimeError("create_memory_and_wait returned no result")
    logger.info(f"create_memory_and_wait result: {result}")

    mem_id = result.get("id")
    mem_arn = result.get("arn")
    if not mem_arn and mem_id:
        account_id = resolve_account_id(cfg)
        mem_arn = (
            f"arn:aws:bedrock-agentcore:{bedrock_region}:{account_id}:memory/{mem_id}"
        )
    if not mem_arn:
        logger.error(f"Could not resolve memory ARN from API response: {result!r}")
        sys.exit(1)

    logger.info(f"Created AgentCore memory: {mem_arn}")
    cfg["agent_memory_arn"] = mem_arn
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        logger.warning(f"Could not persist agent_memory_arn to config.json: {e}")
    else:
        logger.info(f"Saved agent_memory_arn to {config_path}")
    return mem_arn


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


def find_harness_by_api_name(control, harness_api_name: str) -> dict | None:
    for h in _paginate_list_harnesses(control):
        if h.get("harnessName") == harness_api_name:
            return h
    return None


def ensure_harness_memory_binding(control, harness_id: str, agent_memory_arn: str) -> None:
    """
    If the harness still references a deleted Memory, InvokeHarness fails
    (e.g. ListEvents ResourceNotFound). Align binding with EnsureAgentMemory ARN.
    """
    h = control.get_harness(harnessId=harness_id)["harness"]
    memory_cfg = (
        ((h.get("memory") or {}).get("agentCoreMemoryConfiguration") or {})
        if isinstance(h.get("memory"), dict)
        else {}
    )
    current = memory_cfg.get("arn")
    if current == agent_memory_arn:
        return

    logger.info(
        f"Updating harness memory: {current!r} -> {agent_memory_arn!r} "
        f"(harnessId={harness_id})"
    )
    control.update_harness(
        harnessId=harness_id,
        memory={
            "optionalValue": {
                "agentCoreMemoryConfiguration": {
                    "arn": agent_memory_arn,
                },
            },
        },
    )


def main() -> None:
    config = load_config(CONFIG_PATH)

    bedrock_region = config.get("region", "us-west-2")
    logger.info(f"bedrock_region: {bedrock_region}")
    project_name = config.get("projectName", "mop")
    logger.info(f"projectName: {project_name}")
    harness_api_name = harness_name_for_api(project_name)
    if harness_api_name != project_name:
        logger.info(f"harnessName (API): {harness_api_name} (from projectName)")

    execution_role_arn = config.get("executionRoleArn")
    logger.info(f"executionRoleArn (before sync): {execution_role_arn}")

    account_id = resolve_account_id(config)
    iam = boto3.client("iam")

    role_name = f"role-harness-for-{project_name}-{bedrock_region}"
    iam_role_name_max = 64
    if len(role_name) > iam_role_name_max:
        logger.error(
            f"IAM RoleName exceeds {iam_role_name_max} characters ({len(role_name)}): "
            f"{role_name!r}. Shorten projectName or region in config."
        )
        sys.exit(1)

    execution_role_arn = create_or_get_harness_execution_role(
        iam=iam,
        role_name=role_name,
        region=bedrock_region,
        account_id=account_id,
    )
    if execution_role_arn != config.get("executionRoleArn"):
        config["executionRoleArn"] = execution_role_arn
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
        except OSError as e:
            logger.warning(f"Could not persist executionRoleArn to config.json: {e}")
    logger.info(f"Using executionRoleArn: {execution_role_arn}")

    model_id = "global.anthropic.claude-opus-4-7"
    system_prompt = [{"text": BASE_SYSTEM_PROMPT}]

    agent_memory_arn = ensure_agent_memory_arn(
        config,
        bedrock_region=bedrock_region,
        project_name=project_name,
        config_path=CONFIG_PATH,
    )

    control = boto3.client(
        "bedrock-agentcore-control",
        region_name=bedrock_region,
    )

    existing = find_harness_by_api_name(control, harness_api_name)
    if existing:
        harness_id = existing["harnessId"]
        logger.info(
            f"Harness {harness_api_name!r} already exists (harnessId={harness_id}); "
            "skipping CreateHarness."
        )
    else:
        try:
            response = control.create_harness(
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
                memory={
                    "agentCoreMemoryConfiguration": {
                        "arn": agent_memory_arn
                    }
                },
                truncation={
                    "strategy": "sliding_window",
                    "config": {"slidingWindow": {"messagesCount": 50}},
                },
                maxIterations=20,
                maxTokens=50000,
                timeoutSeconds=300,
                environment={
                    "agentCoreRuntimeEnvironment": {
                        "lifecycleConfiguration": {
                            "idleRuntimeSessionTimeout": 600,
                            "maxLifetime": 14400,
                        },
                        "networkConfiguration": {
                            "networkMode": "PUBLIC",
                        },
                    }
                },
                environmentVariables={
                    "LOG_LEVEL": "info",
                },
                tags={"Project": project_name, "Env": "dev"},
            )
            harness_id = response["harness"]["harnessId"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConflictException":
                raise
            rerun = find_harness_by_api_name(control, harness_api_name)
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

    ensure_harness_memory_binding(control, harness_id, agent_memory_arn)

    harness_arn = None
    for _ in range(24):
        res = control.get_harness(harnessId=harness_id)
        if res["harness"]["status"] == "READY":
            harness_arn = res["harness"]["arn"]
            print(f"✅ Harness ready: {harness_arn}")
            break
        time.sleep(5)

    if not harness_arn:
        logger.error("Harness did not reach READY within the polling window.")
        sys.exit(1)

    config["harnessId"] = harness_id
    config["HARNESS_ARN"] = harness_arn
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        logger.warning(f"Could not persist HARNESS_ARN to config.json: {e}")
    else:
        logger.info(f"Saved HARNESS_ARN to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
