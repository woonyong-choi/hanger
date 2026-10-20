# 검색 응답

`markdown-notes-agent search "needle" --folder public`과 MCP `search_documents`는 같은 검색 응답을 사용한다. 이름이나 본문에서 찾은 문서를 시작점으로 삼고, 검증한 관계로 연결된 자료를 `relations.documents`에 담는다. 이 명세는 관계 검색 변경과 함께 제공하는 계약이다.

## 요청

```json
{"query":"needle","folders":["public"],"limit":20}
```

| 매개변수 | 형식 | 뜻 |
|---|---|---|
| `query` | 비어 있지 않은 문자열 | 본문, 제목, 별칭 검색어 |
| `vault` | 문자열 또는 생략 | 등록한 vault 선택 |
| `folders` | 문자열 배열 | vault 안 상대 폴더들의 합집합. 생략하면 전체 |
| `limit` | 1~100 정수 또는 생략 | Elasticsearch 시작 문서 후보 수. 생략하면 검색 설정값 |
| `fallback_reason` | `insufficient_evidence`, `user_reported_missing` 또는 생략 | 해당 범위를 다시 동기화한 뒤 검색 |

CLI는 `--folder`를 여러 번 받을 수 있다. 관계 조회에도 같은 폴더 범위와 `.markdown-notes-agentignore`를 적용한다.

중복 폴더와 이미 선택한 상위 폴더에 포함되는 하위 폴더를 제거한 뒤 검색한다. 폴더 이름은 패턴으로 해석하지 않고 해당 폴더 아래의 경로만 선택한다. 정규화한 폴더가 256개 이하면 검색 요청 한 번을 보낸다. 그보다 많으면 256개씩 나누어 같은 인덱스 스냅샷(PIT)에서 검색한다. 배치가 `b`개이면 PIT 생성과 해제를 포함해 서버 요청은 `b + 2`번이다.

`limit`은 전체 선택 범위에 적용한다. 각 배치의 상위 후보를 점수 내림차순과 경로 오름차순으로 합쳐 전체 상위 후보만 남긴다. 어느 배치에서든 시간 초과, 샤드 실패나 통신 오류가 발생하면 부분 결과를 반환하지 않고 오류로 끝낸다.

## 결과

상위 `documents`에는 검색 설정의 본문 예산 안에서 읽은 문서가 들어간다. `listed`에는 나머지 문서의 경로와 검색어 주변 줄이 들어간다. 두 목록의 `matched_by`는 `text`, `surface_text`, `names` 중 일치한 검색 조건을 담는다. `names`에는 명시한 별칭과 기존 키워드 이름이 함께 들어간다.

`relations`는 관계 저장소가 연결되어 있으면 다음 필드를 갖는다. 연결되어 있지 않으면 `null`이다.

| 필드 | 뜻 |
|---|---|
| `documents` | 시작 문서를 제외한 관계 작성 문서, 끝점 설명 문서, 근거 문서 |
| `coverage` | `bounded_verified_relations` |
| `limits` | `hops: 2`, `documents: 64`, `bytes: 524288`, `assertions: 128` |
| `checked_documents` | 읽기를 시도한 범위 내 문서 수 |
| `read_bytes` | 읽어서 처리한 원문 UTF-8 바이트 합계 |
| `output_bytes` | 반환한 관계 문서 객체를 JSON으로 직렬화한 UTF-8 바이트 합계 |
| `omitted` | 제외 사유 코드 목록. 제외 문서 경로와 인용문 미포함 |
| `unverified_scope` | `unindexed_documents`, `relations_beyond_limits`, `semantic_support_of_quotes` |

관계 문서 객체는 `path`, `content`, `hash`, `matched_by`, `seed`, `relation_path`, `evidence`를 갖는다. `content`는 조회 시 읽은 원문 전체이고 `hash`는 SHA-256이다. `matched_by`는 `["relation"]`이며 `seed`는 처음 검색된 문서 경로다. 문서 하나에 여러 경로가 있으면 먼저 찾은 경로 하나를 반환한다.

끝점이 개체 식별자이면 해당 개체의 이름·별칭·정의·핵심 질문 중 비어 있지 않은 값이 있는 문서를 반환한다. `id`, `kind`만 있거나 설명 값이 `null` 또는 빈 목록인 선언은 끝점 설명 문서로 반환하지 않는다. 이 문서도 해당 관계의 작성 문서나 근거로 검증하면 반환한다. 문서 식별자 자체를 참조한 끝점은 그 문서를 반환한다.

`relation_path`는 순서대로 거친 관계 배열이다. 각 관계에는 원문 선언의 `subject`, `predicate`, `object`, `status`, `origin`과 작성 문서의 `owner`, `owner_sha256`이 들어간다. `direction`은 탐색 방향에 따라 `forward` 또는 `reverse`이며 선언한 관계의 방향 자체를 바꾸지 않는다.

각 관계의 `evidence`에는 `source.id`, `source.path`, `source.sha256`, `quote`, `locator`가 들어간다. 문서 객체의 `evidence`는 경로 전체의 근거를 모은 배열이다. 인용문 일치는 원문 존재를 뜻하며, 인용문이 관계의 의미를 충분히 입증하는지까지 판정하지 않는다.

### 범위와 한도

