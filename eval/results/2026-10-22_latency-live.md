# latency-live

측정 모드: live

| 조건:경로 | 성공 수 | 실패 수 | 중앙값 ms | p90 ms |
| --- | --- | --- | --- | --- |
| cold:build | 3 | 0 | 1146.3558999821544 | 4428.609819989651 |
| cold:build_archive | 0 | 3 | None | None |
| cold:compare | 3 | 0 | 28486.082199960947 | 31979.597880039364 |
| cold:fact | 2 | 1 | 1229.021199978888 | 1242.1675199642777 |
| cold:filter | 3 | 0 | 11063.52990004234 | 13819.573980057612 |
| cold:narrative | 3 | 0 | 26418.555699987337 | 28556.701299967244 |
| cold:set_ops | 3 | 0 | 931.8696999689564 | 5255.537939979695 |
| warm:build | 3 | 0 | 94.99659994617105 | 94.99796004965901 |
| warm:build_archive | 0 | 3 | None | None |
| warm:compare | 3 | 0 | 15310.706499963999 | 15924.213139992207 |
| warm:fact | 3 | 0 | 8.849299978464842 | 9.093859978020191 |
| warm:filter | 3 | 0 | 145.838399999775 | 146.69128006789833 |
| warm:narrative | 3 | 0 | 6063.770700013265 | 6994.400219945237 |
| warm:set_ops | 3 | 0 | 30.720300041139126 | 33.14734008163214 |

| 조건:경로 | 단계 | 표본 수 | 중앙값 ms | p90 ms |
| --- | --- | --- | --- | --- |
| cold:build | routing_ms | 3 | 1050 | 4330.0 |
| cold:build | total_ms | 3 | 1146 | 4428.4 |
| cold:compare | generate_ms | 3 | 6044 | 6872.8 |
| cold:compare | retrieve_ms | 3 | 8932 | 10504.8 |
| cold:compare | routing_ms | 3 | 12957 | 16508.2 |
| cold:compare | total_ms | 3 | 28486 | 31979.6 |
| cold:fact | routing_ms | 2 | 1217.0 | 1229.8 |
| cold:fact | total_ms | 2 | 1229.0 | 1241.8 |
| cold:filter | routing_ms | 3 | 1051 | 4482.200000000001 |
| cold:filter | search_ms | 3 | 8877 | 9782.6 |
| cold:filter | total_ms | 3 | 11064 | 13820.0 |
| cold:narrative | generate_ms | 3 | 3558 | 3690.8 |
| cold:narrative | retrieve_ms | 3 | 21267 | 23974.2 |
| cold:narrative | routing_ms | 3 | 1465 | 5377.0 |
| cold:narrative | total_ms | 3 | 26419 | 28556.6 |
| cold:set_ops | routing_ms | 3 | 897 | 5220.2 |
| cold:set_ops | total_ms | 3 | 932 | 5255.2 |
| warm:build | routing_ms | 3 | 1 | 1.0 |
| warm:build | total_ms | 3 | 95 | 95.0 |
| warm:compare | generate_ms | 3 | 14686 | 15322.0 |
| warm:compare | retrieve_ms | 3 | 179 | 182.2 |
| warm:compare | routing_ms | 3 | 1 | 1.0 |
| warm:compare | total_ms | 3 | 15311 | 15924.6 |
| warm:fact | routing_ms | 3 | 1 | 1.0 |
| warm:fact | total_ms | 3 | 9 | 9.0 |
| warm:filter | routing_ms | 3 | 1 | 1.0 |
| warm:filter | search_ms | 3 | 144 | 144.0 |
| warm:filter | total_ms | 3 | 146 | 146.8 |
| warm:narrative | generate_ms | 3 | 5565 | 6510.6 |
| warm:narrative | retrieve_ms | 3 | 182 | 184.4 |
| warm:narrative | routing_ms | 3 | 1 | 1.0 |
| warm:narrative | total_ms | 3 | 6064 | 6994.4 |
| warm:set_ops | routing_ms | 3 | 1 | 1.0 |
| warm:set_ops | total_ms | 3 | 31 | 33.4 |

| 조건:경로 | 수집 호출 | 신규 검색 | 건너뛴 검색어 | 수집 실패 | 부분 완료 |
| --- | --- | --- | --- | --- | --- |
| cold:build | 0 | 0 | 0 | 0 | 0 |
| cold:build_archive | 0 | 0 | 0 | 0 | 0 |
| cold:compare | 3 | 5 | 2 | 0 | 1 |
| cold:fact | 1 | 0 | 3 | 0 | 0 |
| cold:filter | 0 | 0 | 0 | 0 | 0 |
| cold:narrative | 3 | 3 | 6 | 0 | 0 |
| cold:set_ops | 0 | 0 | 0 | 0 | 0 |
| warm:build | 0 | 0 | 0 | 0 | 0 |
| warm:build_archive | 0 | 0 | 0 | 0 | 0 |
| warm:compare | 3 | 0 | 9 | 0 | 0 |
| warm:fact | 0 | 0 | 0 | 0 | 0 |
| warm:filter | 0 | 0 | 0 | 0 | 0 |
| warm:narrative | 3 | 0 | 9 | 0 | 0 |
| warm:set_ops | 0 | 0 | 0 | 0 | 0 |

p90은 선형 보간입니다. warm은 준비 호출 1회를 제외합니다. 실패·다른 경로 응답은 성공 지연 통계에서 제외하며 원자료에 보존합니다. 운영 색인과 질문 시점 수집을 사용하며 실행 중 색인이 갱신될 수 있습니다. cold는 LLM 캐시만 우회하며 수집 매니페스트·문서·임베딩 캐시는 유지합니다. 이미 완료된 검색어는 건너뜁니다. cold에서 P1이 새 검색어를 만들면 신규 위키 요청이 발생할 수 있습니다. 수집 집계는 실패 표본과 재시도도 포함하며 준비 호출은 제외합니다. 검색 수는 수집기가 반환한 완료 검색 수입니다.
