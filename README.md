<h1 align="center">Hanger</h1>

<p align="center">
  Local Markdown document automation with durable workflows<br>
  한국어 | <a href="README.en.md">English</a><br>
  <a href="#빠른-시작">빠른 시작</a> · <a href="#작동-방식">작동 방식</a> · <a href="#ai-도구에서-쓰기">AI 도구에서 쓰기</a>
</p>

> [!NOTE]
> 개발 중입니다. 배포판은 없고 소스에서 빌드해 실행합니다.

## 빠른 시작

macOS나 Linux, Python 3.12 이상, [uv](https://docs.astral.sh/uv/), 로그인한 Claude Code(`claude`)나 Codex(`codex`)가 필요합니다. API 키는 쓰지 않고 이미 가입한 구독으로 모델을 실행합니다.

```sh
uv tool install git+https://github.com/woonyong-choi/hanger
hanger setup
claude mcp add --scope user hanger -- hanger serve
hanger closet add ~/notes
```

`hanger setup`은 아무것도 묻지 않습니다. 모델 공급자를 찾고(Claude Code가 있으면 Claude Code, 없으면 Codex) 검색 엔진(Elasticsearch와 한국어 분석기)을 내려받아 설치합니다. 공급자가 하나도 없으면 설치를 멈추고 이유를 알려 줍니다. 노트 폴더는 마지막 줄처럼 터미널에서 등록해도 되고, AI 도구가 처음 쓸 때 사용자에게 물어 등록해도 됩니다.

Codex에 연결할 때는 `codex mcp add hanger -- hanger serve`를 씁니다.

업데이트:

```sh
uv tool upgrade hanger
hanger setup
```

## 해결하는 문제

Markdown 노트가 쌓이면 같은 주제의 노트가 여러 곳에 생기고, AI 도구에 정리를 맡기면 근거 없이 고치거나 손대면 안 되는 폴더까지 바꿉니다. Hanger는 AI 도구와 노트 폴더 사이에서 검색, 수정 계획, 검토, 반영을 맡습니다. 노트를 고칠 때는 읽은 원문 구간만 바꾸고, 사용자가 정한 규칙 파일(`HANGER.md`)과 무시 파일(`.hangerignore`)을 따르며, 원문이 그사이 바뀌면 덮어쓰지 않고 멈춥니다.

노트가 한 폴더에 몇 개뿐이고 직접 고치는 편이 빠르면 Hanger 없이 편집기로 충분합니다.

## 작동 방식

1. **closet 등록**: Hanger가 관리하는 노트 폴더를 closet(옷장)이라고 부릅니다. 여러 개를 등록할 수 있고, 등록된 closet 안이나 바깥을 감싸는 폴더는 중복이라 거절합니다. 등록하면 루트에 `.hangerignore`와 `HANGER.md`가 없을 때만 기본 파일을 만듭니다.
2. **색인**: 백그라운드 서비스가 파일 변경을 감시해 검색 색인을 최신으로 유지합니다. `.hangerignore`(`.gitignore` 문법)에 맞는 파일과 `HANGER.md`는 색인하지 않습니다. 기본값은 숨김 파일과 폴더(`.obsidian`, `.git` 등)를 제외합니다.
3. **요청**: AI 도구가 검색하거나 작성·수정·정리를 요청합니다. 변경 요청은 대기열에 들어가고 서비스가 처리합니다.
4. **초안과 검토**: 관련 노트와 그 폴더들에 적용되는 `HANGER.md` 규칙을 모델에 함께 주고 수정안을 만든 뒤, 다른 호출로 검토합니다. 검토에서 거절되면 이유를 반영해 다시 씁니다.
5. **반영**: 원문이 그대로인지 확인한 뒤에만 파일을 바꿉니다. `write: false`인 폴더와 무시된 파일은 바꾸지 않습니다.

**규칙 파일 `HANGER.md`**: 폴더마다 둘 수 있고 그 폴더와 하위 폴더에 적용됩니다. 맨 위 `---` 사이의 설정은 가장 가까운 파일의 값이 이기고, 본문 규칙은 루트부터 가까운 폴더 순서로 모두 모델에 전달됩니다. 다른 AI 도구의 규칙 파일(`AGENTS.md`, `CLAUDE.md` 등)과 섞이지 않도록 이름을 따로 씁니다.

```md
---
write: false
---

이 폴더의 노트는 확정본입니다. 인용만 하고 고치지 마세요.
```

**무시 규칙 변경은 사용자가 승인합니다**: `.hangerignore`를 고치거나 지우면 바로 적용하지 않고 결정(decision)으로 기록합니다. 결정이 남아 있는 동안 Hanger는 검색과 변경 요청을 모두 거절하고 먼저 답을 요구합니다. 파일 감시와 색인 갱신은 계속 돌아갑니다. 색인에서 빠진 노트는 다시 넣어도 같은 키워드와 링크를 보장할 수 없어서 이렇게 확인합니다. 지운 경우 승인하면 기본 파일을 다시 만들고, 거절하면 이전 파일을 되돌립니다.

## AI 도구에서 쓰기

MCP 서버로 연결하면 아래 도구를 씁니다. 안내 문구와 질문은 영어로 돌려주며, AI 도구에 사용자의 언어로 번역해 전달하라고 지시합니다.

| 도구 | 하는 일 |
|---|---|
| `list_closets` | 등록된 closet과 대기 중인 결정 확인. 없으면 폴더를 물어보라는 안내 |
| `register_closet` | 절대 경로로 closet 등록 |
| `remove_closet` | 항상 거절하고 사용자가 실행할 터미널 명령을 안내 |
| `list_decisions`, `decide` | 대기 중인 결정을 보여 주고 사용자의 답을 적용 |
| `search_documents`, `read_document` | 검색과 읽기 |
| `issue_key`, `request_change`, `collect` | 작성·수정·정리 요청, 파일이나 글을 그대로 `inbox/`에 보관 |
| `job_status`, `cancel_job`, `resolve_conflict` | 진행 확인, 취소, 원문 변경으로 멈춘 작업 재검토 |

closet이 둘 이상이면 요청마다 `closet`에 id나 그 closet 안의 경로를 넘깁니다. AI 도구에 줄 사용 지침은 [skills/hanger/SKILL.md](skills/hanger/SKILL.md)에 있습니다.

## 터미널 명령

| 명령 | 하는 일 |
|---|---|
| `hanger setup [--reinstall]` | 모델 공급자 확인, 검색 엔진 설치 |
| `hanger provider` | 역할(초안·검토)별로 실행할 CLI와 모델 확인 |
| `hanger closet add <폴더>` / `list` / `remove <id\|경로> [--yes]` | closet 등록, 목록, 제거. 제거해도 노트와 규칙 파일은 남고 색인과 작업 기록만 지웁니다 |
| `hanger ignore show` / `add <패턴>` / `remove <패턴>` `[--closet] [--yes]` | 무시 규칙 확인과 변경. 바뀌는 노트 수를 보여 주고 확인을 받습니다 |
| `hanger decisions` / `hanger decide <id> approve\|decline` | 대기 중인 결정 확인과 답 |
| `hanger config list` / `get <키>` / `set <키> <값>` / `unset <키>` | 설정 확인, 변경, 기본값 복원 |
| `hanger search <질의> [--closet] [--folder]` | 터미널에서 검색 |
| `hanger stop` | 백그라운드 서비스 중지. 다음 요청 때 다시 시작합니다 |

주요 설정:

| 키 | 기본값 | 뜻 |
|---|---|---|
| `model.provider` | 자동 | `claude` 또는 `codex`. 비우면 설치된 것 중 Claude Code 우선 |
| `model.name` | 자동 | 비우면 Claude Code는 `haiku`, Codex는 `gpt-6-luna` |
| `model.effort` | CLI 기본값 | `low`, `medium`, `high` |
| `models.draft.*`, `models.review.*` | `model.*` | 초안·검토 역할별로 따로 지정. 빈 값은 `model.*`을 따릅니다 |
| `search.candidates` | 20 | 검색 후보 수 |
| `service.idle_minutes` | 30 | 요청이 없을 때 서비스가 스스로 끝나기까지의 시간 |
| `elasticsearch.port` | 19200 | 관리하는 검색 엔진의 포트 |

설정, closet 목록, 적용된 무시 규칙, 결정은 `~/.local/share/hanger/state/hanger.db`에 저장합니다. 설정 변경은 서비스가 다시 시작될 때 적용됩니다(`hanger stop`).

## 상태

| 있는 것 | 아직 없는 것 |
|---|---|
| 여러 closet 관리, 무시 규칙과 승인 흐름, 폴더별 규칙 파일, 검색, 작성·수정·정리 요청과 검토, 구독 CLI(Claude Code, Codex) 실행 | 배포판, 키워드·개념 트리 기반 검색 |

1.0 전에는 형식과 명령이 예고 없이 바뀔 수 있습니다.

## 개발

```sh
uv sync
HANGER_TEST_SEARCH_URL=http://127.0.0.1:19200 uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

검색이 필요한 테스트는 `HANGER_TEST_SEARCH_URL`에 지정한 Elasticsearch(Nori 분석기 포함)를 씁니다. 없으면 건너뜁니다.
