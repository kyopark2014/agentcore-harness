# AgentCore Harness

AgentCore의 관리형 에이전트 하네스(Managed Agent Harness) 는 이 모든 사전 구축 작업을 단순한 설정(configuration) 으로 대체할 수 있습니다.

## 주요 특징

AgentCore Harness는 격리된 microVM으로 실행됩니다.

- 모든 세션이 보안 격리된 Firecracker microVM에서 실행
- 세션별 독립 파일시스템 & 셸 보유
- 기본적으로 Stateful 세션 간 상태 유지

이 저장소에서는 `deployment/create_harness.py`와 같이 Amazon Bedrock Inference Profile을 사용합니다.

AWS 오픈소스 에이전트 프레임워크인 [Strands Agents](https://strandsagents.com/docs/user-guide/quickstart/python/) 로 구동됩니다.

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

> 참고: AgentCore Harness가 노출하는 내장 도구 구성은 제품 버전·설정에 따라 다를 수 있습니다. `deployment/create_harness.py`는 `tools`에 원격 MCP 2개(Exa, AWS Knowledge)와 Browser·Code Interpreter만 선언하며, `shell` 등을 따로 추가하지는 않습니다.



## 배포하기

### create_harness

[create_harness.py](./deployment/create_harness.py)와 같이 AgentCore Harness에 따라 Agent를 배포합니다. 이를 위해 아래처럼 bedrock-agentcore-control으로 client로 정의합니다.

```python
import boto3

control = boto3.client("bedrock-agentcore-control", region_name="us-west-2")
```

### API 목록

| API | 설명 |
|---|---|
| [`CreateHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html) | 하네스 생성 |
| [`GetHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/get_harness.html) | 하네스 정보 조회 |
| [`UpdateHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/update_harness.html) | 하네스 업데이트 |
| [`DeleteHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/delete_harness.html) | 하네스 삭제 |
| [`ListHarnesses`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/list_harnesses.html) | 하네스 목록 조회 |
| [`InvokeHarness`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_harness.html) | 에이전트 호출 (스트리밍 응답) |
| [`InvokeAgentRuntimeCommand`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime_command.html) | 직접 셸 명령 실행 |

### InvokeHarness 스트리밍 이벤트 타입

| 이벤트 | 설명 |
|---|---|
| `messageStart` | 새 메시지 시작 (role 포함) |
| `contentBlockStart` | 콘텐츠 블록 시작 (text, toolUse, toolResult) |
| `contentBlockDelta` | 증분 콘텐츠. AWS 문서 기준 `text`, `toolUse` 입력, `reasoningContent` 등 |
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

`deployment/config.json`에서 `projectName`과 `region`을 설정합니다.

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
# deployment/create_harness.py와 동일: Inference Profile + get_max_output_tokens(model_id) → 128000
model={
    "bedrockModelConfig": {
        "modelId": "global.anthropic.claude-opus-4-7",
        "maxTokens": 128000,
    }
}
```

시스템 프롬프트를 설정합니다.

```python
# deployment/create_harness.py 의 BASE_SYSTEM_PROMPT 요약 (한국어·에이전트 워크플로 안내 전문은 스크립트 참고)
systemPrompt=[
    {"text": "당신의 이름은 서연이고, 질문에 친근한 방식으로 대답하도록 설계된 대화형 AI입니다.\n한국어로 답변하세요.\n..."}
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
# create_harness.py 가 넘기는 태그
tags={
    "Project": "agent-harness",
    "Env": "dev",
}
```

Skills를 환경에 넣는 방법:
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

IAM 권한 모델:
- `InvokeHarness` → `bedrock-agentcore:InvokeHarness` + `bedrock-agentcore:InvokeAgentRuntime` 필요
- `UpdateHarness` → `bedrock-agentcore:UpdateAgentRuntime` 필요
- `DeleteHarness` → `bedrock-agentcore:DeleteAgentRuntime` 필요

> [!warning] SigV4(AWS IAM) 인증 시 per-user Identity 전파 미지원
> 사용자별 자격증명 범위가 필요하면 Inbound OAuth 설정 필요. SigV4 per-user identity 지원은 향후 릴리스 예정.


### 도구 설정 (`tools`) — 이 저장소(`create_harness.py`) 구성

Gateway·inline_function 등 다른 타입은 `CreateHarness` API에서 지원하지만, 이 스크립트는 아래 네 가지만 연결합니다.

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
- `custom` — 커스텀 (`deployment/create_harness.py`는 Memory 생성 시 custom 전략으로 사용자 선호 추출을 사용합니다.)

Actor ID 로 사용자별 격리 메모리를 제공합니다.
- `actorId + sessionId` 스코프로 사용자별 독립 메모리
- 파일시스템: S3 마운트로 세션 간 영속적 파일 저장 지원


### 환경 & Skills (Environment & Skills)

모델 없이 런타임에 셸만 쓰는 `InvokeAgentRuntimeCommand` 는 아래 Python SDK 절의 코드 예시를 참고하세요.

사용 시나리오:
- 에이전트 시작 전 환경 준비 (repo clone, 의존성 설치, 파일 복사)
- 에이전트 실행 후 후처리 (테스트, commit/push, 아티팩트 추출)
- 개발 중 VM 검사 (`ls`, `cat`, `env`, `python --version`)


#### 커스텀 환경 (컨테이너 이미지)

- ECR에 컨테이너 이미지 푸시 후 하네스에 연결
- 반드시 `linux/arm64` 플랫폼으로 빌드 필요
- 하네스가 컨테이너의 `ENTRYPOINT`와 `CMD`를 오버라이드

Skills: AgentCore Skills는 API로 연결할 수 있으나, `create_harness.py`는 `skills` 배열을 설정하지 않습니다 (위 “이 저장소” 절 참고).
### Python SDK (boto3) 방식


#### invoke_harness

`deployment/create_harness.py` 실행 후 `deployment/config.json`에 저장되는 `HARNESS_ARN`으로 에이전트를 호출합니다. 호출·스트림 처리 예시는 `deployment/test_invoke_harness.py` 와 동일합니다.

```python
import boto3
import json
import os

working_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(working_dir, "config.json"), encoding="utf-8") as f:
    _cfg = json.load(f)

bedrock_region = _cfg.get("region", "us-west-2")
HARNESS_ARN = _cfg["HARNESS_ARN"]  # create_harness.py 가 기록

runtime = boto3.client("bedrock-agentcore", region_name=bedrock_region)
SESSION_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"  # 최소 33자권장, 동일 ID로 대화 연속

response = runtime.invoke_harness(
    harnessArn=HARNESS_ARN,
    runtimeSessionId=SESSION_ID,
    actorId="user-alice",  # 선택: 메모리 격리용 (Harness Memory 설정과 맞출 것)
    messages=[
        {
            "role": "user",
            "content": [{"text": "질문 내용"}],
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


#### 직접 셸 접근 (InvokeAgentRuntimeCommand)

모델 추론 없이 런타임 컨테이너에서 셸 명령만 실행합니다. 인자는 `agentRuntimeArn` (Agent Runtime ARN)이며, `HARNESS_ARN` 과 같지 않을 수 있습니다. 이 레포에서는 `deployment/execute_command_harness.py`가 `config.json`의 `agentRuntimeArn`(또는 임시로 `HARNESS_ARN` 폴백)을 사용합니다.

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

스트림에 `validationException`, `runtimeClientError` 등이 올 수 있습니다. 전체 분기 예시는 `deployment/execute_command_harness.py` 를 참고하세요.


### 응답 구조

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

status 전체 값:
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

### 예외 처리

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


## 저장소 구조와 실행 방법

이 저장소는 AWS 쪽 프로비저닝(`deployment/`)과 로컬 채팅 UI(`application/`)로 나뉩니다.

### 디렉터리 구조

| 경로 | 역할 |
|---|---|
| `deployment/config.json` | `region`, `projectName`, `accountId`(권장: 문자열) 등. `create_harness.py` 실행 후 `harnessId`, `HARNESS_ARN`, Memory·IAM ARN 등이 채워짐 |
| `deployment/create_harness.py` | AgentCore Memory·IAM 역할·Harness 생성 또는 기존 Harness 재사용, READY 폴링 후 `config.json` 갱신 |
| `deployment/delete_harness.py` | Harness·Memory 삭제, 삭제 완료 폴링, IAM·`config.json` 정리 |
| `deployment/agentcore_memory.py` | Memory 생성 시 사용하는 프롬프트·설정 |
| `deployment/test_invoke_harness.py` | `config.json`의 `HARNESS_ARN`으로 `invoke_harness` 스트리밍 호출 예시 |
| `deployment/execute_command_harness.py` | `invoke_agent_runtime_command`(셸만) 예시. `agentRuntimeArn` 우선, 없으면 `HARNESS_ARN` 폴백 |
| `application/app.py` | Streamlit UI 진입점 |
| `application/agentcore_client.py` | Harness ARN 해석(`HARNESS_ARN` 또는 제어 플레인 조회), `invoke_harness` 스트림 처리·`run_harness` |
| `application/utils.py` | `application/config.json` 로드(없으면 일부 기본값으로 새로 작성 시도) |
| `application/notification_queue.py` | Streamlit 상태 영역에 도구 진행 등 표시 |
| `application/chat.py`, `application/info.py` | 앱이 모듈로 로드함. Bedrock 모델 메타데이터·직접 호출 경로용 |

`application/.gitignore`에 `config.json`이 있어 UI용 설정은 저장소에 없을 수 있습니다.



### UI에서 요청이 흐르는 방식

사용자 입력은 Streamlit `app.py`에서 `agentcore_client.run_harness`로 넘어가고, 내부에서 Data Plane 클라이언트 `bedrock-agentcore`의 `invoke_harness`가 `HARNESS_ARN`(또는 설정·목록으로 해석한 ARN)을 대상으로 스트리밍 응답을 처리한 뒤, 텍스트(및 필요 시 이미지 URL)를 화면에 붙입니다.

```
사용자 → application/app.py
              → agentcore_client.run_harness (InvokeHarness, 스트림 파싱)
                    → AWS AgentCore Harness (격리 런타임)
```

## 설치하기

아래 명령어로 실행합니다.

```bash
python deployment/create_harness.py
```

Streamlit으로 실행은 아래와 같습니다.

```python
streamlit run application/app.py
```

Agent의 삭제는 아래 명령어로 수행합니다.

```python
python deployment/delete_harness.py
```

## 실행하기

실행 결과는 아래와 같습니다. AgentCore Harness로 배포시에도 tool 정보를 아래와 같이 알수 있습니다.

<img width="700" alt="image" src="https://github.com/user-attachments/assets/92668d53-9d29-4450-a0f6-bb4cef39ec5b" />

이때의 결과는 아래와 같습니다.

<img width="700" alt="image" src="https://github.com/user-attachments/assets/a1cad40e-a68b-4e4d-a423-9c1736f6c6ce" />




## 관련 문서

[AgentCore Harness 개요](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)

[Harness 시작하기](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-get-started.html)

[AgentCore 요금](https://aws.amazon.com/bedrock/agentcore/pricing/)

[AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)

[Harness 보안 및 액세스 제어](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)

[Strands Agents](https://strandsagents.com/)

[Boto3 - Create Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html)

[Boto3 - Invoke Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_harness.html)

[Boto3 - invoke_agent_runtime_command](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime_command.html)

[AgentCore Harness (개요)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)

[Harness 시작하기](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-get-started.html)

[Harness 실행 역할 정책](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html#harness-execution-role-policy)

[AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)
