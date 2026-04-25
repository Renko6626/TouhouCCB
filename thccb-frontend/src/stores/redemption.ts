import { defineStore } from 'pinia'
import { ref } from 'vue'
import { redemptionApi } from '@/api/redemption'
import type { BatchListItem, MyRedemptionItem } from '@/types/redemption'

export const useRedemptionStore = defineStore('redemption', () => {
  const batches = ref<BatchListItem[]>([])
  const myRedemptions = ref<MyRedemptionItem[]>([])
  const loading = ref(false)

  async function loadBatches() {
    loading.value = true
    try {
      batches.value = await redemptionApi.listBatches()
    } finally {
      loading.value = false
    }
  }

  async function loadMyRedemptions() {
    loading.value = true
    try {
      myRedemptions.value = await redemptionApi.myRedemptions()
    } finally {
      loading.value = false
    }
  }

  return { batches, myRedemptions, loading, loadBatches, loadMyRedemptions }
})
