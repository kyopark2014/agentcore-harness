import boto3
import json
import os

working_dir = os.path.dirname(os.path.abspath(__file__))
_config_path = os.path.join(working_dir, "config.json")
try:
    with open(_config_path, encoding="utf-8") as f:
        _cfg = json.load(f)
except FileNotFoundError:
    raise SystemExit(f"Missing {_config_path}. Run create_harness.py first.") from None

bedrock_region = _cfg.get("region", "us-west-2")
# agentRuntimeArn is required; optional legacy key HARNESS_ARN for local configs.
AGENT_RUNTIME_ARN = _cfg.get("agentRuntimeArn") or _cfg.get("HARNESS_ARN")
if not AGENT_RUNTIME_ARN:
    raise SystemExit(
        "agentRuntimeArn (or HARNESS_ARN) is missing in deployment/config.json. "
        "Set the AgentCore agent runtime ARN and retry."
    )

runtime = boto3.client("bedrock-agentcore", region_name=bedrock_region)
# Per API: runtimeSessionId is optional (autopopulated if omitted); keep explicit for session reuse.
SESSION_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"

# See: invoke_agent_runtime_command — contentType/accept for JSON payload/response.
response = runtime.invoke_agent_runtime_command(
    contentType="application/json",
    accept="application/json",
    runtimeSessionId=SESSION_ID,
    agentRuntimeArn=AGENT_RUNTIME_ARN,
    body={
        "command": "python3 -m pip install pandas && ls -la /workspace",
        "timeout": 300,
    },
)

# Event stream: one top-level key per event — chunk, accessDeniedException, …
for event in response["stream"]:
    chunk = event.get("chunk") or {}
    if "contentStart" in chunk:
        pass
    if "contentDelta" in chunk:
        delta = chunk["contentDelta"]
        if "stdout" in delta:
            print(delta["stdout"], end="", flush=True)
        if "stderr" in delta:
            print(delta["stderr"], end="", flush=True)
    if "contentStop" in chunk:
        stop = chunk["contentStop"]
        exit_code = stop.get("exitCode")
        status = stop.get("status")
        print(f"\n[exit code: {exit_code}, status: {status}]")
    for key in (
        "accessDeniedException",
        "internalServerException",
        "resourceNotFoundException",
        "serviceQuotaExceededException",
        "throttlingException",
        "validationException",
        "runtimeClientError",
    ):
        if key in event:
            err = event[key]
            msg = err.get("message", err)
            print(f"\n[{key}] {msg}", flush=True)
