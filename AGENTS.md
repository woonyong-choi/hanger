# Markdown Notes Agent

Markdown 노트를 검색하고 폴더 규칙에 따라 변경하는 로컬 도구다.

## 구성

| 경로 | 내용 |
|---|---|
| `src/markdown_notes_agent/` | CLI, MCP, vault 관리, 검색, 문서 변경과 복구 |
| `tests/` | 기능·충돌·복구 검사 |
| `benchmarks/` | 검색·비용·문서 처리 측정 |
| `skills/markdown-notes-agent/` | 연결된 도구에서 쓰는 노트 작업 지침 |
| `docs/assets/` | README 이미지 |

## 명령

```sh
uv sync --locked
uv build
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

검색 통합 검사는 `MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL`에 Elasticsearch와 Nori 분석기를 지정해 실행한다. 지정하지 않으면 관련 검사를 건너뛴다.

## 규칙

- 커밋 전 명령 절의 검사를 통과하고 건너뛴 범위를 밝힌다.
- 동작, 계약, 설정을 바꾸면 같은 PR에서 관련 문서도 고친다.
- 작업 전 로컬 환경 지침으로 제공된 등록 Vault의 이 저장소 진입 문서를 읽는다. 필요한 규칙과 자료만 고른다. 연결 실패를 읽음으로 처리하지 않는다.
- 지침·맥락·신규 작업 문서는 등록 Vault의 지정 영역에서만 관리한다.
- 사용자의 명시적 요청·승인 없이 프로젝트에 추가 문서나 맥락 사본을 만들지 않는다. 코드·실행 필수 설정과 승인 문서는 보존한다.
