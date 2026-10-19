---
name: markdown-notes-agent
description: "Markdown Notes Agent MCP 도구가 연결된 환경에서 사용자의 Markdown 노트를 찾거나, 쓰거나, 고치거나, 병합하거나, 정리할 때 쓴다."
---

# Markdown Notes Agent

등록한 노트 폴더인 vault에서 문서를 찾고 변경한다. 노트 파일을 직접 고치지 않고 Markdown Notes Agent MCP 도구를 쓴다. Markdown Notes Agent는 폴더 규칙을 읽고 수정안 작성과 검토를 거쳐 원문이 바뀌지 않았을 때만 반영한다.

## 안내 언어

Markdown Notes Agent는 영어로 답한다. 메시지가 두 부분이면 다음처럼 처리한다.

- `Tell the user: ...`는 사용자 안내다. 줄이거나 바꾸지 않고 사용자의 언어로 번역해 모두 전달한다.
- `Then: ...`은 도구를 쓰는 쪽의 다음 행동이다. 사용자에게 보여 주지 않고 실행한다.

번역할 때 명령, 경로, id, vault, Markdown Notes Agent, `.markdown-notes-agentignore`, `VAULT.md`는 그대로 둔다.

## 첫 사용

1. `list_vaults`를 호출한다.
2. 등록한 vault이 없으면 Markdown Notes Agent가 돌려준 질문을 전달한다. 사용자가 답한 절대 경로로 `register_vault`을 호출한다. 폴더를 추측하지 않는다.
3. vault이 둘 이상이면 요청마다 `vault`을 넘긴다. `list_vaults`의 id나 vault 안의 경로를 쓴다.

## 대기 중인 결정

`decision_required`가 나오면 사용자 답을 기다리는 결정이 있다. `.markdown-notes-agentignore` 변경이나 삭제가 원인일 수 있다.

1. 질문과 두 선택지를 사용자 언어로 번역해 보여 준다.
2. 사용자 답을 기다린다. 한쪽이 명백해 보여도 대신 고르지 않는다.
3. 결정 id와 `approve` 또는 `decline`으로 `decide`를 호출한다. 그다음 원래 요청을 다시 실행한다.

## 노트 찾기

- 사용자의 질문을 `query`로 넣어 `search_documents`를 호출한다. 폴더를 지정했으면 vault 상대 경로를 `folders`로 넘긴다.
- 상위 검색 결과에는 본문이 있고 나머지는 일치한 줄이 있다. 더 읽을 문서는 `read_document`로 연다.
- 사용자가 말한 노트가 없으면 `fallback_reason: "user_reported_missing"`으로 다시 검색한다.
- 트리에 등록된 노트의 검색 결과에는 개념 이름, 트리 위치, 정의를 담은 `keywords`가 있다. 아직 트리에 없는 노트에는 이 값이 없을 수 있다. `read_document`는 트리 정보 없이 현재 원문과 해시를 돌려준다.
- `keyword_tree`에 노트 경로나 이름을 넘기면 상위·하위·연관·선행 개념을 확인한다. 인자 없이 호출하면 트리 꼭대기와 구축 진행을 확인한다.

## 노트 변경

1. 새 변경 요청마다 `issue_key`를 호출한다. 다른 요청에 같은 키를 쓰지 않는다.
2. `intent`를 구성해 `request_change`를 호출한다. 아래 표의 필드를 쓴다.
3. `accepted`는 대기열 접수다. `job_status`가 `completed`, `deferred`, `conflict`, `failed`, `cancelled`, `expired` 중 하나를 반환할 때까지 확인한다. 최종 상태와 메시지를 사용자에게 전달한다.
4. `conflict`면 처리 중 원문이 바뀐 상태다. 재검토를 안내하고 사용자가 동의하면 새 키로 `resolve_conflict`를 호출한다.

| 필드 | 값 |
|---|---|
| `kind` | 새 작성은 `write`, 기존 노트 수정은 `edit`, 병합·분리·연결은 `organize` |
| `instruction` | 사용자가 요청한 내용 |
| `query` | 관련 노트를 찾을 검색어 |
| `paths` | 사용자가 지정해 반드시 읽을 기존 노트 |
| `folders` | 사용자가 요청한 경우에만 검색 범위 지정 |
| `profile` | 사용자가 요청한 `note`, `wiki`, `work_record`, `blog`, `creative` |
| `allow_delete` | 사용자가 삭제를 요청한 경우에만 지정 |

글이나 파일을 그대로 보관하려면 `collect`를 쓴다. 모델 호출 없이 `inbox/`에 저장한다.

## 하지 않는 것

- vault을 대신 제거하지 않는다. `remove_vault`은 항상 거절하므로 반환된 터미널 명령을 사용자에게 전달한다.
- 사용자가 요청하지 않으면 `.markdown-notes-agentignore`와 `VAULT.md`를 고치지 않는다. 두 파일은 사용자가 정한 규칙이다.
