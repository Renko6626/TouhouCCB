<script setup lang="ts">
import FxPositionCard from '@/components/market/FxPositionCard.vue'
import FxSubmitSummary from '@/components/market/FxSubmitSummary.vue'
import FxOrderSize from '@/components/market/FxOrderSize.vue'
import FxExecutionPrice from '@/components/market/FxExecutionPrice.vue'
import { computeMinOut, formatFxAmount, formatFxPrice } from '@/api/fx'
import data from './fx-unified-panel-preview-data.json'

const side = new URLSearchParams(location.search).get('side')
const scenarios = [data.long, data.short].filter(s => !side || s.kind === side)
const money = (value: string) => formatFxAmount(value, 2)
const signed = (value: string) => `${Number(value) > 0 ? '+' : ''}${money(value)}`
</script>

<template>
  <main class="preview">
    <header class="page-heading">
      <div><h1>FX 交易框样稿</h1><p>已有持仓，已填写新订单。多空为两个独立账户示例。</p></div>
      <span class="badge">本地样稿</span>
    </header>
    <div class="scenarios" :class="{ single: scenarios.length === 1 }">
      <section v-for="s in scenarios" :key="s.kind">
        <div class="scenario-heading"><h2>{{ s.title }}</h2><span>摩拉 MORA</span></div>
        <div class="trade-panel">
          <nav aria-label="交易方向">
            <button :class="{ selected: s.kind === 'long' }">↗ 买入</button>
            <button :class="{ selected: s.kind === 'short', short: s.kind === 'short' }">↘ 卖出</button>
            <button>卖出多头</button><button>回补空头</button>
          </nav>
          <FxPositionCard :kind="s.kind as 'long' | 'short'" currency="摩拉" :quantity="s.quantity" :basis="s.basis" :close-amount="s.kind === 'long' ? s.close.output_amount : s.close.input_amount" :pnl="s.pnl" :disabled="false" />
          <div class="order-heading"><h3>{{ s.kind === 'long' ? '买入做多' : '借币做空' }}</h3><div v-if="s.kind === 'short'" class="input-mode"><button class="active">按金额</button><button>按数量</button></div><small v-else>本次订单</small></div>
          <FxOrderSize :model-value="s.order.scaleGold" :principal="s.current.liquidation_equity" :limit="3.5" :disabled="false" :tone="s.kind as 'long' | 'short'" :label="s.kind === 'long' ? '买入金额（含费）' : '做空参考金额'" />
          <div class="quote">
            <div class="row output"><span>{{ s.kind === 'long' ? '预计新增持仓' : '预计新增欠币' }}</span><strong>{{ formatFxAmount(s.kind === 'long' ? s.order.preview.output_amount : s.order.input, 2) }} <small>摩拉</small></strong></div>
            <FxExecutionPrice :average="s.order.average" :reference="data.reference" currency="摩拉" />
            <div class="row funds"><span>{{ s.kind === 'long' ? '预计支付' : '预计所得（锁定）' }}</span><strong>{{ money(s.kind === 'long' ? s.order.input : s.order.preview.output_amount) }} <small>金圆券</small></strong></div>
            <div v-if="s.kind === 'long'" class="sources"><span>自有现金 <b>600.00</b></span><span>借款 <b>600.00</b> 金圆券</span></div>
            <div class="row fee"><span>手续费（已计入）</span><span>{{ formatFxAmount(s.order.preview.fee_amount, 2) }} {{ s.kind === 'long' ? '金圆券' : '摩拉' }}</span></div>
          </div>
          <div class="margin"><div class="row"><span>预计成交后保证金率</span><strong>{{ (Number(s.order.margin) * 100).toFixed(2) }}%</strong></div><small>维持门槛 20.00%，低于门槛将被强平</small></div>
          <button class="submit"><FxSubmitSummary :action="s.kind === 'long' ? '借款买入' : '借币做空'" :foreign="s.kind === 'long' ? s.order.preview.output_amount : s.order.input" :gold="s.kind === 'long' ? s.order.input : s.order.preview.output_amount" currency="摩拉" :receiving="s.kind === 'short'" /></button>
          <button class="refresh">重新报价</button>
          <details class="protection"><summary>成交保护</summary><div class="row"><span>报价变动容忍度</span><strong>5%</strong></div><div class="row"><span>最低收到</span><strong>{{ money(computeMinOut(s.order.preview.output_amount, 500)) }} {{ s.kind === 'long' ? '摩拉' : '金圆券' }}</strong></div></details>
        </div>
      </section>
    </div>
    <p class="footnote">示例值来自现有本地报价函数。此页仅查看布局，不执行交易。</p>
  </main>
