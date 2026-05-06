# AgentCore Harness

AgentCore’s Managed Agent Harness replaces much of this upfront build work with straightforward configuration.

## Highlights

AgentCore Harness runs in isolated microVMs.

- Every session runs in a security-isolated Firecracker microVM
- Per-session independent filesystem and shell
- Stateful sessions preserve state across turns by default

This repository uses an Amazon Bedrock Inference Profile, as in `deployment/create_harness.py`.

It runs on the AWS open-source agent framework [Strands Agents](https://strandsagents.com/docs/user-guide/quickstart/python/).

## Capabilities

### Connect to Tools

Supports five tool types plus built-in defaults.

| Tool type | Description |
|---|---|
| MCP Servers | Connect to remote Model Context Protocol endpoints by URL |
| AgentCore Gateway | Managed APIs with authentication, access control, and policy enforcement |
| AgentCore Browser | Managed web browsing and automation |
| AgentCore Code Interpreter | Sandboxed Python / JS / TS execution |
| Inline Functions | Client-side execution (human-in-the-loop patterns) |

Built-in tools:

- `shell` — run bash commands
- `file_operations` — view, create, and edit files

> Note: The exact built-in tool set exposed by AgentCore Harness may vary by product version and configuration. `deployment/create_harness.py` declares two remote MCP servers (Exa, AWS Document) plus Browser and Code Interpreter under `tools`; it does not add `shell` separately.

## Deploy

### create_harness

Deploy an Agent according to Agent Core Harness, following [create_harness.py](./deployment/create_harness.py). Define the client with `bedrock-agentcore-control` as follows:

```python
import boto3

control = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
```

### API summary

| API | Description |
|---|---|
| [`CreateHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html) | Create a harness |
| [`GetHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/get_harness.html) | Get harness details |
| [`UpdateHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/update_harness.html) | Update a harness |
| [`DeleteHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/delete_harness.html) | Delete a harness |
| [`ListHarnesses`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/list_harnesses.html) | List harnesses |
| [`InvokeHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_harness.html) | Invoke the agent (streaming response) |
| [`InvokeAgentRuntimeCommand`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime_command.html) | Run shell commands directly |

### InvokeHarness streaming event types

| Event | Description |
|---|---|
| `messageStart` | Start of a new message (includes role) |
| `contentBlockStart` | Start of a content block (text, toolUse, toolResult) |
| `contentBlockDelta` | Incremental content; per AWS docs: `text`, tool-use input, `reasoningContent`, etc. |
| `contentBlockStop` | End of content block |
| `messageStop` | End of message (includes stopReason) |
| `metadata` | Token usage and latency metrics |
| `runtimeClientError` | Runtime error |

### stopReason values

| Value | Meaning |
|---|---|
| `end_turn` | Agent finished normally |
| `tool_use` | Waiting for inline function invocation |
| `max_tokens` | Per-turn token limit reached |
| `max_iterations_exceeded` | maxIterations exceeded |
| `timeout_exceeded` | timeoutSeconds exceeded |
| `max_output_tokens_exceeded` | maxTokens budget exhausted |

### Required parameters

| Parameter | Type | Description |
|---|---|---|
| `harnessName` | string | Harness name: must start with a letter; alphanumeric and underscores only |
| `executionRoleArn` | string | IAM role ARN the harness assumes at runtime |

### Minimal create example

Set `projectName` and `region` in `deployment/config.json`.

```python
response = control.create_harness(
    harnessName="MyResearchAgent",
    executionRoleArn="arn:aws:iam::123456789012:role/MyHarnessRole"
)

harness = response["harness"]
print(f"Harness ID  : {harness['harnessId']}")
print(f"Harness ARN : {harness['arn']}")
print(f"Status      : {harness['status']}")  # CREATING → READY
```

> [!tip] If you omit `clientToken`, one is generated automatically (for idempotency).

### Key configuration

Configure the model as follows:

```python
# Same as deployment/create_harness.py: Inference Profile + get_max_output_tokens(model_id) → 128000
model={
    "bedrockModelConfig": {
        "modelId": "global.anthropic.claude-opus-4-7",
        "maxTokens": 128000,
    }
}
```

Set the system prompt:

```python
# Summary of BASE_SYSTEM_PROMPT in deployment/create_harness.py (full Korean agent-workflow text is in the script)
systemPrompt=[
    {"text": "Your name is Seoyeon; you are a conversational AI designed to answer questions in a friendly way.\nRespond in Korean.\n..."}
]
```

Configure Skills:

```python
skills=[
    {"path": ".agents/skills/xlsx"},
    {"path": ".agents/skills/github"}
]
```

Optionally set `tags`:

```python
# Tags passed by create_harness.py
tags={
    "Project": "agent-harness",
    "Env": "dev",
}
```

How to ship Skills into the environment:

1. Bake into the container image (recommended for production) — fixed path inside the image
2. Install at session start — via `InvokeAgentRuntimeCommand`

### Security & access control

| Capability | Description |
|---|---|
| Isolated execution | Firecracker microVM; no shared state/filesystem |
| IAM execution role | Apply least privilege |
| Inbound OAuth | JWT-based caller authentication |
| VPC connectivity | Reach private resources |
| Cedar-backed policies | Fine-grained Gateway tool access |

IAM permission model:

- `InvokeHarness` → requires `bedrock-agentcore:InvokeHarness` + `bedrock-agentcore:InvokeAgentRuntime`
- `UpdateHarness` → requires `bedrock-agentcore:UpdateAgentRuntime`
- `DeleteHarness` → requires `bedrock-agentcore:DeleteAgentRuntime`

> [!warning] SigV4 (AWS IAM) authentication does not propagate per-user identity today.
> For per-user credential scope, configure Inbound OAuth. SigV4 per-user identity is planned for a future release.

### Tool configuration (`tools`) — this repo (`create_harness.py`)

Gateway, `inline_function`, and other types are supported by `CreateHarness`, but this script wires only the configurations illustrated below.

```python
tools=[
    # 1. Remote MCP server
    {
        "type": "remote_mcp",
        "name": "exa-search",
        "config": {
            "remoteMcp": {
                "url": "https://mcp.exa.ai/mcp",
                "headers": {"Authorization": "Bearer <token>"}  # optional
            }
        }
    },

    # 2. AgentCore Gateway (SigV4 default)
    {
        "type": "agentcore_gateway",
        "name": "my-gateway",
        "config": {
            "agentCoreGateway": {
                "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/my-gw",
                "outboundAuth": {
                    "awsIam": {}          # SigV4 (default)
                    # "none": {}          # no auth
                    # "oauth": { ... }    # OAuth
                }
            }
        }
    },

    # Gateway + OAuth
    {
        "type": "agentcore_gateway",
        "name": "oauth-gateway",
        "config": {
            "agentCoreGateway": {
                "gatewayArn": "arn:aws:...",
                "outboundAuth": {
                    "oauth": {
                        "providerArn": "arn:aws:...",           # [REQUIRED]
                        "scopes": ["read", "write"],            # [REQUIRED]
                        "grantType": "CLIENT_CREDENTIALS",      # CLIENT_CREDENTIALS | AUTHORIZATION_CODE | TOKEN_EXCHANGE
                        "customParameters": {"key": "value"},
                        "defaultReturnUrl": "https://myapp.com/callback"
                    }
                }
            }
        }
    },

    # 3. AgentCore Browser
    {
        "type": "agentcore_browser",
        "name": "browser",
        "config": {
            "agentCoreBrowser": {
                "browserArn": "arn:aws:..."  # omit for default Browser
            }
        }
    },

    # 4. AgentCore Code Interpreter
    {
        "type": "agentcore_code_interpreter",
        "name": "code-interpreter",
        "config": {
            "agentCoreCodeInterpreter": {
                "codeInterpreterArn": "arn:aws:..."  # omit for default Code Interpreter
            }
        }
    },

    # 5. Inline Function (client-side / human-in-the-loop)
    {
        "type": "inline_function",
        "name": "approve_purchase",
        "config": {
            "inlineFunction": {
                "description": "Request human approval for a purchase",  # [REQUIRED]
                "inputSchema": {                                          # [REQUIRED]
                    "type": "object",
                    "properties": {
                        "item":   {"type": "string"},
                        "amount": {"type": "number"}
                    },
                    "required": ["item", "amount"]
                }
            }
        }
    }
]
```

### Memory & filesystem

State is retained at short- and long-term horizons:

- Short-term → raw in-session events (messages, tool calls)
- Long-term → durable knowledge extraction and semantic search

Long-term strategies you can choose:

- `semantic` — semantic search
- `summarization` — summarization
- `user preference` — user preferences
- `episodic` — episodic memory
- `custom` — custom (`deployment/create_harness.py` uses a custom strategy when creating Memory to extract user preferences)

Actor ID scopes memory per user:

- `actorId + sessionId` scope for isolated per-user memory
- Filesystem: S3 mounts enable persistent files across sessions

### Environment & Skills

For `InvokeAgentRuntimeCommand` (shell-only runtime without a model), see the Python SDK examples below.

Typical uses:

- Prepare the environment before the agent runs (clone repos, install deps, copy files)
- Post-process after the agent (tests, commit/push, artifact extraction)
- Inspect the VM during development (`ls`, `cat`, `env`, `python --version`)

#### Custom environment (container image)

- Push the image to ECR and attach it to the harness
- Build for `linux/arm64`
- The harness overrides the container `ENTRYPOINT` and `CMD`

Skills: AgentCore Skills can be attached via API; `create_harness.py` does not set a `skills` array (see “this repo” above).

### Python SDK (boto3)

#### invoke_harness

After running `deployment/create_harness.py`, invoke using `HARNESS_ARN` written to `deployment/config.json`. Streaming handling matches `deployment/test_invoke_harness.py`.

```python
import boto3
import json
import os

working_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(working_dir, "config.json"), encoding="utf-8") as f:
    _cfg = json.load(f)

bedrock_region = _cfg.get("region", "us-west-2")
HARNESS_ARN = _cfg["HARNESS_ARN"]  # written by create_harness.py

runtime = boto3.client("bedrock-agentcore", region_name=bedrock_region)
SESSION_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"  # ≥33 chars recommended; same ID continues the conversation

response = runtime.invoke_harness(
    harnessArn=HARNESS_ARN,
    runtimeSessionId=SESSION_ID,
    actorId="user-alice",  # optional: memory isolation (align with Harness Memory settings)
    messages=[
        {
            "role": "user",
            "content": [{"text": "Your question here"}],
        }
    ],
)

for event in response["stream"]:
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        if "text" in delta:
            print(delta["text"], end="", flush=True)
    elif "messageStop" in event:
        print(f"\n\n[Stop reason: {event['messageStop']['stopReason']}]")
    elif "metadata" in event:
        usage = event["metadata"].get("usage", {})
        print(
            f"[Tokens - input: {usage.get('inputTokens')}, output: {usage.get('outputTokens')}]"
        )
    elif "runtimeClientError" in event:
        print(f"\n[Error]: {event['runtimeClientError']['message']}")
```

#### Direct shell access (InvokeAgentRuntimeCommand)

Run shell commands in the runtime container without model inference. The argument is `agentRuntimeArn` (Agent Runtime ARN), which may differ from `HARNESS_ARN`. This repo’s `deployment/execute_command_harness.py` uses `agentRuntimeArn` from `config.json` (or falls back to `HARNESS_ARN`).

```python
import boto3

runtime = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = runtime.invoke_agent_runtime_command(
    contentType="application/json",
    accept="application/json",
    runtimeSessionId="1234abcd-12ab-34cd-56ef-1234567890ab",
    agentRuntimeArn=AGENT_RUNTIME_ARN,
    body={
        "command": "python3 -m pip install pandas && ls -la /workspace",
        "timeout": 300,
    },
)

for event in response["stream"]:
    chunk = event.get("chunk") or {}
    if "contentDelta" in chunk:
        delta = chunk["contentDelta"]
        if "stdout" in delta:
            print(delta["stdout"], end="", flush=True)
        if "stderr" in delta:
            print(delta["stderr"], end="", flush=True)
    elif "contentStop" in chunk:
        stop = chunk["contentStop"]
        print(f"\n[exit code: {stop.get('exitCode')}, status: {stop.get('status')}]")
```

The stream may include `validationException`, `runtimeClientError`, etc. See `deployment/execute_command_harness.py` for full branching.

### Response shape

```python
response = control.create_harness(...)
harness = response["harness"]
```

| Field | Type | Description |
|---|---|---|
| `harnessId` | string | Harness ID |
| `harnessName` | string | Harness name |
| `arn` | string | Harness ARN |
| `status` | string | `CREATING` → `READY` (or `CREATE_FAILED`) |
| `executionRoleArn` | string | Execution role ARN |
| `createdAt` | datetime | Creation time |
| `updatedAt` | datetime | Last update time |
| `failureReason` | string | Failure reason when applicable |

Full status values:
`CREATING` · `CREATE_FAILED` · `UPDATING` · `UPDATE_FAILED` · `READY` · `DELETING` · `DELETE_FAILED`

#### Poll until READY

```python
import time

def wait_for_harness_ready(control, harness_id, timeout=120):
    for _ in range(timeout // 5):
        res = control.get_harness(harnessId=harness_id)
        status = res["harness"]["status"]
        print(f"Status: {status}")
        if status == "READY":
            return res["harness"]
        if "FAILED" in status:
            raise RuntimeError(f"Harness creation failed: {res['harness'].get('failureReason')}")
        time.sleep(5)
    raise TimeoutError("Timed out waiting for harness READY")

harness = wait_for_harness_ready(control, response["harness"]["harnessId"])
print(f"Harness ARN: {harness['arn']}")
```

### Error handling

```python
from botocore.exceptions import ClientError

try:
    response = control.create_harness(
        harnessName="agent_harness",
        executionRoleArn="arn:aws:iam::123456789012:role/MyHarnessRole"
    )
except ClientError as e:
    code = e.response["Error"]["Code"]
    if code == "ConflictException":
        print("A harness with this name already exists.")
    elif code == "ServiceQuotaExceededException":
        print("Service quota exceeded.")
    elif code == "AccessDeniedException":
        print("Insufficient IAM permissions.")
    elif code == "ValidationException":
        print(f"Parameter validation error: {e.response['Error']['Message']}")
    elif code == "ThrottlingException":
        print("Request throttled; retry after a short delay.")
    else:
        raise
```

| Exception | Description |
|---|---|
| `ServiceQuotaExceededException` | Account quota exceeded |
| `AccessDeniedException` | Insufficient IAM permissions |
| `ConflictException` | Harness name already exists |
| `ValidationException` | Invalid parameters |
| `ThrottlingException` | Request throttling |
| `InternalServerException` | AWS internal error |

## Repository layout & how to run

This repo splits AWS provisioning (`deployment/`) and a local chat UI (`application/`).

### Directory map

| Path | Role |
|---|---|
| `deployment/config.json` | `region`, `projectName`, `accountId` (string recommended), etc. After `create_harness.py`, populated with `harnessId`, `HARNESS_ARN`, Memory/IAM ARNs, etc. |
| `deployment/create_harness.py` | Create AgentCore Memory, IAM role, Harness (or reuse existing); poll READY; refresh `config.json` |
| `deployment/delete_harness.py` | Delete Harness/Memory, poll completion, clean IAM/`config.json` |
| `deployment/agentcore_memory.py` | Prompts/settings used when creating Memory |
| `deployment/test_invoke_harness.py` | Streaming `invoke_harness` example using `HARNESS_ARN` from `config.json` |
| `deployment/execute_command_harness.py` | `invoke_agent_runtime_command` (shell-only); prefers `agentRuntimeArn`, falls back to `HARNESS_ARN` |
| `application/app.py` | Streamlit entrypoint |
| `application/agentcore_client.py` | Resolve Harness ARN (`HARNESS_ARN` or control-plane lookup), stream handling for `invoke_harness`, `run_harness` |
| `application/utils.py` | Load `application/config.json` (writes defaults if missing) |
| `application/notification_queue.py` | Tool progress in Streamlit state |
| `application/chat.py`, `application/info.py` | Imported by the app; Bedrock metadata and direct-call paths |

`application/.gitignore` includes `config.json`, so UI config may be absent from the repo.

### Request flow in the UI

User input in Streamlit `app.py` goes to `agentcore_client.run_harness`, which calls Data Plane `bedrock-agentcore` `invoke_harness` against `HARNESS_ARN` (or a resolved ARN), parses the stream, and appends text (and image URLs when needed) to the UI.

```
User → application/app.py
          → agentcore_client.run_harness (InvokeHarness, stream parsing)
                → AWS AgentCore Harness (isolated runtime)
```

## Install

Provision with:

```bash
python deployment/create_harness.py
```

Run Streamlit:

```bash
streamlit run application/app.py
```

Delete the agent:

```bash
python deployment/delete_harness.py
```

## Running

Example screenshots below. When deployed on AgentCore Harness you can see tool usage similarly.

<img width="700" alt="image" src="https://github.com/user-attachments/assets/92668d53-9d29-4450-a0f6-bb4cef39ec5b" />

Another example:

<img width="700" alt="image" src="https://github.com/user-attachments/assets/a1cad40e-a68b-4e4d-a423-9c1736f6c6ce" />

## References

[AgentCore Harness overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)

[Get started with Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-get-started.html)

[AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)

[AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)

[Harness security and access control](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)

[Strands Agents](https://strandsagents.com/)

[Boto3 — Create Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html)

[Boto3 — Invoke Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_harness.html)

[Boto3 — invoke_agent_runtime_command](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime_command.html)

[Harness execution role policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html#harness-execution-role-policy)
