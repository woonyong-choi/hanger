# 데이터

Markdown 문서와 문서가 다루는 개체를 구분하는 의미 메타데이터 계약이다. `semantics`가 정본 namespace다. JSON 객체를 frontmatter에 넣으면 문서 하나에 여러 개체와 관계를 선언할 수 있다.

```text
---
{
  "title": "WAL과 복구",
  "semantics": {
    "version": 1,
    "document_id": "urn:example:document:wal",
    "document_kind": "note",
    "entities": [
      {"id": "urn:example:concept:wal", "kind": "concept", "label": "WAL"},
      {"id": "urn:example:concept:recovery", "kind": "concept", "label": "복구"}
    ],
    "assertions": [
      {
        "subject": "urn:example:concept:wal",
        "predicate": "skos:broader",
        "object": "urn:example:concept:recovery",
        "status": "accepted",
        "origin": "explicit",
        "evidence": [
          {
            "source": {"id": "urn:example:document:wal"},
            "quote": "WAL은 복구 기법이다.",
            "locator": "# 정의"
          }
        ]
      }
    ]
  }
}
---
# 정의

WAL은 복구 기법이다.
```

## 저장 방식

| 항목 | 값 |
|---|---|
| 정본 | 전달받은 Markdown 원문 |
| namespace | `semantics` |
| 입력 형식 | 문서 첫 줄과 닫는 줄의 `---` 사이에 JSON 객체 또는 안전한 YAML mapping |
| 스키마 버전 | `semantics.version`, 현재 정수 `1` |
| 파서 | [semantics.py](../src/hanger/semantics.py)의 Pydantic 모델과 PyYAML SafeLoader 기반 파서 |
| 어휘 | [vocabulary.py](../src/hanger/vocabulary.py)의 `PREDICATES` |
| 부작용 | 파일·네트워크 읽기와 쓰기 없음 |
| 소비 범위 | Python 파서와 검증 API |

이 형식은 고정 어휘를 쓰는 typed JSON이다. RDF 또는 JSON-LD 처리기가 아니며 `@context`, 원격 context 조회, 자동 추론을 지원하지 않는다. 기존 키워드 그래프·검색 경로에는 아직 연결하지 않는다.

## API

```python
from hanger.semantics import parse_semantics, semantic_schema, verify_evidence
from hanger.vocabulary import predicate_catalog

snapshot = parse_semantics(markdown_text, path="notes/wal.md")
payload = snapshot.model_dump(mode="json")
metadata = snapshot.metadata.model_dump(mode="json", exclude_none=True)
schema = semantic_schema()
catalog = predicate_catalog()
checks = verify_evidence(snapshot, sources=other_snapshots)
```

`parse_semantics(text: str, *, path: str = "document.md")`는 `DocumentSemanticSnapshot`을 반환한다. `semantic_schema()`는 `SemanticMetadata.model_json_schema()`의 결과다. `predicate_catalog()`는 compact predicate를 키로, `uri`, `domain`, `range`, `is_extension`을 값으로 반환한다. 스키마와 카탈로그는 실행 코드에서 생성하며 별도 JSON 파일을 정본으로 두지 않는다.

`verify_evidence(snapshot, *, sources=())`는 새 `EvidenceCheck` 목록을 반환한다. 원래 snapshot의 `evidence_checks`를 바꾸지 않는다. 호출자는 추가 문서를 같은 파서로 읽어 `sources`에 전달한다.

### `DocumentSemanticSnapshot`

| 필드 | 형식 | 뜻 |
|---|---|---|
| `path` | 문자열 | closet 기준 상대 Markdown 경로 |
| `sha256` | 문자열 | frontmatter를 포함한 전체 `text`의 UTF-8 SHA-256 |
| `text` | 문자열 | 변경하지 않은 전체 입력 |
| `body` | 문자열 | frontmatter 뒤의 본문, frontmatter가 없으면 전체 입력 |
| `document_id` | ID | 명시한 문서 ID 또는 `doc:` 경로 참조 |
| `metadata` | `SemanticMetadata` | 검증한 정본 namespace |
| `legacy` | `LegacyMetadata` | 기존 루트 메타데이터 어댑터 |
| `frontmatter` | JSON 객체 | `semantics`와 알려지지 않은 루트 키를 포함한 전체 값 |
| `declared_fields` | 문자열 목록 | `semantics`에 실제로 존재한 최상위 필드명 |
| `evidence_checks` | `EvidenceCheck` 목록 | 파싱 시점에 가능한 인용 대조 결과 |

명시한 `assertions: []`는 `declared_fields`에 `assertions`를 남긴다. 필드를 생략하면 남기지 않는다. namespace 자체의 존재는 `"semantics" in snapshot.frontmatter`로 검사한다. 이 차이를 이용해 후속 소비자가 명시한 빈 관계를 추론으로 덮지 않도록 한다.

### `SemanticMetadata`

| 필드 | 형식 | 기본값 |
|---|---|---|
| `version` | 정수 `1` | `1` |
| `document_id` | ID 또는 null | `null` |
| `document_kind` | 문서 종류 | `unknown` |
| `entities` | `Entity` 목록 | `[]` |
| `assertions` | `RelationAssertion` 목록 | `[]` |

