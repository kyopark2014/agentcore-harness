runtime = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = runtime.invoke_agent_runtime_command(
    agentRuntimeArn=HARNESS_ARN,
    runtimeSessionId=SESSION_ID,
    body={"command": "pip install pandas && ls -la /workspace"},
)

for event in response["stream"]:
    chunk = event.get("chunk", {})
    if "contentDelta" in chunk:
        delta = chunk["contentDelta"]
        if "stdout" in delta:
            print(delta["stdout"], end="", flush=True)
        if "stderr" in delta:
            print(delta["stderr"], end="", flush=True)
    elif "contentStop" in chunk:
        print(f"\n[exit code: {chunk['contentStop']['exitCode']}]")