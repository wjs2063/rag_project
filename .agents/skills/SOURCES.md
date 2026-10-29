# 가져온 스킬의 출처

2026-10-29에 아래 GitHub 커밋에서 프로젝트 `.agents/skills/`로 복사했다. `poi-vector-search`는 이 프로젝트를 위해 `skill-creator`로 새로 작성했다. 각 원본 저장소의 라이선스 전문은 `.agents/skills/licenses/`에 보관한다. Codex 검증기에 맞게 일부 원본의 `compatibility`·`tags`·`activation` 앞부분 필드를 `metadata`로 옮겼고, MongoDB 스킬의 깨진 상대 링크를 수정했다.

| 출처 | 고정 커밋 | 포함 범위 |
| --- | --- | --- |
| [zilliztech/milvus-skill](https://github.com/zilliztech/milvus-skill) | `7553e6c2b5940aef0f4a1aacc40d956745af0ac5` | 루트 `milvus` 스킬 |
| [wshobson/agents](https://github.com/wshobson/agents) | `0874a27b17a22e2b7c2e2bcd725768e0803cad06` | `plugins/python-development/skills/*` 16개, `fastapi-templates` 1개 |
| [microsoft/postgres-skills](https://github.com/microsoft/postgres-skills) | `9ba96abc800a574a0872f3d6b912ea8be1a6735e` | `plugin/skills/*` 2개 |
| [mongodb/agent-skills](https://github.com/mongodb/agent-skills) | `155bb9c4b42a9a3e019f47be0669de0e4547d1ea` | 중복 플러그인 경로를 제외한 루트 `skills/*` 8개 |
| [docker/skills](https://github.com/docker/skills) | `179c9cfd04c89ae50cd5d5d68b939797a8fd03d3` | 루트 `skills/*` 12개 |
