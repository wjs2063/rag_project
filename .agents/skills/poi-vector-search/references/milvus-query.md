# Milvus POI 검색 설계

## 스키마와 인덱스

- `GEOMETRY`와 `RTREE`는 Milvus 2.6.4 이상 기능이다. 서버와 `pymilvus`의 실제 조합을 확인한다. 위치는 명시적 `DataType.GEOMETRY` 필드에 WKT `POINT(경도 위도)`로 저장한다. 동적 필드에 지오메트리를 넣지 않는다.
- 안정적인 POI ID를 기본 키로 사용하고, 카테고리 코드·출처·갱신 시각 등 검색에 필요한 속성은 명시적 스칼라 필드로 고려한다. 벡터 차원과 거리 척도는 선택한 임베딩 모델과 맞춘다.
- `geo`에 `RTREE`, 벡터에 적합한 벡터 인덱스, 카테고리에 스칼라 인덱스를 검토한다. 카테고리 종류가 적으면 `BITMAP`, 많으면 `INVERTED` 또는 자동 인덱스가 후보이며 실제 분포로 비교한다. `RTREE`가 없으면 공간 필터가 전체 스캔으로 돌아갈 수 있다.

## 질의 의미

`ST_DWITHIN(geo, 'POINT(127.0 37.5)', 1000)`은 중심점에서 1,000m 이내를 뜻한다. 점의 두 좌표는 경도, 위도 순서다. 카테고리 필터는 예를 들어 `category == 'cafe'`이며 두 조건을 `and`로 결합할 수 있다. 이 식은 설명용 상수 예제다. 실제 API에서는 유한한 좌표/반경 검증과 카테고리 허용값 또는 안전한 매개변수 처리를 거친다.

- 위치와 카테고리만 조회할 때는 `query` 성격의 결과 제한과 페이징을 설계한다.
- 의미 검색은 검증된 결합 필터를 `search(filter=...)`에 넣어 조건에 맞는 POI 중 벡터 상위 K를 반환한다. 상위 K는 거리순 POI가 아니다. 거리순이 필요하면 별도 계산·정렬 계약과 비용을 설계한다.
- 표준 필터링은 먼저 필터로 후보를 줄인다. 복잡한 필터에서 `iterative_filter`가 대안이 될 수 있으나, 한 건씩 판정해 선택도가 낮을 때 느릴 수 있다. 반경·카테고리 분포별로 비교하고 기본값을 고른다.
- 필터가 벡터 검색 결과에 미치는 영향은 작은 실제 데이터셋으로 확인한다. 반경 경계, 0건, 여러 카테고리, 좌표 순서, 빈/누락 위치를 포함한다.

## 공식 자료

- [GEOMETRY 필드](https://milvus.io/docs/geometry-field.md)
- [기하학 연산자와 ST_DWITHIN](https://milvus.io/docs/ko/geometry-operators.md)
- [RTREE 인덱스](https://milvus.io/docs/ko/rtree.md)
- [필터링된 검색](https://milvus.io/docs/ko/filtered-search.md)
- [BITMAP 인덱스](https://milvus.io/docs/bitmap.md)
- [스칼라 인덱스](https://milvus.io/docs/scalar_index.md)
