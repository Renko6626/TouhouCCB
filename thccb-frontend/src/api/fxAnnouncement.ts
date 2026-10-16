import api from './index'

export interface FxAnnouncement {
  id: number
  content: string
  created_at: string
}

export const fxAnnouncementApi = {
  latest: (): Promise<FxAnnouncement | null> => api.get('/api/v1/fx/announcements/latest'),
  publish: (content: string): Promise<FxAnnouncement> =>
    api.postForCurrentUser('/api/v1/admin/fx/announcements', { content }),
}
