# 执行脚本快照修订

本目录保留2026-10-18本轮实际使用的临时harness；run/read/count脚本在测量后没有修改。prepare.py和build_report.py在结果审阅后做了以下记录修订，未重新压测、未修改raw：

- prepare.py将还原before函数的git引用从动态HEAD固定为基线e3b2087f4828f76a79cfe152be768d3621829801。实际压测时HEAD即该提交，生成的before源码hash和scope.diff不变。
- build_report.py补充固定读SQL统计边界、带shell引用的PG测试命令、临时依赖与重跑限制；门闩时间显示保留两位小数。
- 本脚本依赖旧/tmp harness及保留融资diff的工作树，不是可移植独立包；再次执行需要新副本目录、新测试库名并调整路径。
