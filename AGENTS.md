# 프로젝트 지침

이 저장소는 공공 POI 1천만~5천만 건의 Milvus 벡터 검색 서비스를 개발한다. Python으로 데이터를 가공·주입하고 FastAPI로 AI 연계 검색을 제공한다. 기본 검색 조건은 반경과 카테고리다.

프로젝트 스킬은 `skills/`에 있다. 관련 작업을 시작할 때 [POI 벡터 검색 스킬](skills/poi-vector-search/SKILL.md)을 읽고, 필요한 전문 스킬의 `SKILL.md`만 추가로 읽는다. Milvus 일반 작업은 `skills/milvus/SKILL.md`, Python/FastAPI/Docker 작업은 해당 이름의 스킬을 참고한다. PostgreSQL과 MongoDB 스킬은 해당 저장소를 실제로 선택하거나 사용할 때만 적용한다.

프로젝트 스킬의 관리 위치는 루트 `skills/`다. `.agents/skills/poi-vector-search`에 남은 초기화 틀은 미완성이므로 사용하지 않는다.