관계의 작성 문서, 시작 개체, 중간 개체, 끝점과 모든 근거는 현재 범위 안에 있어야 한다. 조회 시 원문 SHA-256이 SQLite에 저장한 값과 같아야 한다. 하나라도 삭제되거나 이름이 바뀌거나 수정되어 동기화되지 않았으면 그 관계를 사용하지 않는다. 근거 문서를 동기화했더라도 인용문이 현재 본문과 일치하지 않으면 제외한다.

읽기와 관계 문서 출력에 각각 524288바이트 한도를 적용한다. 출력 한도는 관계 문서 객체 합계이며 응답 전체의 전송 크기는 아니다. `documents: 64`는 반환 수가 아닌 검증할 문서 수 한도다. `assertions: 128`은 검증을 시도할 관계 수 한도다. 최대 두 관계를 따라가며, 전체 연결망을 빠짐없이 찾았다고 보장하지 않는다.

| `omitted` 코드 | 조건 |
|---|---|
| `assertion_budget` | 관계 검사 한도 도달 |
| `byte_budget` | 원문 읽기 바이트 한도 초과 |
| `document_budget` | 문서 검사 한도 초과 |
| `identity_conflict` | 같은 식별자의 문서 또는 개체 선언 충돌 |
| `missing_changed_or_invalid_document` | 파일 읽기 또는 의미 메타데이터 검증 실패 |
| `outside_scope_or_ignored` | 폴더 범위 밖이거나 제외한 문서 |
| `projection_stale_or_missing` | 현재 원문과 파생 저장소의 해시 불일치 또는 저장 누락 |
| `proposed_rejected_or_inferred` | 명시적으로 수락한 관계가 아님 |
| `response_byte_budget` | 관계 문서 출력 바이트 한도 초과 |
| `unresolved_document` | 참조한 문서 식별자를 현재 문서로 해석할 수 없음 |
| `unresolved_endpoint` | 끝점 식별자를 검증할 수 없음 |
| `unverified_evidence` | 근거 원문 또는 인용문 검증 실패 |

## 메타데이터와 호환성

Markdown이 정본이다. `relations.sqlite`는 문서와 개체 식별자를 분리해 저장하는 재생성 가능한 파생 저장소다. 저장소가 비면 다음 전체 동기화가 원문에서 다시 만든다. 검색은 추가 모델 호출 없이 SQLite 후보와 현재 파일을 검사한다.

`status: accepted`, `origin: explicit`이고 모든 근거가 `verified`인 관계만 확장한다. 이름이 같은 개체도 식별자가 다르면 합치지 않는다. 충돌하는 식별자는 임의로 하나를 선택하지 않는다.

기존 YAML 메타데이터와 `semantics` JSON 메타데이터를 읽는다. 개념 문서의 JSON에 개념 개체 하나를 선언하면 명시한 설명 필드는 같은 YAML 값보다 우선한다. 명시한 값과 빈 목록은 모델 결과보다 우선하고, 모델은 누락한 필드만 보완한다.

- 별칭을 빈 목록으로 명시하면 이전 YAML 별칭을 `names`에 색인하지 않는다. 명시한 별칭은 Elasticsearch 이름 검색에 포함한다.
- JSON에 `assertions`를 명시하면 빈 목록이어도 기존 YAML 관계와 `parent`를 대체한다.
- 원문에 남아 있는 YAML 값은 본문 검색에서 일치할 수 있다.

YAML의 구별 설명(`differentia`)은 명시한 문자열이 우선하며 빈 문자열과 `null`은 모델 보완을 막는다. 기존 필수 개념 필드가 완성돼 추출을 생략해도 명시한 구별 설명은 유지한다.

YAML의 `same_as`는 원래 값을 보존한다. 키가 있으면 해당 문서를 포함하는 모델 동일판정과 자동 병합을 차단한다. `null`은 추출 어댑터에서 빈 문자열로 처리하며, 명시한 값을 병합 승인으로 사용하지 않는다.

인물, 활동, 판단처럼 비개념으로 선언한 문서는 자동 개념 추출과 병합 대상에서 제외한다.

기존 `broader`, `related`, `requires`, `parent` 목록은 문자열과 `{"id":"slug-or-path"}`를 받는다. 별칭과 관계 필드의 `null`은 명시한 빈 목록으로 처리한다. 알려진 형식이 아닌 항목은 진단으로 보고하며 빈 목록으로 바꾸지 않는다.

검색 결과의 `keywords`는 기존 개념 이름(`name`), 트리 위치(`position`), 정의(`definition`)를 유지한다. `source: legacy_keyword_tree`, `verification: unverified_model_or_links`로 출처와 미검증 상태를 표시한다. 키워드 트리와 모델 제안의 상세는 `keyword_tree` 도구에서 조회한다. `relations`는 이 요약을 검증된 관계로 승격하지 않는다.

## 검증

[관계 검색 검사](../tests/test_relations.py)는 합성 문서로 두 단계 연결, 별칭, 명시 빈 목록, 동명이인, 범위 제외, 삭제, 이름 변경, 원문 변경, 인용문 불일치, 읽기 한도와 CLI·MCP 응답을 검사한다. `MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL`을 지정하면 실제 Elasticsearch와 Nori를 사용한 별칭·관계 응답 검사도 실행한다.
