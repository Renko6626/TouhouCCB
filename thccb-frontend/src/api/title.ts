import api from './index'

export interface TitleRead {
  id: number
  name: string
  description: string
  color: string
  icon: string
  sort_order: number
  is_active: boolean
  created_at: string
}

export interface TitleChip {
  id: number
  name: string
  color: string
  icon: string
}

export interface MyTitleItem {
  title: TitleRead
  granted_at: string
  source: 'admin' | 'code'
}

export interface MyTitlesResponse {
  equipped_title_id: number | null
  titles: MyTitleItem[]
}

export const titleApi = {
  myTitles: () => api.get<MyTitlesResponse>('/api/v1/title/me'),
  equip: (title_id: number | null) =>
    api.post<{ equipped_title_id: number | null }>('/api/v1/title/me/equip', { title_id }),
  redeem: (code: string) =>
    api.post<{ title: TitleRead }>('/api/v1/title/redeem', { code }),
  publicCatalog: () => api.get<TitleRead[]>('/api/v1/title/catalog'),
  userEquipped: (user_id: number) =>
    api.get<TitleChip | null>(`/api/v1/title/users/${user_id}/equipped`),
}
