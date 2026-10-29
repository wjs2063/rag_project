# 공공 POI 검색 서비스

EPSG:5179 SHP/DBF 원본을 `NF_ID`로 조인하고, 분류 허용목록을 적용한 뒤 512차원 임베딩과 함께 Milvus 2.6에 적재한다. 검색 API는 한국어 BM25와 벡터 검색을 RRF로 결합하며 두 경로에 같은 반경·분류 필터를 먼저 적용한다. 점수는 검색 관련도이고 거리는 별도 미터 값이다.

## 준비

```bash
uv sync --extra dev
cp .env.sample .env
cp config/source_manifest.example.json config/source_manifest.json
```

`.env`에 로컬 MinIO 자격 증명과 `OPENAI_API_KEY`를 채운다. 기존 `POI_OPENAI_API_KEY`도 읽을 수 있다. `config/source_manifest.json`의 ZIP 경로, 내부 멤버 경로, DBF 필드명을 실제 원본에 맞게 수정한다. `poi inspect-zip /path/to/archive.zip`으로 멤버 이름과 DBF 헤더 건수를 확인할 수 있다. 각 지역의 SHP, SHX, DBF는 동일한 ZIP에 있어야 한다. 주소는 `auxiliary`에 넣고 `NF_ID`로, 지역별 XLSX 별칭은 `POIID`로 연결한다. XLSX 보조 자료에는 실제 시트·헤더 행·열 이름을 지정한다. 실제 DBF 인코딩을 확인해 지정한다.

`config/categories.csv`는 원본 분류표 **16열의 12자리 실제 입력 코드 2,331개**와 4개 상위 그룹을 담는다. `config/visit_policy_v1.json`은 방문 목적 허용목록의 생성 규칙을 버전 관리한다. 정책 초안은 기관·지명, 3차 산업 상호, 관광·문화·체육 시설과 공항·정류장·역·주차장·휴게소를 포함하고 1·2차 산업 및 선형·경계·기반시설을 제외한다. 업무 기준이 바뀌면 정책을 검토한 뒤 `poi catalog-build`로 CSV를 다시 생성한다. 허용 항목이 없으면 준비 과정과 API 시작이 중단된다. 북한 지역은 manifest에서 `"exclude": true`로 표시한다.

```bash
poi catalog-build 'data/source/DownloadMap(20261029) (1)/SEOUL_POI.zip' 'TN_ASORTCL/TN_ASORTCL_v.1.4.xlsx' config/visit_policy_v1.json config/categories.csv
```

이 자료는 `data/source/DownloadMap(20261029)`의 지역별 ZIP에 XLSX 대조표가, `data/source/DownloadMap(20261029) (1)`의 ZIP에 SHP·DBF·별칭·분류표가 있다. 서울·경기 manifest는 실제 멤버와 필드명을 사용한다. 원본 구조를 확인할 때는 `poi inspect-zip ZIP --headers`를 쓴다.
`TN_POI`에는 별도 `POIID` 열이 없으므로 이 자료의 별칭 `poiid`를 POI의 `NF_ID`에 연결한다. `TN_POI`는 CP949이고 다국어 `TN_FRGN`은 이번 한국어 서비스에서 제외했다.

## 로컬 Milvus와 적재

```bash
docker compose up -d --wait
poi prepare config/source_manifest.json data/seoul-gyeonggi.sqlite3
poi sample data/seoul-gyeonggi.sqlite3 data/sample.sqlite3 --regions seoul gyeonggi --per-region 2000 --seed poi-sample-20261029-v1
poi batch-write data/sample.sqlite3 data/batch --run-id sample-20261029-v1 --max-rows 4000
poi collection-create pois_sample_20261029 --nlist 64
# 원본 텍스트의 외부 전송 조건을 확인한 뒤에만 실행:
poi batch-submit data/batch --run-id sample-20261029-v1 --max-shards 1 --external-transfer-authorized
poi batch-collect data/sample.sqlite3 data/batch --run-id sample-20261029-v1
poi batch-status data/sample.sqlite3 data/batch --run-id sample-20261029-v1
poi batch-check data/sample.sqlite3 NF_ID
# 실패한 ID가 있을 때만 새 run에 담는다.
# poi batch-write data/sample.sqlite3 data/batch --run-id sample-retry-001 --max-rows 4000
poi bulk-export data/sample.sqlite3 data/bulk-files.json
poi import-start data/bulk-files.json pois_sample_20261029
poi import-status JOB_ID
poi alias-switch pois_sample_20261029 --job-id JOB_ID --expected-rows 4000 --initial
docker compose --profile serving up -d --build api
curl -f http://127.0.0.1:18000/readyz
```

