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
HARNESS_ARN = _cfg.get("HARNESS_ARN")
if not HARNESS_ARN:
    raise SystemExit(
        "HARNESS_ARN is missing in deployment/config.json. Run create_harness.py first."
    )

runtime = boto3.client("bedrock-agentcore", region_name=bedrock_region)
SESSION_ID  = "1234abcd-12ab-34cd-56ef-1234567890ab"  # 최소 33자

response = runtime.invoke_harness(
    harnessArn=HARNESS_ARN,
    runtimeSessionId=SESSION_ID,   # 동일 ID 재사용 → 대화 이어가기
    actorId="user-alice",          # 사용자별 메모리 격리 (선택)
    messages=[{
        "role": "user",
        "content": [{"text": "AWS Document를 이용하여 AgentCore Harness에 대해 조사하세요."}]
    }]
)

# 스트리밍 응답 처리
for event in response["stream"]:
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        if "text" in delta:
            print(delta["text"], end="", flush=True)
    elif "messageStop" in event:
        print(f"\n\n[Stop reason: {event['messageStop']['stopReason']}]")
    elif "metadata" in event:
        usage = event["metadata"].get("usage", {})
        print(f"[Tokens - input: {usage.get('inputTokens')}, output: {usage.get('outputTokens')}]")
    elif "runtimeClientError" in event:
        print(f"\n[Error]: {event['runtimeClientError']['message']}")