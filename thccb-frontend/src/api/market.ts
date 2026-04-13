import api from './index'
import type {
  MarketListItem,
  MarketDetail,
  TradeRequest,
  TradeResponse,
  QuoteRequest,
  QuoteResponse,
  MarketTrade,
  LeaderboardItem,
  MarketCreateResponse,
  MarketStatusResponse,
  SettleResult,
  MarketCreate,
} from '@/types/api'

export const marketApi = {
  // 获取所有活跃市场（支持搜索和过滤）
  async getMarkets(params?: { keyword?: string; tag?: string; include_halt?: boolean; include_settled?: boolean }): Promise<MarketListItem[]> {
    return api.get<MarketListItem[]>('/api/v1/market/list', { params })
  },

  // 获取市场详情
  async getMarketDetail(marketId: number): Promise<MarketDetail> {
    return api.get<MarketDetail>(`/api/v1/market/${marketId}`)
  },

  // 创建新市场（仅管理员）
  async createMarket(data: MarketCreate): Promise<MarketCreateResponse> {
    return api.post<MarketCreateResponse>('/api/v1/market/create', data)
  },

  // 买入胜券
  async buy(outcomeId: number, shares: number): Promise<TradeResponse> {
    const request: TradeRequest = { outcome_id: outcomeId, shares }
    return api.post<TradeResponse>('/api/v1/market/buy', request)
  },

  // 卖出胜券
  async sell(outcomeId: number, shares: number): Promise<TradeResponse> {
    const request: TradeRequest = { outcome_id: outcomeId, shares }
    return api.post<TradeResponse>('/api/v1/market/sell', request)
  },

  // 下单预估
  async quote(request: QuoteRequest): Promise<QuoteResponse> {
    return api.post<QuoteResponse>('/api/v1/market/quote', request)
  },

  // 关闭市场交易（仅管理员）
  async closeMarket(marketId: number): Promise<MarketStatusResponse> {
    return api.post<MarketStatusResponse>(`/api/v1/market/${marketId}/close`)
  },

  // 恢复市场交易（仅管理员）
  async resumeMarket(marketId: number): Promise<MarketStatusResponse> {
    return api.post<MarketStatusResponse>(`/api/v1/market/${marketId}/resume`)
  },

  // 结算市场（指定赢家 + 兑付比例，仅管理员）
  async resolveMarket(marketId: number, winningOutcomeId: number, payout: number): Promise<SettleResult> {
    return api.post<SettleResult>(`/api/v1/market/${marketId}/resolve`, {
      winning_outcome_id: winningOutcomeId,
      payout
    })
  },

  // 获取市场成交记录
  async getMarketTrades(marketId: number, limit: number = 50): Promise<MarketTrade[]> {
    return api.get<MarketTrade[]>(`/api/v1/market/${marketId}/trades`, {
      params: { limit }
    })
  },

  // 财富排行榜
  async getLeaderboard(limit: number = 20): Promise<LeaderboardItem[]> {
    return api.get<LeaderboardItem[]>('/api/v1/market/leaderboard', {
      params: { limit }
    })
  },
}

export default marketApi
