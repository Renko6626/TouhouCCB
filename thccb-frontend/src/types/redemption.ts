export interface PartnerPublic {
  id: number
  name: string
  description: string
  website_url: string
  logo_url: string | null
}

export interface BatchListItem {
  id: number
  partner: PartnerPublic
  name: string
  unit_price: string
  available_count: number
}

export interface BatchDetail extends BatchListItem {
  description: string
}

export interface PurchaseResponse {
  code_id: number
  code_string: string
  batch_name: string
  partner_name: string
  partner_website_url: string
  description: string
  paid_amount: string
  cash_after: string
}

export interface MyRedemptionItem {
  code_id: number
  batch_name: string
  partner_name: string
  partner_website_url: string
  paid_amount: string
  bought_at: string
  redeemed_at: string | null
  marked_used_by_user_at: string | null
}

export interface MyRedemptionDetail extends MyRedemptionItem {
  code_string: string
  description: string
}

// ===== Admin =====

export interface PartnerAdminItem {
  id: number
  name: string
  description: string
  website_url: string
  logo_url: string | null
  is_active: boolean
  created_at: string
}

export type BatchStatus = 'draft' | 'active' | 'archived'

export interface BatchAdminItem {
  id: number
  partner_id: number
  partner_name: string
  name: string
  description: string
  unit_price: string
  status: BatchStatus
  total_count: number
  sold_count: number
  available_count: number
  redeemed_count: number
  created_at: string
}

export type RedemptionCodeFilter = 'all' | 'available' | 'pending' | 'redeemed'

export interface RedemptionCodeAdminItem {
  id: number
  batch_id: number
  batch_name: string
  partner_name: string
  code_string: string
  status: 'available' | 'sold'
  bought_by_user_id: number | null
  bought_by_username: string | null
  bought_at: string | null
  redeemed_at: string | null
  redeemed_by_admin_id: number | null
  redeemed_by_admin_username: string | null
  redemption_note: string
}

export interface RedemptionCodeAdminPage {
  items: RedemptionCodeAdminItem[]
  total: number
  page: number
  page_size: number
}

export interface CsvImportPreview {
  total_lines: number
  new_codes: string[]
  duplicate_codes: string[]
  invalid_codes: string[]
}

export interface CsvImportResult {
  inserted: number
  skipped_duplicate: number
  skipped_invalid: number
}
