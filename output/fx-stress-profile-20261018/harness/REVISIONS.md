# Harness记录

控制轮加载profile_server_control_loaded.py所示文本（实际模块名profile_server.py）；profile轮加载最终profile_server.py。控制轮启动后、profile轮启动前，按主agent要求补了main线程thread_time资源字段，并将profile-only CPU tick gate标记改为GATES._task_keys的实际持有状态；控制轮没有CPU采样，不受gate标记修正影响。不重启control、不更改负载/阈值。

分析脚本生成后修正inclusive函数统计：同一栈内按file+function去重，避免同名不同code对象重复超过100%；未改raw、未重新测量。报告为真实raw汇总。

脚本依赖本机/tmp旧harness中的迁移/seed辅助以及保留融资dirty diff的实验源码。固定目录与测试库已存在；再次执行需新路径/新*_test库名及同一源码快照，不能直接开箱重跑。
