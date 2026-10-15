"""Render saved results only; never rerun simulations."""
from pathlib import Path
import json
HERE=Path(__file__).parent

def main():
 rows=json.loads((HERE/'summary.json').read_text());manifest=json.loads((HERE/'manifest.json').read_text());validation=json.loads((HERE/'validation.json').read_text())
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 from matplotlib.ticker import PercentFormatter
 metrics=json.loads((HERE/'metrics.json').read_text())
 plt.rcParams.update({'font.family':'Noto Sans CJK SC','axes.unicode_minus':False})
 fig,axes=plt.subplots(1,2,figsize=(11,4.5))
 for ax,key,scenario,title in zip(axes,['range30','mean_retraced_10m'],['normal','shock'],['常态30分钟波幅中位数','冲击后10分钟原始回吐']):
  for seed in [81,82,83]:
   values=[next(r[key] for r in metrics if r['arm']==arm and r['scenario']==scenario and r['seed']==seed) for arm in ['mixed_10_5_5','random_20']]
   ax.plot([0,1],values,'o-',label=str(seed))
  ax.set(xticks=[0,1],xticklabels=['10随机+5库存+5短线','20随机'],title=title,ylabel='百分比')
  ax.yaxis.set_major_formatter(PercentFormatter(xmax=1))
  ax.grid(alpha=.2)
 axes[0].legend(title='种子')
 fig.text(.5,.025,'每点代表一个种子；零手续费，无玩家。冲击回吐每点为该路径9次事件的均值。',ha='center',fontsize=9)
 fig.tight_layout(rect=(0,.07,1,1));fig.savefig(HERE/'comparison.png',dpi=150);plt.close(fig)
 fields=[('成交笔数/分钟','trades_per_minute','.2f'),('外币成交量/分钟','foreign_volume_per_minute','.2f'),('30分钟波幅中位数','range30','.2%'),('收益标准差/5秒','return_std','.5f'),('方向ACF lag1','signed_acf1','.3f'),('裁剪/尝试','clipped_attempt_ratio','.2%'),('未成交/尝试','skipped_attempt_ratio','.2%'),('无意向/到达','no_intent_arrival_ratio','.2%'),('现金耗尽账户时点占比','cash_exhausted_fraction','.2%'),('外币耗尽账户时点占比','foreign_exhausted_fraction','.2%'),('终点现金耗尽户数','final_cash_exhausted','.2f'),('终点外币耗尽户数','final_foreign_exhausted','.2f'),('末值相对同路径HODL/每户','final_mean_vs_hodl','+.2f'),('各户独立清算估值合计','sum_independent_liquidations','.2f'),('全部钱包合并清算估值','aggregate_liquidation','.2f'),('资产低于3账户时点占比','below3_account_tick_fraction','.2%')]
 lines=['# 混合20户与纯随机20户离线比较','','两组仅改变角色分配。冻结混合组为10随机、5库存、5短线；对照为20随机。两组均使用同一个新研究模拟器，角色边界的具体差异见 model.diff。原模拟器和全部既有研究文件保持不变。','','每条10000个5秒tick，约13小时53分；种子81/82/83，常态和相同10次±5%冲击，共12条。每户5000金圆券与1000外币，池100万/20万；随机订单标准差120，热度psi=.0008，其他冻结参数见 manifest.json。零费、无玩家、无补资、无资产重置。','','这三个种子参与过混合参数选择，本轮属于探索性对照，不是未见种子验证。相同种子共享随机流构造，但路径分化后到达强度及事件顺序随市场状态变化，不能当作固定外生价格的配对回放。']
 for scenario,label in [('normal','常态'),('shock','冲击')]:
  pair=[next(r for r in rows if r['arm']==a and r['scenario']==scenario) for a in ['mixed_10_5_5','random_20']]
  lines+=['',f'## {label}：三种子等权均值','','|指标|混合20户|纯随机20户|','|---|---:|---:|']
  for label,key,fmt in fields:lines.append('|'+label+'|'+'|'.join(format(r[key],fmt) for r in pair)+'|')
  if scenario=='shock':
   for label,key in [('10分钟原始回吐','mean_retraced_10m'),('上涨冲击回吐','up_retraced_10m'),('下跌冲击回吐','down_retraced_10m')]:lines.append('|'+label+'|'+'|'.join(format(r[key],'.1%') for r in pair)+'|')
  mixed=pair[0];lines+=['',f"混合组已完成短线持仓中位数 {mixed['short_holding_median_minutes']:.2f} 分钟，每位短线已实现盈亏 {mixed['short_realized_per_account']:+.2f}；纯随机组无短线轮次，这两项为 N/A。"]
 lines+=['','## 口径与限制','','波幅使用27个完整非重叠30分钟窗口，各窗口最高最低差除该窗口期初价，先逐路径取中位数，再跨3种子平均。收益统计剔除冲击瞬间跳变，仍保留冲击后的内生变化；方向ACF的lag1/12/60对应5秒/1分钟/5分钟。','','冲击响应沿用既有 event_response：观察120tick，用最后12个收盘均价计算相对即时冲击的原始回吐。每条9个可观察事件，终点冲击没有后续，排除；27个事件相关，独立种子数仍为3。回吐可小于0或超过100%，不裁剪。','','耗尽阈值为余额≤1e-6。裁剪含余额或池上限/精度造成的限制，不全部等同资金不足；未成交与无意向分开统计，零现金买入/零外币卖出尝试见种子级CSV。未实现正式机器人死亡状态；资产低于3仅按实际钱包边际市值计算，不能替代正式系统死亡机制。','','末值相对HODL仅是钱包边际市值减同路径初始钱包持有末值。零费封闭AMM里机器人自身交易会改变池价，因此该差额不能解释为真实盈利或可复制套利。清算调用既有AMM卖出报价：每户独立清算都从同一终点池出发，不能相加当作实际同时可得；另列全部钱包合并清算值。均为估值，不是已实现盈亏。未验证正手续费、玩家参与、长期单边趋势或24小时运行。','','## 验证','','12条全部完成。两币守恒（含冲击注资）、逐时点钱包非负、个人市值、成交计数及正常/冲击首1000tick前缀核对通过。混合组6条与冻结 profit_trail04 的全部trace字段逐值完全一致。'+f" {validation['protected_files_unchanged']}个既有研究/AMM/量化文件哈希保持不变。",'','复现命令：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=backend:. python -m research.fx.reversion.repeated.random20_comparison.run`。仅生成报告：`PYTHONPATH=backend:. python -m research.fx.reversion.repeated.random20_comparison.finalize`。没有追加种子、情景、费用网格或永久测试。','','![常态三种子比较](comparison.png)','','[逐种子CSV](comparison.csv)、[完整指标](metrics.json)、[均值](summary.json)、[参数与哈希](manifest.json)、[验证](validation.json)、[模型差异](model.diff)。']
 lines[lines.index('![常态三种子比较](comparison.png)')]='![常态波幅与冲击后10分钟回吐的三种子比较](comparison.png)'
 if all(manifest['notebook_dependencies'].values()):
  import nbformat
  from nbclient import NotebookClient
  nb=nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell('# 保存结果的复算伴随笔记\n\n只读已保存的12路径指标，核对三种子均值，不运行模拟。数据与口径见 report.md 和 manifest.json。'),nbformat.v4.new_code_cell("import json\nfrom pathlib import Path\nimport numpy as np\nbase=Path.cwd()\nmetrics=json.loads((base/'metrics.json').read_text())\nsummary=json.loads((base/'summary.json').read_text())\nassert len(metrics)==12\nfor row in summary:\n    group=[r for r in metrics if r['arm']==row['arm'] and r['scenario']==row['scenario']]\n    assert len(group)==3\n    for key in ['range30','trades_per_minute','final_mean_vs_hodl']:\n        assert np.isclose(row[key],np.mean([r[key] for r in group]))\n[(r['arm'],r['scenario'],round(r['trades_per_minute'],3),round(r['range30'],5)) for r in summary]"),nbformat.v4.new_markdown_cell('3种子曾参与调参，属于探索性对照；没有将钱包市值差额解释为盈利。')])
  nbformat.validate(nb);NotebookClient(nb,timeout=60,kernel_name='python3',resources={'metadata':{'path':str(HERE)}}).execute();nbformat.write(nb,HERE/'companion.ipynb');lines+=['','[已执行伴随笔记](companion.ipynb)仅复算保存指标；未单独渲染预览。']
 else:lines+=['','当前环境缺少 nbformat/nbclient，未安装新依赖；CLI、保存源码与输出是主要复现交付。']
 (HERE/'report.md').write_text('\n'.join(lines)+'\n')
if __name__=='__main__':main()
