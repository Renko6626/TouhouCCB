<script setup lang="ts">
import { NTabs, NTabPane } from 'naive-ui'
import type { AdminUserActivity } from '@/api/admin'
defineProps<{ activity: AdminUserActivity | null; error: string }>()
const fmt = (v: string | null | undefined) => v ? new Date(v).toLocaleString('zh-CN') : '—'
const labels: Record<string, string> = { player: '用户交易', liquidation_action: '管理员平仓', settle: '结算', settle_lose: '未中奖结算', liquidate: '强制平仓', spot: '现货交易', borrow_buy: '借款买入', admin_close: '管理员平仓', short_open: '开空', admin_ban: '封禁', admin_unban: '解封', admin_set_role: '调整管理员权限', admin_batch_close: '批量平仓', credit_freeze_set: '调整信贷冻结', admin_fx_short_writeoff: '核销做空债务', user: '用户操作', manual: '手动交易', lmsr: '预测市场', system: '系统', liquidation: '强制平仓', admin: '管理员操作', buy: '买入', sell: '卖出', short: '做空', cover: '平空', short_cover: '平空', trade: '交易', admin_adjust_cash: '管理员调整现金', admin_amnesty: '大赦', admin_force_loan: '管理员放贷', admin_forgive_debt: '管理员免债', ban: '封禁', unban: '解封', user_banned: '封禁', user_unbanned: '解封' }
const label = (v: string) => labels[v] ?? v
</script>
<template>
  <p v-if="error" class="error">{{ error }}</p>
  <p v-else-if="!activity">正在加载持仓与记录…</p>
  <template v-if="activity">
    <p>注册时间：{{ fmt(activity.registered_at) }}　最近交易：{{ fmt(activity.last_trade_at) }}</p>
    <NTabs type="line" size="small">
      <NTabPane name="positions" tab="持仓">
        <p>金币借款见上方债务。做空本金为借入资产数量，利息另列；开空所得为剩余空头对应的卖出所得。</p>
        <div class="scroll"><table><thead><tr><th>市场 / 资产</th><th>类型</th><th>数量 / 做空本金</th><th>做空利息</th><th>成本 / 开空所得</th></tr></thead><tbody>
          <tr v-for="(p, index) in activity.positions" :key="index"><td>{{ p.market_name }} / {{ p.asset_name }}</td><td>{{ p.kind.includes('short') ? '做空' : p.product === 'fx' ? '外汇现货' : '预测持仓' }}</td><td>{{ p.quantity.toFixed(4) }}</td><td>{{ p.interest.toFixed(4) }}</td><td>金 {{ p.cost_basis.toFixed(2) }}</td></tr>
        </tbody></table></div><p v-if="!activity.positions.length">暂无持仓</p>
      </NTabPane>
      <NTabPane name="trades" tab="最近交易">
        <div class="scroll"><table><thead><tr><th>时间</th><th>市场</th><th>交易</th><th>数量</th><th>金额</th><th>来源</th></tr></thead><tbody>
          <tr v-for="(t, index) in activity.transactions" :key="index"><td>{{ fmt(t.created_at) }}</td><td>{{ t.market_name }}</td><td>{{ label(t.side) }} / {{ label(t.purpose) }}</td><td>{{ t.quantity.toFixed(4) }}</td><td>{{ t.gold >= 0 ? '支出' : '收入' }} 金 {{ Math.abs(t.gold).toFixed(2) }}</td><td>{{ label(t.source) }}</td></tr>
        </tbody></table></div><p v-if="!activity.transactions.length">暂无交易</p>
      </NTabPane>
      <NTabPane name="history" tab="管理记录">
        <div class="scroll"><table><thead><tr><th>时间</th><th>操作</th><th>操作人</th><th>原因</th></tr></thead><tbody><tr v-for="e in activity.history" :key="e.id"><td>{{ fmt(e.created_at) }}</td><td>{{ label(e.event_type) }}</td><td>{{ e.operator_user_id ? `#${e.operator_user_id}` : '系统' }}</td><td>{{ e.reason || '—' }}</td></tr></tbody></table></div><p v-if="!activity.history.length">暂无管理记录</p>
      </NTabPane>
    </NTabs>
  </template>
</template>
<style scoped>p{font-size:13px;color:#555;margin:10px 0}.error{color:#b91c1c}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid #ddd;white-space:nowrap}</style>
