import api from './index'

export const siteAccessApi = {
  status: () => api.get<{ disabled: boolean }>('/api/v1/site/access'),
}
