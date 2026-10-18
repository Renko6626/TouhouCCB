# 执行及报告修订

- before由固定a820157 git archive，after由最终330f4e6 git archive；不用移动工作树。
- before首次应用启动因harness lifespan的original变量被背景标签循环覆盖，负载前即失败；修正为lifespan_original后双方共用相同脚本，正式库均使用_r1_test。保留before-startup-failed及旧before_test库，不计入A/B。
- 执行期没有改生产代码、没有增负载/调整阈值/使用profiler。
- 汇总区分目录/my-trades/business DB读取和目标行情输入；public_fx_push的并发版本fallback如实保留。市场目录id发现与market_data历史维护不作为暖态价格输入归零目标。
- 读尾延迟与SSE分类、版本fallback与SQL/CPU结果均来自本轮；报告审阅后只修文案/表头，不修改raw、不重跑。
- 主agent拥有已启动PG；harness只清理自身HTTP/monitor/token。正确性PGunion18passed由主agent另记validation.md，本轮未再次执行pg-tests.sh。
