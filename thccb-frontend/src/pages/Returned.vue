<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { NAlert, NButton, NInputNumber, NSlider, NSpin, NTag } from 'naive-ui'
import { useReturned } from '@/composables/useReturned'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const { round, prediction, results, resultsLoading, resultsError, position, loading, submitting, connected, disabled, error, feedback,
  joined, changed, canSubmit, setPosition, submit, start } = useReturned()
const labels = { open: '竞猜进行中', closed: '已封盘', settled: '结果已公布', cancelled: '本轮已取消' }
const status = computed(() => round.value ? labels[round.value.status] : '等待开题')
const finished = computed(() => round.value?.status === 'settled' || round.value?.status === 'cancelled')
const locked = computed(() => round.value?.status !== 'open' || submitting.value)
const buttonLabel = computed(() => submitting.value ? '正在提交…' : !joined.value
  ? '投入 100 金圆券参与' : changed.value ? '保存竞猜位置' : '已提交竞猜')
const formatPosition = (value: number) => `${(value / 10).toFixed(1)}%`
const money = (value: string | number | null | undefined) => Number(value ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const netProfit = computed(() => {
  const value = Number(prediction.value?.net_profit ?? 0)
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${money(Math.abs(value))}`
})
const bands = computed(() => round.value?.bands ?? [
  { max_distance: 16, payout: '300' }, { max_distance: 56, payout: '200' },
  { max_distance: 100, payout: '150' }, { max_distance: 150, payout: '75' },
])
const rewardTier = computed(() => {
  if (!joined.value || prediction.value?.payout == null) return '未参与'
  const payout = Number(prediction.value.payout)
  return payout > 0 ? `${money(payout)} 金圆券档` : '未中奖'
})
const visibleBands = computed(() => [...bands.value].reverse())
function bandStyle(distance: number) {
  const center = round.value?.target_position ?? 500
  const left = Math.max(0, center - distance), right = Math.min(1000, center + distance)
  return { left: `${left / 10}%`, width: `${(right - left) / 10}%` }
}
onMounted(start)
</script>

<template>
  <div class="returned-page">
    <header class="page-heading">
      <div>
        <h1>逆转录者</h1>
        <p>听台上的线索，在两个概念之间选出你心中的位置。</p>
      </div>
      <div class="page-account">
        <span>我的金圆券</span>
        <strong>{{ money(auth.user?.cash) }}</strong>
      </div>
    </header>

    <div class="sync-bar">
      <span :class="['connection', { connected }]"><i />{{ disabled ? '活动未开启' : connected ? '实时同步中' : '连接中，断线后将自动重连' }}</span>
      <NButton size="small" :loading="loading" :disabled="submitting" @click="start">刷新状态</NButton>
    </div>
    <NAlert v-if="error" type="error" class="page-alert">{{ error }}</NAlert>
    <NAlert v-if="feedback && !finished" type="success" class="page-alert">{{ feedback }}</NAlert>

    <NSpin :show="loading && !round">
      <section v-if="!round" class="panel empty-state">
        <span class="i-mdi-tune-vertical empty-icon" aria-hidden="true" />
        <h2>{{ disabled ? '活动尚未开启' : '等台上出一道题' }}</h2>
        <p>{{ disabled ? '工作人员开启活动后，点击刷新状态即可参加。' : '新题开始后会自动出现在这里，不需要反复刷新。' }}</p>
        <p class="empty-note">每轮投入 100 金圆券，首次提交时扣除。</p>
      </section>

      <section v-else class="panel game-panel">
        <header class="round-heading">
          <div class="round-meta">
            <span class="round-number">第 {{ round.round_number }} 轮</span>
            <span v-if="round.team_name">{{ round.team_name }} 出题</span>
          </div>
          <NTag :type="round.status === 'open' ? 'success' : 'default'" :bordered="true">{{ status }}</NTag>
        </header>

        <div class="concepts">
          <h2>{{ round.left_label }}</h2>
          <span class="concept-divider" aria-hidden="true">↔</span>
          <h2>{{ round.right_label }}</h2>
        </div>

        <div v-if="round.status === 'settled'" class="result-grid result-overview" aria-live="polite">
          <div><span>真实位置</span><strong>{{ round.target_position == null ? '同步中…' : formatPosition(round.target_position) }}</strong></div>
          <template v-if="joined">
            <div><span>你的选择</span><strong>{{ formatPosition(prediction!.position!) }}</strong></div>
            <div><span>相差</span><strong>{{ prediction!.distance == null ? '—' : (prediction!.distance / 10).toFixed(1) }}<small> 个百分点</small></strong></div>
          </template>
          <div class="result-payout"><span>奖励等级</span><strong>{{ rewardTier }}</strong></div>
        </div>

        <div v-if="round.status === 'settled' && round.target_position != null" class="result-track" aria-label="真实目标与我的竞猜位置">
          <span v-for="band in visibleBands" :key="band.max_distance" :class="['result-band', 'band-' + band.payout]" :style="bandStyle(band.max_distance)" />
          <span class="target-marker" :style="{ left: round.target_position / 10 + '%' }"><b>目标</b></span>
          <span v-if="prediction?.position != null" class="guess-marker" :style="{ left: prediction.position / 10 + '%' }"><b>你</b></span>
        </div>

        <div class="slider-wrap">
          <NSlider :value="position" :min="0" :max="1000" :step="1" :format-tooltip="formatPosition"
            :disabled="locked" aria-label="竞猜位置" @update:value="setPosition" />
          <div class="ruler" aria-hidden="true"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div>
        </div>

        <div v-if="round.status === 'open'" class="prediction-controls">
          <div class="position-input">
            <label for="returned-position">我的选择</label>
            <NInputNumber id="returned-position" :value="position / 10" :min="0" :max="100" :step="0.1" :precision="1"
              :disabled="locked" aria-label="竞猜百分比" @update:value="v => { if (v != null) setPosition(v * 10) }">
              <template #suffix>%</template>
            </NInputNumber>
          </div>
          <NButton type="primary" size="large" :loading="submitting" :disabled="!canSubmit" @click="submit">{{ buttonLabel }}</NButton>
        </div>

        <div v-if="round.status === 'open'" class="submission-note" aria-live="polite">
          <p v-if="joined">已保存的位置：<strong>{{ formatPosition(prediction!.position!) }}</strong>。本轮已投入 {{ money(prediction!.stake) }}，修改位置不会再次扣款。</p>
          <p v-else>拖动滑块只预览，点击参与后才扣除 100 金圆券。</p>
          <p v-if="!joined && Number(auth.user?.cash ?? 0) < Number(round.stake)" class="warning">金圆券不足 {{ money(round.stake) }}，暂时无法参与。</p>
          <p v-if="!connected" class="warning">连接恢复并同步最新状态后才能提交。</p>
        </div>

        <div v-else-if="round.status === 'closed'" class="sealed-state" aria-live="polite">
          <h3>竞猜已封盘，等待揭晓</h3>
          <p v-if="joined">你的竞猜位置是 <strong>{{ formatPosition(prediction!.position!) }}</strong>，本轮已投入 {{ money(prediction!.stake) }} 金圆券。</p>
          <p v-else>你没有参与本轮，下一道题开始后可以加入。</p>
        </div>

        <div v-else-if="round.status === 'settled'" class="settlement" aria-live="polite">
          <h3>结算到账 {{ joined ? money(prediction!.payout) : 0 }} 金圆券</h3>
          <p v-if="joined">本轮投入 {{ money(prediction!.stake) }}，净盈亏 <strong>{{ netProfit }}</strong> 金圆券。奖励已由系统结算，无需领取。</p>
          <p v-else>你没有参与本轮。下一道题会自动出现在这里。</p>
        </div>

        <div v-else class="sealed-state" aria-live="polite">
          <h3>本轮竞猜已取消</h3>
          <p v-if="joined">已退回 {{ money(prediction!.refund) }} 金圆券，等待下一道题。</p>
          <p v-else>本轮没有扣款，等待下一道题。</p>
        </div>
        <section v-if="round.status === 'settled'" class="leaderboard" aria-live="polite">
          <h3>最接近目标的前五名</h3>
          <p v-if="resultsLoading && !results">正在加载竞猜结果…</p>
          <p v-else-if="resultsError" class="warning">{{ resultsError }}</p>
          <template v-if="results">
            <div v-if="results.top_five.length" class="leaderboard-scroll">
              <table>
                <thead><tr><th scope="col">名次</th><th scope="col">玩家</th><th scope="col">竞猜位置</th><th scope="col">误差</th><th scope="col">到账金圆券</th></tr></thead>
                <tbody><tr v-for="(entry, index) in results.top_five" :key="entry.username">
                  <td>{{ index + 1 }}</td><td class="player-name">{{ entry.username }}</td><td data-label="竞猜位置">{{ formatPosition(entry.position) }}</td>
                  <td data-label="误差">{{ (entry.distance / 10).toFixed(1) }} 个百分点</td><td data-label="到账金圆券">{{ money(entry.payout) }}</td>
                </tr></tbody>
              </table>
            </div>
            <p v-else>本轮还没有人参与竞猜。</p>
          </template>
        </section>
      </section>
    </NSpin>

    <section class="panel rules-panel">
      <h2>怎么获得奖励</h2>
      <p>与你竞猜的位置相比，真实目标越接近，结算到账越多。边界取较高一档。</p>
      <div class="reward-grid">
        <div v-for="band in bands" :key="band.max_distance" class="reward-cell">
          <span>相差 ≤ {{ band.max_distance / 10 }} 个百分点</span><strong>{{ money(band.payout) }}</strong><small>金圆券到账</small>
        </div>
        <div class="reward-cell"><span>相差 &gt; 15 个百分点</span><strong>0</strong><small>金圆券到账</small></div>
      </div>
      <p class="rules-note">每轮只投入一次，封盘前可以修改。结算金额已包含本轮全部到账，不另退本金；取消则原额退款。</p>
    </section>
  </div>
</template>

<style scoped>
.leaderboard { border-top: 1px solid #ddd; margin-top: 24px; padding-top: 20px; }
.leaderboard h3 { margin: 0 0 16px; font-size: 17px; }
.leaderboard-scroll { overflow-x: auto; }
.leaderboard table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
.leaderboard th, .leaderboard td { text-align: left; padding: 12px 10px; border-bottom: 1px solid #ddd; white-space: nowrap; }
.leaderboard th { font-weight: 600; color: #666; }
.leaderboard .player-name { white-space: normal; overflow-wrap: anywhere; min-width: 6em; max-width: 14em; font-weight: 700; }
.returned-page { max-width: 1040px; margin: 0 auto; padding: 16px 12px 32px; color: #000; }
.page-heading, .sync-bar, .round-heading, .prediction-controls { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-heading { align-items: flex-start; margin-bottom: 20px; }
h1 { font-size: 28px; font-weight: 800; margin: 0 0 6px; }
.page-heading p, .rules-panel > p { margin: 0; font-size: 14px; line-height: 1.7; color: #555; }
.page-account { text-align: right; flex-shrink: 0; }
.page-account span { display: block; font-size: 12px; color: #666; }
.page-account strong { display: block; font-size: 24px; font-variant-numeric: tabular-nums; }
.sync-bar { margin-bottom: 16px; }
.connection { font-size: 12px; color: #777; display: flex; align-items: center; gap: 7px; }
.connection i { width: 7px; height: 7px; background: #999; }
.connection.connected { color: #222; }
.connection.connected i { background: #000; }
.page-alert { margin-bottom: 16px; }
.panel { border: 2px solid #000; background: #fff; box-shadow: 5px 5px 0 #000; padding: 24px; margin-bottom: 24px; }
.empty-state { text-align: center; padding: 56px 20px; }
.empty-icon { display: inline-block; width: 32px; height: 32px; margin-bottom: 12px; }
.empty-state h2 { font-size: 22px; margin: 0 0 10px; }
.empty-state p { color: #666; line-height: 1.7; font-size: 14px; }
.empty-state .empty-note { margin-top: 24px; font-size: 12px; }
.round-meta { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; font-size: 13px; color: #666; }
.round-number { font-size: 18px; color: #000; font-weight: 700; }
.concepts { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 18px; margin: 38px 0 32px; }
.concepts h2 { font-size: clamp(22px, 3vw, 32px); line-height: 1.4; margin: 0; overflow-wrap: anywhere; }
.concepts h2:last-child { text-align: right; }
.concept-divider { color: #999; font-size: 22px; }
.slider-wrap { margin: 10px 3px 28px; }
.ruler { display: flex; justify-content: space-between; margin-top: 10px; color: #888; font-size: 11px; font-variant-numeric: tabular-nums; }
.prediction-controls { border-top: 1px solid #ddd; padding-top: 22px; }
.position-input { display: flex; align-items: center; gap: 12px; }
.position-input label { white-space: nowrap; font-size: 14px; }
.position-input :deep(.n-input-number) { width: 140px; }
.submission-note { color: #666; font-size: 12px; line-height: 1.7; margin-top: 14px; }
.submission-note p { margin: 5px 0; }
.submission-note strong { color: #000; }
.warning { color: #a35c00; }
.sealed-state { padding: 20px; background: #f5f5f5; border: 1px solid #000; }
.sealed-state h3, .settlement h3 { font-size: 17px; margin: 0 0 10px; }
.sealed-state p, .settlement p { margin: 0; font-size: 13px; line-height: 1.7; }
.settlement { border-top: 2px solid #000; padding-top: 22px; }
.result-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }
.result-grid span { color: #666; font-size: 12px; display: block; }
.result-grid strong { font-size: 24px; display: block; font-variant-numeric: tabular-nums; }
.result-grid small { font-size: 12px; font-weight: 400; }
.result-track { height: 22px; margin: 42px 3px 36px; position: relative; border: 1px solid #000; background: #f3f3f3; }
.result-band { position: absolute; top: 0; bottom: 0; }
.band-75 { background: #e7e7e7; }.band-150 { background: #d4d4d4; }.band-200 { background: #adadad; }.band-300 { background: #737373; }
.target-marker, .guess-marker { position: absolute; height: 36px; width: 2px; background: #000; top: -8px; transform: translateX(-50%); }
.target-marker b, .guess-marker b { position: absolute; transform: translateX(-50%); white-space: nowrap; font-size: 11px; line-height: 1.5; background: #fff; padding: 0 3px; }
.target-marker b { bottom: calc(100% + 1px); }.guess-marker b { top: calc(100% + 1px); }
.guess-marker { border-left: 2px dashed #000; background: transparent; }
.rules-panel h2 { font-size: 18px; margin: 0 0 8px; }
.reward-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); margin: 20px 0 16px; border: 1px solid #000; }
.reward-cell { padding: 14px 10px; border-right: 1px solid #000; text-align: center; }
.reward-cell:last-child { border-right: 0; }
.reward-cell span, .reward-cell small { display: block; font-size: 11px; color: #666; line-height: 1.6; }
.reward-cell strong { display: block; font-size: 25px; margin: 4px 0; font-variant-numeric: tabular-nums; }
.rules-panel .rules-note { font-size: 12px; }
@media (max-width: 640px) {
  .leaderboard table, .leaderboard tbody { display: block; }
  .leaderboard thead { display: none; }
  .leaderboard tr { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); padding: 12px 0; border-bottom: 1px solid #ddd; gap: 10px 8px; }
  .leaderboard td { padding: 0; border: 0; white-space: normal; font-size: 12px; }
  .leaderboard td:first-child { font-weight: 700; }
  .leaderboard .player-name { grid-column: 2 / -1; min-width: 0; max-width: none; font-size: 14px; }
  .leaderboard td[data-label]::before { content: attr(data-label); display: block; color: #777; font-size: 11px; margin-bottom: 4px; }

  .returned-page { padding: 8px 0 24px; }.panel { padding: 18px 14px; box-shadow: 3px 3px 0 #000; }
  h1 { font-size: 24px; }.page-account strong { font-size: 20px; }.page-heading p { font-size: 12px; }
  .prediction-controls { align-items: stretch; flex-direction: column; }.position-input { justify-content: space-between; }
  .result-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 20px; }
  .reward-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); border: 0; gap: 6px; }.reward-cell, .reward-cell:last-child { border: 1px solid #000; padding: 12px 5px; }
  .concepts { gap: 10px; margin-top: 30px; }.round-heading { gap: 8px; }.round-meta { gap: 8px; }
}
</style>