문서 종류는 `unknown`, `note`, `concept`, `person`, `project`, `source`, `claim`, `activity`, `index`, `decision`, `experiment`다. 문서 종류가 `person`이어도 문서 자체는 사람 개체가 아니다. 개체는 `entities`에 별도 ID로 선언한다. 종류를 생략하면 `unknown`이며 명시한 잘못된 값은 오류다.

### `Entity`

| 필드 | 형식 | 기본값 |
|---|---|---|
| `id` | ID | 필수 |
| `kind` | 개체 종류 | `unknown` |
| `label` | 비어 있지 않은 문자열 또는 null | `null` |
| `aliases` | 비어 있지 않은 문자열 목록 | `[]` |
| `definition` | 비어 있지 않은 문자열 또는 null | `null` |
| `central_question` | 비어 있지 않은 문자열 또는 null | `null` |

개체 종류는 `unknown`, `concept`, `person`, `agent`, `activity`, `document`, `project`, `source`, `claim`이다. 이름과 별칭이 같아도 다른 ID를 합치지 않는다. 타입이 없는 개체를 자동으로 `concept`으로 만들지 않는다.

### `RelationAssertion`

| 필드 | 형식 | 기본값 |
|---|---|---|
| `subject` | 관계 출발 개체 ID | 필수 |
| `predicate` | 등록부의 compact predicate | 필수 |
| `object` | 관계 도착 개체 ID | 필수 |
| `status` | `proposed`, `accepted`, `rejected` | `proposed` |
| `origin` | `explicit`, `inferred` | `explicit` |
| `evidence` | `Evidence` 목록 | `[]` |
| `time` | 원문에 기록된 시점 문자열 또는 null | `null` |
| `scope` | 적용 범위 문자열 또는 null | `null` |

`accepted`는 관계를 기록에 채택한 상태다. 사실의 진위나 인용의 타당성을 확정하지 않는다. `accepted`에는 인용 근거가 최소 하나 필요하다. `proposed`와 `rejected`도 원형대로 반환하며 파서는 상태를 승격하지 않는다. `origin`은 관계의 명시·추론 출처를 구분하며 파서 자체는 관계를 추론하지 않는다. `time`은 원문 표현을 보존하며 날짜나 시간대로 해석하지 않는다.

### `Evidence`와 `EvidenceCheck`

`Evidence`는 필수 `source`와 공백만으로 이루어지지 않은 필수 `quote`, 선택 `locator`를 가진다. `source`는 `DocumentReference`이며 필수 `id`, 선택 상대 Markdown `path`, 선택 소문자 64자리 `sha256`으로 구성한다. `locator`는 비어 있지 않은 문자열이며 위치 힌트로 보존한다. 실제 인용 대조는 본문 전체에서 수행한다.

`EvidenceCheck`는 `assertion_index`, `evidence_index`, `source_id`, `status`를 가진다. 두 index는 0부터 센다. `status`는 본문에 정확한 인용이 있으면 `verified`, 해당 ID의 문서를 제공하지 않았으면 `unverified`다. 제공한 문서의 인용·경로·해시가 다르면 오류다. 앞뒤 공백과 줄바꿈을 포함해 문자열 그대로 대조하며 frontmatter 안의 인용은 본문 근거로 쓰지 않는다.

외부 근거가 `unverified`인 `accepted` 관계를 검증 완료로 읽으면 안 된다. 후속 소비자는 채택 상태와 해당 관계의 모든 인용 검증 결과를 함께 확인해야 한다. 인용 존재 검사는 그 인용이 관계를 뒷받침하는지 판단하는 의미 검증과 다르다.

### 기존 메타데이터

`LegacyMetadata.from_frontmatter()` 하나가 `aliases`, `definition`, `central_question`, `broader`, `related`, `requires`, `parent`, `title`, `slug`를 `values`에 원래 형식대로 보존한다. 실제로 존재한 키는 `legacy.declared_fields`에 남긴다. 알려지지 않은 루트 키도 `snapshot.frontmatter`에 남긴다.

YAML의 목록·인용 문자열·여러 줄 문자열을 읽는다. 날짜와 timestamp scalar는 JSON과 호환되는 문자열로 보존한다. YAML alias 참조는 순환·반복 확장을 막기 위해 거부한다. 사용자 정의 태그와 JSON으로 표현할 수 없는 값도 거부한다. 원문은 `snapshot.text`에 그대로 남는다. 기존 루트 관계 값의 타입을 추측해 새 assertion으로 바꾸거나 `semantics`를 덮어쓰지 않는다. 새 namespace가 없으면 새 모델은 `unknown`과 빈 목록을 반환한다.

## 관계

등록부의 `domain`과 `range`는 Hanger가 지원하는 개체 종류다. `prov:Entity` 자리에는 이 계약의 `concept`, `person`, `agent`, `document`, `project`, `source`, `claim`을 허용한다. PROV의 Agent와 Entity는 겹칠 수 있으므로 사람·agent도 Entity 자리에 허용한다. `prov:Agent` 자리에는 `person`, `agent`를 허용한다. `activity`는 Entity 자리에 넣지 않는다.

