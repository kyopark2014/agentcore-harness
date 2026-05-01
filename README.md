# AgentCore Harness

AgentCore의 관리형 에이전트 하네스(Managed Agent Harness) 는 이 모든 사전 구축 작업을 단순한 설정(configuration) 으로 대체할 수 있습니다.

## 아키텍처 특징

### 격리된 microVM 실행

- 모든 세션이 보안 격리된 Firecracker microVM** 에서 실행
- 세션별 독립 파일시스템 & 셸 보유
- 기본적으로 Stateful 세션 간 상태 유지

### 멀티 모델 지원

- Amazon Bedrock, OpenAI, Google Gemini 등 모든 모델 사용 가능
- 세션 중에도 모델 제공업체 전환 가능 (컨텍스트 유실 없음)
- 기본 모델: Anthropic Claude Sonnet 4.6

### Strands Agents 기반

AWS 오픈소스 에이전트 프레임워크인 **[[Strands Agents]]** 로 구동




## 주요 기능

### 도구 연결 (Connect to Tools)

총 5가지 도구 타입과 기본 내장 도구를 지원합니다.

| 도구 타입 | 설명 |
|---|---|
| MCP Servers | URL로 원격 Model Context Protocol 엔드포인트 연결 |
| AgentCore Gateway | 인증 / 접근 제어 / 정책 시행이 포함된 관리형 API 연결 |
| AgentCore Browser | 관리형 웹 브라우징 & 자동화 |
| AgentCore Code Interpreter | 샌드박스 Python / JS / TS 코드 실행 |
| Inline Functions | 클라이언트 사이드 실행 (Human-in-the-loop 패턴) |

기본 내장 도구는 아래와 같습니다.

- `shell` — bash 명령 실행
- `file_operations` — 파일 뷰 / 생성 / 편집

`allowedTools` 파라미터로 허용 도구 제어 가능합니다.

| 패턴 | 예시 | 매칭 |
|---|---|---|
| `*` | `"*"` | 모든 도구 |
| Plain name | `"shell"` | 내장 도구 이름 |
| `@builtin` | `"@builtin"` | 모든 내장 도구 |
| `@server` | `"@git"` | MCP 서버의 모든 도구 |
| `@server/tool` | `"@git/git_status"` | 특정 MCP 도구 |




## boto3로 배포하기

### create_harness

> [!note] 클라이언트 구분
> - Control Plane (`bedrock-agentcore-control`): 하네스 생성/수정/삭제 등 관리 작업
> - Data Plane (`bedrock-agentcore`): 하네스 호출 (InvokeHarness)

```python
import boto3

control = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
```

### API 목록

| API | 설명 |
|---|---|
| `CreateHarness` | 하네스 생성 |
| `GetHarness` | 하네스 정보 조회 |
| `UpdateHarness` | 하네스 업데이트 |
| `DeleteHarness` | 하네스 삭제 |
| `ListHarnesses` | 하네스 목록 조회 |
| `InvokeHarness` | 에이전트 호출 (스트리밍 응답) |
| `InvokeAgentRuntimeCommand` | 직접 셸 명령 실행 |

### InvokeHarness 스트리밍 이벤트 타입

| 이벤트 | 설명 |
|---|---|
| `messageStart` | 새 메시지 시작 (role 포함) |
| `contentBlockStart` | 콘텐츠 블록 시작 (text, toolUse, toolResult) |
| `contentBlockDelta` | 증분 콘텐츠 |
| `contentBlockStop` | 콘텐츠 블록 종료 |
| `messageStop` | 메시지 종료 (stopReason 포함) |
| `metadata` | 토큰 사용량 및 지연 시간 메트릭 |
| `runtimeClientError` | 실행 중 오류 |

### stopReason 값

| 값 | 의미 |
|---|---|
| `end_turn` | 에이전트 정상 종료 |
| `tool_use` | 인라인 함수 호출 대기 |
| `max_tokens` | 턴당 토큰 한도 도달 |
| `max_iterations_exceeded` | maxIterations 한도 초과 |
| `timeout_exceeded` | timeoutSeconds 한도 초과 |
| `max_output_tokens_exceeded` | maxTokens 예산 소진 |



### 필수 파라미터

| 파라미터 | 타입 | 설명 |
|---|---|---|
| `harnessName` | string | 하네스 이름. 영문자로 시작, 영숫자와 언더스코어만 허용 |
| `executionRoleArn` | string | 하네스가 실행 시 assume할 IAM 역할 ARN |

