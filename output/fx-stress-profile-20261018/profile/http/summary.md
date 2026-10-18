# FX load-test report

Target: http://127.0.0.1:18543
Abort: 30s window: successful trade P99 exceeds 2000ms

| Stage | Active users | Successful trades/s | HTTP/s |
| --- | ---: | ---: | ---: |
| smoke | 1 | 0.727 | 3.727 |
| readonly | 0 | 0.0 | 6.451 |
| normal | 70 | 2.508 | 19.228 |
| active | 90 | 7.195 | 43.667 |
| stress | 100 | 8.446 | 55.101 |

| Stage / action | Write results | Success P95 (ms) | Success P99 (ms) |
| --- | --- | ---: | ---: |
| active / cash_buy | {'ok': 108} | 547.084 | 711.677 |
| active / cash_sell | {'ok': 104} | 501.959 | 657.897 |
| active / cross_zero | {'ok': 6} | 1087.543 | 1087.543 |
| active / financed_buy | {'ok': 32} | 658.801 | 763.699 |
| active / sell_repay | {'ok': 35} | 767.342 | 777.399 |
| active / short_cover | {'ok': 28} | 648.217 | 721.35 |
| active / short_open | {'ok': 22} | 982.189 | 1500.412 |
| normal / cash_buy | {'ok': 69} | 114.353 | 138.496 |
| normal / cash_sell | {'ok': 30} | 118.779 | 121.265 |
| normal / cross_zero | {'ok': 2} | 76.82 | 76.82 |
| normal / financed_buy | {'ok': 19} | 184.649 | 184.649 |
| normal / sell_repay | {'ok': 8} | 284.795 | 284.795 |
| normal / short_cover | {'ok': 9} | 116.852 | 116.852 |
| normal / short_open | {'ok': 16} | 196.309 | 196.309 |
| role_prepare / cash_buy | {'ok': 5} | 26.231 | 26.231 |
| role_prepare / cross_zero | {'ok': 5} | 63.132 | 63.132 |
| role_prepare / financed_buy | {'ok': 20} | 51.922 | 53.066 |
| role_prepare / sell_repay | {'ok': 20} | 54.665 | 55.301 |
| role_prepare / short_cover | {'ok': 20} | 31.032 | 34.524 |
| role_prepare / short_open | {'ok': 15} | 46.907 | 46.907 |
| smoke / cash_buy | {'ok': 4} | 88.725 | 88.725 |
| smoke / cash_sell | {'ok': 4} | 49.481 | 49.481 |
| stress / cash_buy | {'ok': 120} | 10589.197 | 10933.077 |
| stress / cash_sell | {'ok': 114} | 10682.673 | 10773.88 |
| stress / cross_zero | {'ok': 1, 'timeout': 4, 'version_conflict': 2} | 9368.499 | 9368.499 |
| stress / financed_buy | {'ok': 18} | 10879.552 | 10879.552 |
| stress / sell_repay | {'ok': 11, 'timeout': 15, 'version_conflict': 15} | 14439.63 | 14439.63 |
| stress / short_cover | {'ok': 18} | 10674.225 | 10674.225 |
| stress / short_open | {'ok': 10, 'timeout': 10, 'version_conflict': 10} | 13692.793 | 13692.793 |

Coverage: `{"chart_sse_tail_reused": 983, "cash_buy": 306, "cash_sell": 252, "financed_buy": 89, "sell_repay": 74, "cross_zero": 14, "short_cover": 75, "short_open": 63}`

See summary.json and requests.jsonl for errors and sampling boundaries.
