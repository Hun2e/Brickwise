# latency-live-legacy

측정 모드: live

| 조건:경로 | 성공 수 | 실패 수 | 중앙값 ms | p90 ms |
| --- | --- | --- | --- | --- |
| cold:build | 3 | 0 | 1648.1549999443814 | 9953.217320027761 |
| cold:build_archive | 3 | 0 | 25795.84709997289 | 41481.83734002523 |
| cold:compare | 3 | 0 | 45776.2495999923 | 46330.441119964235 |
| cold:fact | 3 | 0 | 985.6630000285804 | 1390.2843600139022 |
| cold:filter | 3 | 0 | 3274.968500016257 | 3844.3398599978536 |
| cold:narrative | 3 | 0 | 15069.36039996799 | 16808.78744001966 |
| cold:set_ops | 3 | 0 | 827.5198999326676 | 1059.9786200094968 |
| warm:build | 3 | 0 | 92.30179991573095 | 93.11563996598125 |
| warm:build_archive | 3 | 0 | 3203.149600070901 | 17191.826479998417 |
| warm:compare | 3 | 0 | 5762.097500031814 | 9594.853099947795 |
| warm:fact | 3 | 0 | 8.623700006864965 | 8.671699999831617 |
| warm:filter | 3 | 0 | 901.4204000122845 | 918.4903999790549 |
| warm:narrative | 3 | 0 | 5333.572800038382 | 8542.79744005762 |
| warm:set_ops | 3 | 0 | 37.252900074236095 | 37.9684999352321 |

| 조건:경로 | 단계 | 표본 수 | 중앙값 ms | p90 ms |
| --- | --- | --- | --- | --- |
| cold:build | routing_ms | 3 | 1554 | 9858.800000000001 |
| cold:build | total_ms | 3 | 1648 | 9952.800000000001 |
| cold:build_archive | generate_ms | 3 | 1844 | 1923.2 |
| cold:build_archive | retrieve_ms | 3 | 20969 | 22592.2 |
| cold:build_archive | routing_ms | 3 | 1151 | 15990.2 |
| cold:build_archive | total_ms | 3 | 25796 | 41481.6 |
| cold:compare | generate_ms | 3 | 4289 | 4516.2 |
| cold:compare | retrieve_ms | 3 | 3860 | 18999.2 |
| cold:compare | routing_ms | 3 | 12154 | 17611.6 |
| cold:compare | total_ms | 3 | 45776 | 46330.4 |
| cold:fact | routing_ms | 3 | 974 | 1378.8 |
| cold:fact | total_ms | 3 | 986 | 1390.0 |
| cold:filter | routing_ms | 3 | 787 | 857.4 |
| cold:filter | search_ms | 3 | 2486 | 2755.6 |
| cold:filter | total_ms | 3 | 3275 | 3844.6 |
| cold:narrative | generate_ms | 3 | 5428 | 5534.4 |
| cold:narrative | retrieve_ms | 3 | 9478 | 10290.0 |
| cold:narrative | routing_ms | 3 | 883 | 1671.8000000000002 |
| cold:narrative | total_ms | 3 | 15069 | 16809.0 |
| cold:set_ops | routing_ms | 3 | 788 | 1020.0 |
| cold:set_ops | total_ms | 3 | 827 | 1059.8 |
| warm:build | routing_ms | 3 | 1 | 1.0 |
| warm:build | total_ms | 3 | 92 | 92.8 |
| warm:build_archive | generate_ms | 3 | 2564 | 16598.4 |
| warm:build_archive | retrieve_ms | 3 | 194 | 296.4 |
| warm:build_archive | routing_ms | 3 | 1 | 1.8 |
| warm:build_archive | total_ms | 3 | 3203 | 17191.800000000003 |
| warm:compare | generate_ms | 3 | 5155 | 8995.0 |
| warm:compare | retrieve_ms | 3 | 181 | 182.6 |
| warm:compare | routing_ms | 3 | 1 | 1.0 |
| warm:compare | total_ms | 3 | 5762 | 9594.8 |
| warm:fact | routing_ms | 3 | 1 | 1.0 |
| warm:fact | total_ms | 3 | 9 | 9.0 |
| warm:filter | routing_ms | 3 | 1 | 1.0 |
| warm:filter | search_ms | 3 | 898 | 915.6 |
| warm:filter | total_ms | 3 | 901 | 918.6 |
| warm:narrative | generate_ms | 3 | 4860 | 8069.6 |
| warm:narrative | retrieve_ms | 3 | 174 | 174.8 |
| warm:narrative | routing_ms | 3 | 1 | 1.0 |
| warm:narrative | total_ms | 3 | 5334 | 8542.8 |
| warm:set_ops | routing_ms | 3 | 1 | 1.0 |
| warm:set_ops | total_ms | 3 | 37 | 37.8 |

| 조건:경로 | 수집 호출 | 신규 검색 | 건너뛴 검색어 | 수집 실패 | 부분 완료 |
| --- | --- | --- | --- | --- | --- |
| cold:build | 0 | 0 | 0 | 0 | 0 |
| cold:build_archive | 3 | 4 | 4 | 0 | 0 |
| cold:compare | 4 | 2 | 6 | 0 | 1 |
| cold:fact | 0 | 0 | 0 | 0 | 0 |
| cold:filter | 0 | 0 | 0 | 0 | 0 |
| cold:narrative | 3 | 3 | 6 | 0 | 0 |
| cold:set_ops | 0 | 0 | 0 | 0 | 0 |
| warm:build | 0 | 0 | 0 | 0 | 0 |
| warm:build_archive | 3 | 0 | 9 | 0 | 0 |
| warm:compare | 3 | 0 | 3 | 0 | 0 |
| warm:fact | 0 | 0 | 0 | 0 | 0 |
| warm:filter | 0 | 0 | 0 | 0 | 0 |
| warm:narrative | 3 | 0 | 9 | 0 | 0 |
| warm:set_ops | 0 | 0 | 0 | 0 | 0 |

p90은 선형 보간입니다. warm은 준비 호출 1회를 제외합니다. 실패·다른 경로 응답은 성공 지연 통계에서 제외하며 원자료에 보존합니다. 운영 색인과 질문 시점 수집을 사용하며 실행 중 색인이 갱신될 수 있습니다. cold는 LLM 캐시만 우회하며 수집 매니페스트·문서·임베딩 캐시는 유지합니다. 이미 완료된 검색어는 건너뜁니다. cold에서 P1이 새 검색어를 만들면 신규 위키 요청이 발생할 수 있습니다. 수집 집계는 실패 표본과 재시도도 포함하며 준비 호출은 제외합니다. 검색 수는 수집기가 반환한 완료 검색 수입니다.
