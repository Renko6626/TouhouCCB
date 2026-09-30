import { describe, expect, it } from 'vitest'
import { useFxResource } from '../useFxResource'

describe('FX 账户资源状态', () => {
  it('首次失败不伪造零值；成功后失败保留快照并显式标记错误', async () => {
    const resource = useFxResource<number>()
    await resource.load(() => Promise.reject(new Error('offline')))
    expect(resource.data.value).toBeNull()
    expect(resource.failed.value).toBe(true)
    await resource.load(() => Promise.resolve(0))
    expect(resource.data.value).toBe(0)
    expect(resource.failed.value).toBe(false)
    await resource.load(() => Promise.reject(new Error('offline')))
    expect(resource.data.value).toBe(0)
    expect(resource.failed.value).toBe(true)
    expect(resource.updatedAt.value).not.toBe('')
  })

  it('切币后旧响应不回填新币种，重叠刷新只接受最后一次请求', async () => {
    const resource = useFxResource<string>()
    let resolveOld!: (value: string) => void
    const old = resource.load(() => new Promise<string>(resolve => { resolveOld = resolve }))
    resource.reset()
    await resource.load(() => Promise.resolve('new pair'))
    resolveOld('old pair')
    await old
    expect(resource.data.value).toBe('new pair')
    expect(resource.loading.value).toBe(false)
  })
})
