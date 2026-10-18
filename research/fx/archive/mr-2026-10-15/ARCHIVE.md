# M/R分支归档

2026-10-15。按第四、第五轮讨论停止扩展固定基准库存/固定参与率/阈值M/R组合；同质240仅保留作有限尺度参照，不认定复现市场典型事实。

- [最终第四轮报告](horizon-v4-findings.md)
- [第三轮：冲击与库存抵销](impact-v3-findings.md)
- [第二轮：评分修正与固定配比](switching-v2-findings.md)
- [第一轮：含原评分时点偏差](switching-findings.md)
- [原活跃目录README](README.md)
- [文件清单、大小及SHA256](manifest.json)

236个原文件共310625206字节整体移动，逐项核验字节不变。包括原脚本、配置、13项模型/数值检查、各轮原始结果与源码快照，以及原缓存。没有修改旧结果、写入新hash冒充旧实现或合并新分支数据。更早归档保持原位。

第二轮讨论原位在research/gpt-discussion-2.md，其副本在context/research/gpt-discussion-2.md。第五轮输入留在当前research/fx/gpt-discussion-5.md。

原报告的本目录相对结果链接仍可查看；原运行命令、`research.fx.*`导入、历史JSON绝对来源路径保持原样。重放旧实验应在独立临时checkout中，把此目录原文件恢复到research/fx，再按对应源码快照恢复相应轮次；不要直接覆盖当前公共基础设施。manifest以旧research/fx为相对根，不能将缺少旧绝对路径误认为历史数据丢失。
