# 唯一 writer 高压 profile

固定后端：6c7675722cda265bd205dced6ce0f939ae7ac769，与上一轮A/B after完全一致。单轮profile，沿用100人/200SSE、60/20/20角色、全部既有阶段、负载与保护。临时数据库fx_writer_profile_20261018_test，Unix socket55489；不连接生产。

profile_server.py仅实验期包装已加载函数引用与AsyncSession方法，不改业务文件。active/stress启用49Hz SIGPROF CPU采样和函数/SQL耗时，stress另启用19Hz --gil --native py-spy；保护后停止采样，已开始的span仍完成记录。native捕获的是持有GIL的线程栈，不是所有CPU时间的完整分类；SIGPROF也受Python信号延迟交付影响。SQL cursor与函数elapsed含事件循环等待；嵌套span不可相加，不能用process_time差值跨await冒充单请求CPU。吞吐与上一轮无profile结果只能作扰动对照，不是优化A/B。

准备验证：项目venv py_compile profile_server.py/run.py退出0。实际命令：backend/venv/bin/python /tmp/sunyunbo/fx-writer-profile-20261018/run.py --start-authorized。已完成，结论和profiler扰动见 [report.md](report.md)。服务及PG已停止，临时token已删除。