</template>

<style scoped>
.preview{max-width:820px;margin:24px auto;color:#111;font-variant-numeric:tabular-nums;--trade-panel-gap:8px;--trade-control-height:28px;--trade-action-height:34px}
.page-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;gap:12px}h1{font-size:22px;font-weight:700;margin:0}.page-heading p,.footnote{font-size:12px;color:#777;margin-top:6px}.badge{font-size:11px;border:1px solid #aaa;padding:4px 8px;white-space:nowrap}.scenarios{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}.scenarios.single{grid-template-columns:minmax(0,398px);justify-content:center}.scenario-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.scenario-heading h2{font-size:14px;font-weight:700;margin:0}.scenario-heading>span{font-size:11px;color:#777}.trade-panel{border:2px solid #000;padding:12px;background:#fff;display:grid;gap:10px}button{font:inherit;cursor:pointer;border:1px solid #000;background:white;color:#111;border-radius:0}nav{display:grid;grid-template-columns:1fr 1fr;gap:5px}nav button{height:29px;font-size:12px}.selected{background:#000;color:#fff}.selected.short{background:var(--color-down);border-color:var(--color-down)}.holding{padding:10px;border:1px solid #000}.profit{background:var(--color-up-bg);--pnl-color:var(--color-up)}.loss{background:var(--color-down-bg);--pnl-color:var(--color-down)}.holding-heading{display:flex;justify-content:space-between;gap:6px;align-items:center;margin-bottom:12px}.holding-heading strong{font-size:13px}.holding-heading small{font-size:10px;color:#777}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.metrics>div{display:grid;gap:4px}.metrics span{font-size:10px;color:#666;white-space:nowrap}.metrics strong{font-size:14px;font-weight:700;white-space:nowrap}small{font-weight:400;font-size:10px;color:#777}.row{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:12px}.row strong{font-weight:700;text-align:right}.close-amount{margin-top:13px}.close-amount>span{color:#666}.holding-footer{display:flex;justify-content:space-between;align-items:end;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid #0002}.holding-footer>div{display:grid;gap:3px}.holding-footer span{font-size:11px;color:#666}.holding-footer strong{font-size:21px;font-weight:700;color:var(--pnl-color)}.holding-footer button{padding:5px 8px;font-size:11px;white-space:nowrap}.holding details{margin-top:10px}details{font-size:11px;color:#777}summary{cursor:pointer}details p{line-height:1.6;margin:8px 0 0}.order-heading{display:flex;align-items:center;justify-content:space-between;margin-top:3px}h3{font-size:14px;font-weight:700;margin:0}.input-mode{display:flex}.input-mode button{border-color:#ccc;padding:3px 8px;font-size:10px}.input-mode .active{background:#000;color:#fff;border-color:#000}.quote{border:1px solid #ccc;padding:10px;display:grid;gap:7px}.output{padding-bottom:6px}.output>span{font-size:11px;color:#666}.output strong{font-size:13px}.funds{border-top:1px solid #eee;padding-top:8px}.sources{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:#777}.sources b{color:#444;font-weight:500}.fee{font-size:10px;color:#777}.margin{display:grid;gap:4px}.margin .row>span{font-size:11px;color:#666}.margin strong{font-size:16px}.margin small{font-size:10px}.submit{background:#000;color:#fff;height:36px;font-size:12px;font-weight:700}.refresh{border:0;font-size:11px;color:#777;justify-self:center;padding:0}.protection{border-top:1px solid #ddd;padding-top:9px}.protection .row{font-size:11px;margin-top:8px}.footnote{text-align:center;margin-top:20px}@media(max-width:680px){.preview{margin:12px auto}.scenarios{grid-template-columns:1fr;gap:24px}.page-heading{margin-bottom:20px}.page-heading h1{font-size:18px}.page-heading p{max-width:250px;line-height:1.6}.badge{display:none}.trade-panel{padding:10px}.metrics strong{font-size:13px}.holding-heading small{font-size:9px}}
</style>
