import api from './index'
import type {
  BatchListItem, BatchDetail, PurchaseResponse,
  MyRedemptionItem, MyRedemptionDetail,
  PartnerAdminItem, BatchAdminItem, BatchStatus,
  CsvImportPreview, CsvImportResult,
} from '@/types/redemption'

export const redemptionApi = {
  // ===== 用户端 =====
  listBatches: () => api.get<BatchListItem[]>('/api/v1/redemption/batches'),
  batchDetail: (id: number) => api.get<BatchDetail>(`/api/v1/redemption/batches/${id}`),
  purchase: (batchId: number) =>
    api.post<PurchaseResponse>('/api/v1/redemption/purchase', { batch_id: batchId }),
  myRedemptions: () => api.get<MyRedemptionItem[]>('/api/v1/redemption/my'),
  myRedemptionDetail: (id: number) =>
    api.get<MyRedemptionDetail>(`/api/v1/redemption/my/${id}`),
  markUsed: (id: number, used: boolean) =>
    api.post<{ ok: boolean; marked_used_by_user_at: string | null }>(
      `/api/v1/redemption/my/${id}/mark-used`, { used },
    ),
}

export interface PartnerCreatePayload {
  name: string
  description?: string
  website_url?: string
  logo_url?: string | null
  is_active?: boolean
}

export interface PartnerUpdatePayload {
  name?: string
  description?: string
  website_url?: string
  logo_url?: string | null
  is_active?: boolean
}

export interface BatchCreatePayload {
  partner_id: number
  name: string
  description?: string
  unit_price: string
}

export interface BatchUpdatePayload {
  name?: string
  description?: string
  unit_price?: string
  status?: BatchStatus
}

export const redemptionAdminApi = {
  listPartners: () => api.get<PartnerAdminItem[]>('/api/v1/admin/redemption/partners'),
  createPartner: (data: PartnerCreatePayload) =>
    api.post<PartnerAdminItem>('/api/v1/admin/redemption/partners', data),
  updatePartner: (id: number, data: PartnerUpdatePayload) =>
    api.patch<PartnerAdminItem>(`/api/v1/admin/redemption/partners/${id}`, data),

  listBatches: () => api.get<BatchAdminItem[]>('/api/v1/admin/redemption/batches'),
  createBatch: (data: BatchCreatePayload) =>
    api.post<BatchAdminItem>('/api/v1/admin/redemption/batches', data),
  updateBatch: (id: number, data: BatchUpdatePayload) =>
    api.patch<BatchAdminItem>(`/api/v1/admin/redemption/batches/${id}`, data),

  importPreview: (batchId: number, csvText: string) =>
    api.post<CsvImportPreview>(
      `/api/v1/admin/redemption/batches/${batchId}/import/preview`,
      { csv_text: csvText },
    ),
  importCommit: (batchId: number, csvText: string) =>
    api.post<CsvImportResult>(
      `/api/v1/admin/redemption/batches/${batchId}/import/commit`,
      { csv_text: csvText, confirm: true },
    ),
}