| predicate | URI | domain | range |
|---|---|---|---|
| `skos:broader` | `http://www.w3.org/2004/02/skos/core#broader` | concept | concept |
| `skos:related` | `http://www.w3.org/2004/02/skos/core#related` | concept | concept |
| `prov:used` | `http://www.w3.org/ns/prov#used` | activity | PROV Entity |
| `prov:wasGeneratedBy` | `http://www.w3.org/ns/prov#wasGeneratedBy` | PROV Entity | activity |
| `prov:wasAttributedTo` | `http://www.w3.org/ns/prov#wasAttributedTo` | PROV Entity | person, agent |
| `prov:wasAssociatedWith` | `http://www.w3.org/ns/prov#wasAssociatedWith` | activity | person, agent |
| `prov:wasDerivedFrom` | `http://www.w3.org/ns/prov#wasDerivedFrom` | PROV Entity | PROV Entity |
| `prov:wasRevisionOf` | `http://www.w3.org/ns/prov#wasRevisionOf` | PROV Entity | PROV Entity |
| `dcterms:subject` | `http://purl.org/dc/terms/subject` | document | concept, person |
| `hanger:supportedBy` | `urn:hanger:relation:supportedBy` | claim | source, document |
| `hanger:refutedBy` | `urn:hanger:relation:refutedBy` | claim | source, document |
| `hanger:requires` | `urn:hanger:relation:requires` | concept | concept |
| `hanger:supersedes` | `urn:hanger:relation:supersedes` | claim, document | claim, document |

`skos:`와 `prov:`는 [W3C SKOS Reference](https://www.w3.org/TR/skos-reference/)와 [W3C PROV-O](https://www.w3.org/TR/prov-o/)의 URI를 사용한다. `dcterms:subject`는 [DCMI Metadata Terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/#http://purl.org/dc/terms/subject)의 용어이며 document에서 concept·person으로 이어지는 범위는 이 계약의 지원 범위다. `hanger:` 네 관계는 Hanger 확장이며 W3C 표준 관계로 표시하지 않는다.

`skos:broader`와 `skos:related`는 concept 사이에서만 허용한다. 순환 검사, 전이 추론, 역관계 생성, 개체 병합은 이 파서에서 수행하지 않는다. SKOS 전체의 추론·일관성 검증을 보장하지 않는다.

## 지켜야 할 규칙

| 조건 | 지키는 방법 |
|---|---|
| ID 형식 | `urn:<namespace>:<value>`, 절대 HTTP(S) IRI, `doc:<percent-encoded relative Markdown path>`만 허용한다. `urn:uuid:`를 쓰면 실제 UUID 문법을 검사한다. |
| 문서 경로와 사람 ID | `doc:`는 document에만 허용한다. 사람 이름이나 일반 파일 경로를 사람 ID로 받지 않는다. |
| ID 생성과 범위 | 개체 ID는 호출자가 부여하고 이름으로 만들지 않는다. 자동 문서 ID만 `document_id_for_path(path)`로 만들며 closet 안에서만 유효하다. 서로 다른 closet은 별도 범위로 처리해야 한다. |
| 문서 이동 | 명시한 URN·HTTP(S) 문서 ID는 경로와 독립이다. 자동 `doc:` ID는 경로를 바꾸면 달라진다. |
| 중복 | 한 입력의 개체 ID와 현재 문서 ID가 중복되면 거부한다. 외부 근거 snapshot의 ID가 중복되어도 거부한다. |
| 관계 끝점 | 현재 문서 ID 외에는 `entities`에 선언해야 한다. 선언하지 않은 타입이나 `unknown`을 관계에 필요한 타입으로 추정하지 않는다. |
| 새 namespace 확장 | 모든 새 의미 모델은 정의하지 않은 필드를 거부한다. |
| 잘못된 입력 | YAML·JSON 구문, 중복 키, 잘못된 종류·predicate·근거를 오류로 반환한다. 기존 모델이나 추론으로 숨기지 않는다. |
| 누락과 빈 값 | 빈 개체·관계 목록은 정상이다. 실제 선언 여부를 `declared_fields`로 구분한다. |

파싱 실패는 `HangerError`를 상속한 `SemanticError`다. 구문·루트 구조·중복 키 오류는 하위 `FrontmatterError`이며 오류 코드는 `invalid_frontmatter`다. namespace 형식, ID, 관계 타입, 인용 오류는 `invalid_semantics`다. 모델을 직접 생성하면 Pydantic `ValidationError`가 발생할 수 있으며 `parse_semantics()`가 이를 Hanger 오류로 변환한다.

## 보관 기간

파서는 원문을 저장·수정·삭제하지 않는다. snapshot과 직렬화 결과의 보관 기간은 호출자가 정한다.

## 스키마 변경

현재 `version`은 정수 `1`만 허용한다. 지원하지 않는 버전은 오류이며 자동 변환하지 않는다. 기존 루트 메타데이터는 그대로 보존하고 새 namespace와 별도로 읽는다.