`batch-write`는 SQLite에 임베딩이 없는 `NF_ID`만 `data/batch/<run_id>/` 아래에 기록한다. **`batch-submit`은 원본 텍스트의 외부 전송 조건을 확인하기 전에는 실행하지 않는다.** `--external-transfer-authorized`는 그 확인을 기록하는 명시적 플래그다. 기본값은 run당 4만 건, `batch-submit`은 호출당 샤드 1개 제출이다. `run.json`에 업로드 ID·Batch ID·상태를 원자적으로 기록하고, `batch-collect`는 완료·만료·취소 배치의 성공 결과를 `NF_ID`별로 SQLite에 저장한다. 배치가 진행 중이면 `batch-collect`를 나중에 다시 실행한다. 실패한 ID는 다음 run에서 다시 선택된다. 미완료 run이 있으면 새 run을 만들 수 없다. 제출 응답을 받지 못해 `creating` 상태가 남은 경우 자동 재제출을 막는다. OpenAI에서 기존 Batch ID를 확인한 뒤 `poi batch-attach data/batch --run-id RUN --file embeddings-00000.jsonl --batch-id BATCH_ID`로 연결한다. 원격 Batch가 생성되지 않았음을 확인한 경우에만 `poi batch-release data/batch --run-id RUN --file embeddings-00000.jsonl --confirmed-no-batch`로 해당 샤드를 다시 제출 가능하게 한다. 벡터 건수가 선별 건수와 다르면 `bulk-export`가 중단된다. 재시도할 오류는 해당 run의 `collection-report.json`과 `*.errors.jsonl`에서 확인한다. 별칭 전환 명령은 완료된 import job·행 수·인덱스·로드 상태와 공간 필터를 적용한 벡터·BM25 검색을 확인한다. 이후 재구축은 새 컬렉션에 적재·검증하고 `--initial` 없이 같은 명령으로 전환한다. 로컬 Compose는 기능 확인용이며 1천만~5천만 건 성능 시험에는 분산 Milvus가 필요하다.

`data/seoul-gyeonggi.report.json`에 지역별 원본/선별 건수, 중복 `NF_ID`, 누락 조인, 미분류·제외 분류, 좌표 오류 및 XLSX 건수 차이가 기록된다. `sample`은 각 지역의 적격 POI에서 시드가 고정된 해시 순서로 2,000건씩 선택하고 별도 보고서를 쓴다. 전체 원본 ZIP을 일괄 추출하지 않고 선언된 멤버만 지역별 임시 폴더에 복사한다. 보조 테이블과 POI는 SQLite에 저장한다. 메모리를 과도하게 쓰지 않도록 Batch 요청을 최대 4만 건·150MB 단위로 나눈다. 실제 최대 RSS는 원본 표본으로 측정해야 한다.

## API

```bash
curl -X POST http://localhost:18000/v1/pois/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"국수집","longitude":127.0,"latitude":37.5,"radius_m":1000,"category_ids":[],"limit":10}'
```

`GET /v1/categories`는 버전이 지정된 분류 ID와 상위 ID를 제공한다. 검색은 필수 검색어·중심점·반경 1m~30km를 받고 최대 20건을 반환한다. `score`는 Milvus RRF 점수이며 큰 값이 더 관련 있다. `distance_m`은 중심점과의 대권 거리다. 임베딩 모델·차원·입력 텍스트 버전은 적재 레코드에 기록한다. 단건 재처리는 `poi upsert COLLECTION row.json --external-transfer-authorized`, 조회·삭제는 `poi get` / `poi delete`로 수행한다.

## 검증과 출시 기준

2026-10-29 로컬 표본 검증 결과:

| 항목 | 서울 | 경기 |
| --- | ---: | ---: |
| SHP/DBF 원본 및 XLSX 대조 | 1,583,283 | 2,096,515 |
| 방문 목적 정책 통과 | 1,222,905 | 1,459,846 |
| 고정 시드 표본 | 2,000 | 2,000 |

분류표의 실제 코드 2,331개를 매핑하고 1,641개를 허용했다. 표본 `NF_ID` 4,000개는 모두 고유하며 주소와 좌표가 유효하다. OpenAI Batch 4,000건 완료·오류 0, Milvus bulk import 4,000행 완료, 다섯 인덱스와 `/readyz`를 확인했다. 1·5·30km, 별칭, 상위 분류, 서울·경기 경계 횡단 검색이 통과했다. 재현에 필요한 자세한 수치는 `data/validation-summary.json`과 `data/functional-validation.json`에 기록되어 있으며 `data/`는 Git에서 제외된다. 표본 응답 시간은 기능 확인용이고 100 RPS·p95 1초 목표의 판정값이 아니다.

`uv run pytest -q`로 `NF_ID`/`POIID` 조인, 좌표 변환, 분류 제외, XLSX 건수 대조, 필터 입력 검증을 확인한다. 실제 원본을 연결한 뒤 보고서의 누락·중복·미분류 건수를 검토하고, 반경 경계·행정 경계 횡단·별칭 질의 표본을 검색한다. SQ8과 정확 검색의 대표 질의 재현율을 비교해 `nlist`·`nprobe`를 조정한다.

검색 질의 JSONL을 준비한 뒤 `python scripts/load_search.py scenarios.jsonl --rps 100 --seconds 900`으로 API 전체 p95를 측정한다. 1·5·30km, 흔한·드문 분류와 일반·별칭 질의를 혼합한다. 동시에 Milvus QueryNode 메모리와 API 작업자 RSS를 관측한다. 고유 질의 100 RPS라면 최소 6,000 임베딩 요청/분이므로 계정 RPM·TPM과 외부 API 지연 시간을 먼저 확인한다. 전체 API p95가 1초 미만이고 필요한 처리량을 유지해야 출시 기준을 충족한다. 성능 기준을 충족했다는 결과는 실제 규모 시험 전에는 주장할 수 없다.

기준 문서: [Milvus 2.6 RTREE](https://milvus.io/docs/v2.6.x/rtree.md), [BM25](https://milvus.io/docs/v2.6.x/full-text-search.md), [bulk import](https://milvus.io/docs/v2.6.x/import-data.md), [OpenAI Batch](https://developers.openai.com/api/docs/guides/batch).