### 최소 생성 예시

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

> [!tip] `clientToken`을 지정하지 않으면 자동 생성된다 (멱등성 보장용)



### 주요 설정

아래와 같이 모델을 설정합니다.

```python
# Amazon Bedrock
model={
    "bedrockModelConfig": {
        "modelId": "anthropic.claude-sonnet-4-5",  # [REQUIRED]
        "maxTokens": 4096,      # 모델 호출당 최대 생성 토큰
        "temperature": 0.7,
        "topP": 0.9
    }
}
```

시스템 프롬프트를 설정합니다.

```python
systemPrompt=[
    {"text": "You are a helpful research assistant specializing in travel."}
]
```

Skills을 설정합니다.

```python
skills=[
    {"path": ".agents/skills/xlsx"},
    {"path": ".agents/skills/github"}
]
```

필요시 태그 (`tags`)를 설정합니다.

```python
tags={
    "Project": "MyAIProject",
    "Env": "prod"
}
```

**Skills를 환경에 넣는 방법:**
1. 컨테이너 이미지에 베이크 (권장, 프로덕션용) — 이미지 내 고정 경로에 포함
2. 세션 시작 시 설치 — `InvokeAgentRuntimeCommand`로 설치




### 보안 & 접근 제어 (Security)

| 보안 기능 | 설명 |
|---|---|
| 격리된 실행 | Firecracker microVM, 공유 상태/파일시스템 없음 |
| IAM 실행 역할 | 최소 권한 원칙 적용 |
| Inbound OAuth | JWT 기반 호출자 인증 |
| VPC 연결 | 프라이빗 리소스 접근 |
| Cedar 기반 정책 | Gateway 도구 호출 세밀한 접근 제어 |

**IAM 권한 모델:**
- `InvokeHarness` → `bedrock-agentcore:InvokeHarness` + `bedrock-agentcore:InvokeAgentRuntime` 필요
- `UpdateHarness` → `bedrock-agentcore:UpdateAgentRuntime` 필요
- `DeleteHarness` → `bedrock-agentcore:DeleteAgentRuntime` 필요

> [!warning] SigV4(AWS IAM) 인증 시 per-user Identity 전파 미지원
> 사용자별 자격증명 범위가 필요하면 **Inbound OAuth** 설정 필요. SigV4 per-user identity 지원은 향후 릴리스 예정.


### 도구 설정 (`tools`)

5가지 도구 타입 지원:

```python
tools=[
    # 1. 원격 MCP 서버
    {
        "type": "remote_mcp",
        "name": "exa-search",
        "config": {
            "remoteMcp": {
                "url": "https://mcp.exa.ai/mcp",
                "headers": {"Authorization": "Bearer <token>"}  # 선택
            }
        }
    },

    # 2. AgentCore Gateway (SigV4 기본)
    {
        "type": "agentcore_gateway",
        "name": "my-gateway",
        "config": {
            "agentCoreGateway": {
                "gatewayArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/my-gw",
                "outboundAuth": {
                    "awsIam": {}          # SigV4 (기본값)
                    # "none": {}          # 인증 없음
                    # "oauth": { ... }    # OAuth
                }
            }
        }
    },

    # Gateway + OAuth 인증
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
                "browserArn": "arn:aws:..."  # 생략 시 기본 Browser 사용
            }
        }
    },

    # 4. AgentCore Code Interpreter
    {
        "type": "agentcore_code_interpreter",
        "name": "code-interpreter",
        "config": {
            "agentCoreCodeInterpreter": {
                "codeInterpreterArn": "arn:aws:..."  # 생략 시 기본 Code Interpreter 사용
            }
        }
    },

    # 5. Inline Function (클라이언트 사이드 실행 / Human-in-the-loop)
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


### 메모리 & 파일시스템 (Memory & Filesystem)

아래 단기/장기로 상태를 유지합니다.

- 단기 메모리 (Short-term) → 세션 내 원시 이벤트 (메시지, 도구 호출)
- 장기 메모리 (Long-term)  → 내구성 있는 지식 추출 및 시맨틱 검색

장기 메모리 전략은 아래와 같이 선택 가능합니다.

- `semantic` — 시맨틱 검색
- `summarization` — 요약
- `user preference` — 사용자 선호도
- `episodic` — 에피소딕 기억
- `custom` — 커스텀

Actor ID 로 사용자별 격리 메모리를 제공합니다.
- `actorId + sessionId` 스코프로 사용자별 독립 메모리
- 파일시스템: S3 마운트로 세션 간 영속적 파일 저장 지원


### 환경 & Skills (Environment & Skills)

#### 직접 셸 접근 (InvokeAgentRuntimeCommand)

모델 추론 없이, 토큰 비용 없이 microVM에 직접 셸 접근 가능.

**사용 시나리오:**
- 에이전트 시작 전 환경 준비 (repo clone, 의존성 설치, 파일 복사)
- 에이전트 실행 후 후처리 (테스트, commit/push, 아티팩트 추출)
- 개발 중 VM 검사 (`ls`, `cat`, `env`, `python --version`)


#### 커스텀 환경 (컨테이너 이미지)

- ECR에 컨테이너 이미지 푸시 후 하네스에 연결
- 반드시 `linux/arm64` 플랫폼으로 빌드 필요
- 하네스가 컨테이너의 `ENTRYPOINT`와 `CMD`를 오버라이드



### Python SDK (boto3) 방식


#### invoke_harness 

```python
import boto3

