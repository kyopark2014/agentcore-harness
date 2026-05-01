# Agentcore Harness

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

---

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

#### 도구 추가 예시 (CLI)

```bash
# 원격 MCP 서버 추가
agentcore add tool --harness my-agent --type remote_mcp \
  --name exa --url https://mcp.exa.ai/mcp

# Browser 추가
agentcore add tool --harness my-agent --type agentcore_browser --name browser

# Code Interpreter 추가
agentcore add tool --harness my-agent --type agentcore_code_interpreter --name code-interpreter

# Gateway 추가 (ARN으로)
agentcore add tool --harness my-agent --type agentcore_gateway \
  --name my-gateway --gateway-arn arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/my-gateway
```

---

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

#### 메모리 설정 예시 (CLI)

```bash
# 메모리 포함 생성 (기본값)
agentcore create --name myagent

# 메모리 없이 생성
agentcore create --name myagent --no-harness-memory

# Actor ID 지정 호출 (사용자별 메모리 격리)
agentcore invoke --harness research-agent \
  --session-id "$(uuidgen)" \
  --actor-id alice \
  "Research tropical vacations under $3k"
```


### 환경 & Skills (Environment & Skills)

#### 직접 셸 접근 (InvokeAgentRuntimeCommand)

모델 추론 없이, 토큰 비용 없이 microVM에 직접 셸 접근 가능.

**사용 시나리오:**
- 에이전트 시작 전 환경 준비 (repo clone, 의존성 설치, 파일 복사)
- 에이전트 실행 후 후처리 (테스트, commit/push, 아티팩트 추출)
- 개발 중 VM 검사 (`ls`, `cat`, `env`, `python --version`)

```bash
# 의존성 설치
agentcore invoke --exec --harness my-agent --session-id "$(uuidgen)" \
  "pip install pandas matplotlib"

# 에이전트가 생성한 파일 확인
agentcore invoke --exec --harness my-agent --session-id "$(uuidgen)" \
  "ls -la /tmp && cat /tmp/results.csv"
```

> [!note] 기본 환경에는 Python과 bash 포함. `git`, `node` 등은 직접 설치하거나 커스텀 환경 사용

#### 커스텀 환경 (컨테이너 이미지)

- ECR에 컨테이너 이미지 푸시 후 하네스에 연결
- 반드시 `linux/arm64` 플랫폼으로 빌드 필요
- 하네스가 컨테이너의 `ENTRYPOINT`와 `CMD`를 오버라이드

```bash
# Dockerfile로 생성
agentcore create --name coding-agent --container ./Dockerfile
agentcore deploy

# 사전 빌드 이미지 참조
agentcore create --name node-agent \
  --container public.ecr.aws/docker/library/node:slim
agentcore deploy
```

#### Agent Skills

마크다운 + 스크립트 번들로 에이전트에게 온디맨드 도메인 지식을 제공합니다.

예시: Excel 파일 처리 방법, 특정 API 사용법

**Skills를 환경에 넣는 방법:**
1. 컨테이너 이미지에 베이크 (권장, 프로덕션용) — 이미지 내 고정 경로에 포함
2. 세션 시작 시 설치 — `InvokeAgentRuntimeCommand`로 설치

```bash
# 세션 시작 시 skill 설치
agentcore invoke --exec --harness my-agent --session-id "$(uuidgen)" \
  "npx @anthropic-ai/agent-skills add xlsx github"

# 하네스에 영구 skill 설정
agentcore add harness --name my-agent \
  --skill-path .agents/skills/xlsx \
  --skill-path .agents/skills/github
agentcore deploy

# 특정 호출에만 skill 오버라이드
agentcore invoke --harness my-agent --skill-path .agents/skills/xlsx \
  "Find errors in the Excel files"
```


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

#### VPC 설정 예시

```bash
agentcore add harness --name internal-agent \
  --network-mode VPC \
  --subnets subnet-0abc1234def56789a \
  --security-groups sg-0abc1234def56789a
agentcore deploy
```

#### Inbound OAuth 설정 예시

```bash
agentcore add harness --name MyNewHarness \
  --authorizer-type CUSTOM_JWT \
  --discovery-url {DISCOVERY_URL} \
  --allowed-clients {CLIENT_ID}
agentcore deploy

# Bearer 토큰으로 호출
agentcore invoke --harness MyNewHarness --bearer-token "{token}" "Hello"
```

---

## 빠른 시작

### Prerequisites

- AWS 자격증명 (프리뷰 리전 중 하나)
- **CLI**: Node.js 20+
- **SDK**: Python 3.10+, boto3, IAM 실행 역할

### AgentCore CLI 방식

```bash
# 1. CLI 설치
npm install -g @aws/agentcore@preview

# 2. 하네스 생성 (non-interactive)
agentcore create --name myresearchagent --model-provider bedrock

# 3. 배포
agentcore deploy

# 4. 호출
agentcore invoke --harness myresearchagent \
  --session-id "$(uuidgen)" \
  "Research three tropical vacation options under $3k, within five hours of NYC."
```

### Python SDK (boto3) 방식

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

---

## API 목록

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

---

## 관련 문서

- [[AgentCore Runtime]]
- [[AgentCore Gateway]]
- [[AgentCore Memory]]
- [[AgentCore Identity]]
- [[AgentCore Observability]]
- [[Strands Agents]]


[Boto3 - Create Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html)

