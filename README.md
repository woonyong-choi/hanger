<h1 align="center">Markdown Notes Agent</h1>

<p align="center">
  Search and edit local Markdown notes through MCP<br>
  <a href="https://pypi.org/project/markdown-notes-agent/"><img src="https://img.shields.io/badge/version-v0.3.0-blue" alt="PyPI version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a><br>
  한국어 | <a href="README.en.md">English</a>
</p>

## 빠른 시작

macOS 또는 Linux, Python 3.12 이상, [uv](https://docs.astral.sh/uv/)가 필요합니다. 모델은 로그인한 Claude Code 또는 Codex CLI를 통해 호출합니다.

```sh
uv tool install markdown-notes-agent
markdown-notes-agent setup
markdown-notes-agent vault add ~/notes
```

`~/notes`는 이미 존재하고 읽기·쓰기 가능한 Markdown 노트 폴더로 바꾸세요. Markdown Notes Agent가 관리하는 노트 폴더를 vault이라고 부릅니다.

등록하면 루트에 `.markdown-notes-agentignore`와 `VAULT.md`가 없을 때만 기본 파일을 만듭니다. 기존 파일은 보존합니다. 기본 무시 파일에는 제외 패턴이 없어 모든 Markdown 노트가 색인 대상이며, `VAULT.md` 자체는 색인하지 않습니다.

`markdown-notes-agent setup`은 모델 CLI를 찾고 Elasticsearch와 한국어 분석기를 설치합니다. Claude Code가 있으면 먼저 선택하고, 없으면 Codex를 선택합니다. 모델 CLI가 없으면 설치를 멈춥니다. CLI 로그인과 실제 모델 응답은 이 단계에서 검사하지 않습니다.

### AI 도구 연결

사용하는 도구의 연결 명령을 실행하세요. 연결된 MCP 서버에 요청하면 백그라운드 서비스가 시작됩니다.

<details>
<summary>Claude Code 연결</summary>

```sh
claude mcp add --scope user markdown-notes-agent -- markdown-notes-agent serve
```

</details>

<details>
<summary>Codex 연결</summary>

```sh
codex mcp add markdown-notes-agent -- markdown-notes-agent serve
```

</details>

### 첫 요청

연결한 AI 도구에 검색을 요청하세요. 검색어는 등록한 노트의 내용에 맞춰 바꾸세요.

```text
등록한 노트 폴더에서 멱등성 키를 다룬 노트를 찾아 줘.
```

검색은 노트를 바꾸지 않습니다. 찾은 내용을 고치려면 변경을 따로 요청하세요.

```text
방금 찾은 노트에서 중복된 문장을 정리해 줘. 원문 근거와 폴더 규칙은 유지해 줘.
```

변경 요청의 `accepted`는 접수 상태입니다. AI 도구는 `job_status`로 최종 결과와 다음 행동을 확인합니다. 원문 변경으로 멈춘 작업은 사용자 동의를 받아 다시 검토합니다. 사용 지침은 [skills/markdown-notes-agent/SKILL.md](skills/markdown-notes-agent/SKILL.md)에 있습니다.

<details>
<summary>업데이트와 소스 설치</summary>

배포판은 다음 명령으로 업데이트합니다.

```sh
uv tool upgrade markdown-notes-agent
markdown-notes-agent setup
```

이름 변경 전 [Hanger 0.2.0 태그](https://github.com/woonyong-choi/markdown-notes-agent/releases/tag/v0.2.0)는 보존합니다. 기존 설치에서 옮길 때는 기존 서비스를 중지하고 상태와 검색 데이터, 요청 키, 복구 파일을 보존한 채 새 경로로 이전해야 합니다. 기존 폴더 규칙과 무시 규칙의 내용을 새 파일 이름으로 옮기고, 명시 관계 식별자와 MCP 연결도 새 이름에 맞춘 뒤 실행하세요. 이 버전은 이전 이름의 명령이나 모듈을 자동으로 연결하지 않습니다.

소스는 다음 명령으로 설치합니다.

```sh
uv tool install git+https://github.com/woonyong-choi/markdown-notes-agent
```

</details>

## 해결하는 문제

Markdown 노트가 쌓이면 같은 설명이 여러 파일에 흩어집니다. Markdown Notes Agent는 AI 도구가 노트를 검색하고, 폴더 규칙에 따라 변경안을 만들고 검토하도록 연결합니다. 기존 노트는 근거로 읽은 원문 구간을 바꾸며, 원문이 달라졌는지 반영 전에 다시 확인합니다.

노트가 몇 개뿐이고 직접 고치는 편이 빠르면 편집기로 충분합니다.

## 작동 방식

그림은 모델을 거치는 변경 요청의 흐름입니다. 검색은 조회로 끝나고, `collect`는 모델 없이 내용을 그대로 `inbox/`에 보관합니다.

<img src="docs/assets/change-request.svg" width="360" alt="수정안을 별도로 검토하고 원문을 재검사한 뒤 파일마다 반영하거나 충돌로 멈춥니다">

1. 서비스가 변경 요청을 대기열에 저장하고, 관련 노트와 폴더 규칙을 모아 초안을 만듭니다.
2. 모델을 별도로 호출해 초안을 검토합니다. 검토에서 거절하면 이유를 반영해 다시 작성합니다. 다른 모델이나 사람의 승인을 뜻하지 않습니다.
3. 수정 대상과 참고 원문이 그대로인지 확인한 뒤 파일마다 반영합니다. 기본 모드(`normal`)는 원문이 달라지면 충돌로 멈춥니다. `bypass` 모드는 현재 원문으로 수정안을 다시 만듭니다. 강제로 덮어쓰지 않습니다.

여러 파일 전체를 한 번에 되돌리는 방식은 아닙니다. 도중에 충돌하거나 취소해도 이미 반영한 파일은 남습니다.

### 규칙과 검색

등록한 폴더의 변경을 감시해 검색 색인을 갱신합니다. `VAULT.md`의 폴더 규칙과 `.markdown-notes-agentignore`의 무시 패턴을 따릅니다. `write: false`인 폴더와 무시된 파일은 변경하지 않습니다.

<details>
<summary>폴더별 규칙 파일</summary>

`VAULT.md`는 폴더마다 둘 수 있고 그 폴더와 하위 폴더에 적용됩니다. 맨 위 `---` 사이의 설정이 충돌하면 노트에 가장 가까운 파일의 값을 적용합니다. 본문 규칙은 루트부터 노트에 가까운 폴더 순서로 모두 모델에 전달합니다. 한 노트 경로에 적용할 규칙이나 요청 전체에서 모은 규칙의 본문이 합계 32KiB를 넘으면 `rules_limit` 오류로 요청을 중단합니다. 한도를 맞추려고 일부 규칙을 생략하지는 않습니다. 다른 AI 도구의 규칙 파일(`AGENTS.md`, `CLAUDE.md` 등)과 섞이지 않도록 이름을 따로 씁니다.

| 설정 | 기본값 | 뜻 |
|---|---|---|
| `write` | `true` | `false`면 이 폴더의 노트 생성·수정·삭제 차단 |
| `tree` | 빈 값(vault 전체가 트리 하나) | 키워드 트리 이름. 서로 다른 이름의 폴더 사이 개념 연결 제외 |
| `keywords` | `true` | `false`면 이 폴더 노트의 키워드 트리 제외. 색인·검색 유지 |
| `merge` | 빈 값 | `auto`면 같은 중심 질문에 답하는 두 노트의 병합 작업 자동 등록 |

```markdown
---
write: false
---

이 폴더의 노트는 확정본입니다. 인용만 하고 고치지 마세요.
```

</details>

<details>
<summary>키워드 트리의 생성과 병합</summary>

노트 하나가 개념 하나입니다. 상위 개념은 사용자가 쓴 것이 먼저입니다. 머리말의 `parent`(slug, 파일 이름, 링크 모두 가능)와, 목록 줄을 링크로 시작하는 색인 노트(그 노트가 링크한 노트들의 상위)가 그렇습니다. 사용자가 정하지 않은 자리는 모델이 노트와 비슷한 노트 20개를 보고 개념 이름, 다른 이름, 정의, 상위·연관·선행 개념을 제안하고, 프로그램이 검사해 저장합니다.

- 상위 개념은 같은 트리에 있어야 하고 순환을 만들 수 없습니다.
- 후보에 없는 새 상위 이름은 같은 이름의 노트나 개념에 합쳐지고, 없으면 두 번째 노트가 같은 이름을 제안할 때 개념으로 만들어집니다. 자식이 하나 이하로 줄면 다시 대기로 돌아갑니다.
- 직계 자식이 15개를 넘는 개념은 모델이 중간 묶음으로 나눕니다.
- 노트를 지우면 그 자식은 한 단계 위로 올라가고 다시 판단합니다.
- 같은 중심 질문에 답하는 노트는 결과에 표시하고, 두 폴더 모두 `merge: auto`면 병합 작업을 대기열에 넣습니다. 병합도 다른 변경처럼 검토를 거칩니다.
- 개념 이름과 다른 이름은 검색에 보조적으로 사용합니다. 본문에 없는 다른 이름으로도 노트를 찾습니다. 트리에 등록된 노트의 검색 결과와 초안 작성에는 트리 위치가 함께 들어갑니다.

트리는 사용자 요청이 없을 때 백그라운드에서 한 노트씩 만듭니다. 진행은 `markdown-notes-agent keywords`나 `list_vaults`로 봅니다.

</details>

<details>
<summary>무시 규칙 변경의 승인</summary>

`.markdown-notes-agentignore`의 패턴을 고치거나 파일을 지우면 바로 적용하지 않고 결정(decision)으로 기록합니다. 주석만 바꾼 수정은 색인에 영향이 없어 바로 적용합니다. 결정이 남아 있는 동안 Markdown Notes Agent는 검색과 변경 요청을 모두 거절하고 먼저 답을 요구합니다. vault 폴더가 없거나 비어 있으면(드라이브 연결 해제 등) 그 vault의 결정은 철회하고, 폴더가 돌아오면 다시 묻습니다. 파일 감시와 색인 갱신은 계속 돌아갑니다. 색인에서 빠진 노트는 다시 넣어도 같은 키워드와 링크를 보장할 수 없어서 이렇게 확인합니다. 지운 경우 승인하면 기본 파일을 다시 만들고, 거절하면 이전 파일을 되돌립니다.

</details>

### 도구와 설정

필요한 도구, 명령, 설정을 펼쳐 확인할 수 있습니다.

<details>
<summary>MCP 도구 목록</summary>

MCP 서버로 연결하면 아래 도구를 씁니다. 안내 문구와 질문은 영어로 돌려주며, AI 도구에 사용자의 언어로 번역해 전달하라고 지시합니다.

| 도구 | 하는 일 |
|---|---|
| `list_vaults` | 등록된 vault과 대기 중인 결정 확인. 없으면 폴더를 물어보라는 안내 |
| `register_vault` | 절대 경로로 vault 등록 |
| `remove_vault` | 항상 거절하고 사용자가 실행할 터미널 명령을 안내 |
| `list_decisions`, `decide` | 대기 중인 결정을 보여 주고 사용자의 답을 적용 |
| `search_documents` | 검색. 트리에 등록된 노트의 결과에 키워드 위치 포함 |
| `read_document` | 현재 원문과 해시 읽기 |
| `keyword_tree` | 트리 꼭대기, 또는 한 개념의 이름·정의·위치·상위·하위·연관·선행 개념 |
| `issue_key`, `request_change`, `collect` | 작성·수정·정리 요청, 파일이나 글을 그대로 `inbox/`에 보관 |
| `job_status`, `cancel_job`, `resolve_conflict` | 진행 확인, 취소, 원문 변경으로 멈춘 작업 재검토 |

vault이 둘 이상이면 요청마다 `vault`에 id나 그 vault 안의 경로를 넘깁니다. AI 도구에 줄 사용 지침은 [skills/markdown-notes-agent/SKILL.md](skills/markdown-notes-agent/SKILL.md)에 있습니다.

</details>

<details>
<summary>터미널 명령과 설정</summary>

| 명령 | 하는 일 |
|---|---|
| `markdown-notes-agent setup [--reinstall]` | 모델 공급자 확인, 검색 엔진 설치 |
| `markdown-notes-agent provider` | 역할(초안·검토·키워드)별로 실행할 CLI와 모델 확인 |
| `markdown-notes-agent vault add <폴더>` / `list` / `remove <id\|경로> [--yes]` | vault 등록·조회·제거. 제거 시 노트·규칙 파일 보존, 색인·작업 기록 삭제 |
| `markdown-notes-agent ignore show` / `add <패턴>` / `remove <패턴>` `[--vault] [--yes]` | 무시 규칙 확인·변경. 영향받는 노트 수 표시와 승인 요청 |
| `markdown-notes-agent decisions` / `markdown-notes-agent decide <id> approve\|decline` | 대기 중인 결정 확인과 답 |
| `markdown-notes-agent config list` / `get <키>` / `set <키> <값>` / `unset <키>` | 설정 확인, 변경, 기본값 복원 |
| `markdown-notes-agent search <질의> [--vault] [--folder]` | 터미널에서 검색 |
| `markdown-notes-agent keywords [개념] [--vault]` | 키워드 트리 진행 상황과 꼭대기, 또는 한 개념(노트 경로나 이름) |
| `markdown-notes-agent stop` | 백그라운드 서비스 중지. 다음 요청 시 재시작 |

주요 설정:

| 키 | 기본값 | 뜻 |
|---|---|---|
| `model.provider` | 자동 | `claude` 또는 `codex`. 비우면 설치된 것 중 Claude Code 우선 |
| `model.name` | 자동 | 비우면 Claude Code는 `haiku`, Codex는 `gpt-6-luna` |
| `model.effort` | CLI 기본값 | `low`, `medium`, `high` |
| `models.draft.*`, `models.review.*`, `models.keywords.*` | `model.*` | 초안·검토·키워드 역할별 설정. 빈 값은 `model.*` 적용 |
| `search.candidates` | 20 | 검색 후보 수 |
| `service.idle_minutes` | 30 | 요청이 없을 때 서비스가 스스로 끝나기까지의 시간 |
| `elasticsearch.port` | 19200 | 관리하는 검색 엔진의 포트 |

설정, vault 목록, 적용된 무시 규칙, 결정은 `~/.local/share/markdown-notes-agent/state/markdown-notes-agent.db`에 저장합니다. 설정 변경은 서비스가 다시 시작될 때 적용됩니다(`markdown-notes-agent stop`).

</details>

## 상태

[PyPI의 0.3.0 배포판](https://pypi.org/project/markdown-notes-agent/0.3.0/)은 여러 vault 관리, 폴더 규칙, 검색·키워드 트리, 변경 요청·검토·복구를 지원합니다.

| 있는 것 | 아직 없는 것 | 다음 | 측정 |
|---|---|---|---|
| 검색과 키워드 트리 | 현재 트리의 검색 누락 해결 판정 | [검색 누락 수정](https://github.com/woonyong-choi/markdown-notes-agent/issues/7) | 현재 트리 재측정 전 |
| 작성·수정·정리와 검토 | 전체 정리 과정의 비용·품질 비교 | [전체 과정 측정](https://github.com/woonyong-choi/markdown-notes-agent/issues/8) | 전체 비교 미완료 |

1.0 전에는 형식과 명령이 예고 없이 바뀔 수 있습니다.

## 문서

- [폴더 규칙](docs/configuration.md)
- [검색 응답과 한도](docs/search.md)
- [의미 메타데이터 형식](docs/data.md)

## 개발

```sh
uv sync --locked
uv build
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

검색 통합 검사는 `MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL`에 Elasticsearch와 Nori 분석기를 지정해 실행합니다. 관리형 서버 수명 검사에는 `MARKDOWN_NOTES_AGENT_TEST_ES_HOME`으로 Nori가 설치된 Elasticsearch 경로를 지정합니다. 지정하지 않은 검사는 건너뜁니다. 실제 모델 호출 검사는 명시적으로 선택한 경우에만 실행합니다.

<details>
<summary>그림 다시 만들기</summary>

[change-request.diagramplay](docs/assets/change-request.diagramplay)가 원본이고 SVG는 생성물입니다. [DiagramPlay ebdc3e9](https://github.com/woonyong-choi/diagramplay/tree/ebdc3e9c84c3d5443af7f1b6b5d9b2a18e5002ca)의 CLI 경로를 `DIAGRAMPLAY_CLI`에 지정해 생성합니다.

```sh
node "$DIAGRAMPLAY_CLI" check docs/assets/change-request.diagramplay --strict
node "$DIAGRAMPLAY_CLI" render docs/assets/change-request.diagramplay --strict --out docs/assets
```

</details>

## 라이선스

[MIT](LICENSE)