client = boto3.client("bedrock-agentcore", region_name="us-west-2")

response = client.invoke_harness(
    harnessArn="arn:aws:bedrock-agentcore:us-west-2:123456789012:harness/MyHarness-XyZ123",
    runtimeSessionId="1234abcd-12ab-34cd-56ef-1234567890ab",  # 최소 33자 이상
    messages=[{
        "role": "user",
        "content": [{"text": "Research three tropical vacation options under $3k."}]
    }],
)

for event in response["stream"]:
    if "contentBlockDelta" in event:
        delta = event["contentBlockDelta"].get("delta", {})
        if "text" in delta:
            print(delta["text"], end="", flush=True)
    elif "runtimeClientError" in event:
        print(f"\nError: {event['runtimeClientError']['message']}")
```


#### 직접 셸 접근 (InvokeAgentRuntimeCommand)

모델 추론 없이, 토큰 비용 없이 microVM에 직접 셸 접근 가능.

```python
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
```



### 📤 응답 구조

```python
response = control.create_harness(...)
harness = response["harness"]
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `harnessId` | string | 하네스 ID |
| `harnessName` | string | 하네스 이름 |
| `arn` | string | 하네스 ARN |
| `status` | string | `CREATING` → `READY` (또는 `CREATE_FAILED`) |
| `executionRoleArn` | string | 실행 역할 ARN |
| `createdAt` | datetime | 생성 시각 |
| `updatedAt` | datetime | 최종 수정 시각 |
| `failureReason` | string | 실패 시 사유 |

**status 전체 값:**
`CREATING` · `CREATE_FAILED` · `UPDATING` · `UPDATE_FAILED` · `READY` · `DELETING` · `DELETE_FAILED`

#### READY 상태까지 폴링하기

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
            raise RuntimeError(f"Harness 생성 실패: {res['harness'].get('failureReason')}")
        time.sleep(5)
    raise TimeoutError("Harness READY 대기 시간 초과")

harness = wait_for_harness_ready(control, response["harness"]["harnessId"])
print(f"Harness ARN: {harness['arn']}")
```

### 🔁 예외 처리

```python
from botocore.exceptions import ClientError

try:
    response = control.create_harness(
        harnessName="MyAgent",
        executionRoleArn="arn:aws:iam::123456789012:role/MyHarnessRole"
    )
except ClientError as e:
    code = e.response["Error"]["Code"]
    if code == "ConflictException":
        print("같은 이름의 하네스가 이미 존재합니다.")
    elif code == "ServiceQuotaExceededException":
        print("서비스 할당량을 초과했습니다.")
    elif code == "AccessDeniedException":
        print("IAM 권한이 부족합니다.")
    elif code == "ValidationException":
        print(f"파라미터 검증 오류: {e.response['Error']['Message']}")
    elif code == "ThrottlingException":
        print("요청이 제한되었습니다. 잠시 후 재시도하세요.")
    else:
        raise
```

| 예외 | 설명 |
|---|---|
| `ServiceQuotaExceededException` | 계정 할당량 초과 |
| `AccessDeniedException` | IAM 권한 부족 |
| `ConflictException` | 동일 이름 하네스 이미 존재 |
| `ValidationException` | 파라미터 유효성 오류 |
| `ThrottlingException` | 요청 스로틀링 |
| `InternalServerException` | AWS 내부 오류 |




## 관련 문서

[Boto3 - Create Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html)

