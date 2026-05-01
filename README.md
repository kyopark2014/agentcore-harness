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


[Boto3 - Create Harness](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_harness.html)

