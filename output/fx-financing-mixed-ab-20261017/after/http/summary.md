# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.726 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.459 | 18.982 |
| active | 90 | 7.393 | 44.941 |
| stress | 100 | 9.641 | 59.043 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 109} | 781.093 | 941.595 |
| active / cash_sell | {'ok': 113} | 529.819 | 729.394 |
| active / cross_zero | {'ok': 6} | 1159.725 | 1159.725 |
| active / financed_buy | {'ok': 30} | 705.235 | 919.05 |
| active / sell_repay | {'ok': 31} | 1147.964 | 1429.038 |
| active / short_cover | {'ok': 32} | 730.235 | 848.418 |
| active / short_open | {'ok': 23} | 1239.274 | 1364.12 |
| normal / cash_buy | {'ok': 68} | 146.233 | 198.577 |
| normal / cash_sell | {'ok': 29} | 116.049 | 197.606 |
| normal / cross_zero | {'ok': 2} | 83.206 | 83.206 |
| normal / financed_buy | {'ok': 20} | 118.242 | 118.647 |
| normal / sell_repay | {'ok': 8} | 142.708 | 142.708 |
| normal / short_cover | {'ok': 8} | 102.975 | 102.975 |
| normal / short_open | {'ok': 15} | 175.856 | 175.856 |
| role_prepare / cash_buy | {'ok': 5} | 28.482 | 28.482 |
| role_prepare / cross_zero | {'ok': 5} | 68.12 | 68.12 |
| role_prepare / financed_buy | {'ok': 20} | 50.558 | 74.094 |
| role_prepare / sell_repay | {'ok': 20} | 48.232 | 53.801 |
| role_prepare / short_cover | {'ok': 20} | 37.318 | 45.938 |
| role_prepare / short_open | {'ok': 15} | 54.145 | 54.145 |
| smoke / cash_buy | {'ok': 4} | 89.428 | 89.428 |
| smoke / cash_sell | {'ok': 4} | 49.551 | 49.551 |
| stress / cash_buy | {'ok': 133} | 9469.453 | 9555.979 |
| stress / cash_sell | {'ok': 123} | 9428.369 | 9605.412 |
| stress / cross_zero | {'ok': 5, 'timeout': 2, 'version_conflict': 2} | 14907.959 | 14907.959 |
| stress / financed_buy | {'ok': 22} | 9126.264 | 9535.524 |
| stress / sell_repay | {'ok': 19, 'timeout': 12, 'version_conflict': 10} | 14503.763 | 14503.763 |
| stress / short_cover | {'ok': 20} | 9427.412 | 9462.647 |
| stress / short_open | {'ok': 16, 'timeout': 7, 'version_conflict': 4} | 14587.73 | 14587.73 |

Coverage: `{"chart_sse_tail_reused": 1035, "cash_buy": 319, "cash_sell": 269, "financed_buy": 92, "sell_repay": 78, "cross_zero": 18, "short_cover": 80, "short_open": 69}`

See summary.json and requests.jsonl for errors and sampling boundaries.
