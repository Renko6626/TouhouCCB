export type FxPendingStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
type RequestIdentity = { readonly body: Readonly<{ idempotency_key: string }> }

function browserStorage(): FxPendingStorage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null
  }
}

/** One saved request per account and entry; recovery is always user initiated. */
export class FxPendingOrder<Request extends RequestIdentity> {
  pending: Request | null = null
  busy = false
  storageError = false
  private userId: string | null = null
  private generation = 0

  constructor(
    userId: string | number | null,
    private readonly prefix: string,
    private readonly restore: (value: unknown) => Request | null,
    private readonly storage: FxPendingStorage | null = browserStorage(),
  ) {
    this.setUser(userId)
  }

  setUser(id: string | number | null) {
    const next = id == null ? null : String(id)
    if (next === this.userId) return
    this.generation++
    this.userId = next
    this.pending = null
    this.storageError = false
    if (next === null) return
    try {
      if (!this.storage) throw new Error('Storage unavailable')
      const raw = this.storage.getItem(this.prefix + next)
      if (raw === null) return
      this.pending = this.restore(JSON.parse(raw))
      this.storageError = this.pending === null
    } catch {
      this.storageError = true
    }
  }

  get hasUnresolved() {
    return this.pending !== null || this.storageError
  }

  protected async submit<T>(value: unknown, run: (request: Request) => Promise<T>): Promise<T | null> {
    if (this.busy || this.hasUnresolved) throw new Error('有订单等待核对，请先处理原订单')
    if (this.userId === null || !this.storage) throw new Error('无法保存订单，尚未提交')
    const request = this.restore(value)
    if (!request) throw new Error('订单参数无效')
    try {
      this.storage.setItem(this.prefix + this.userId, JSON.stringify(request))
    } catch {
      this.storageError = true
      throw new Error('无法保存订单，尚未提交')
    }
    this.pending = request
    return this.retry(run)
  }

  private clear(id: string, request: Request) {
    try {
      if (!this.storage) throw new Error('Storage unavailable')
      const raw = this.storage.getItem(this.prefix + id)
      const saved = raw === null ? null : this.restore(JSON.parse(raw))
      if (raw !== null && !saved) throw new Error('Invalid saved order')
      // A late response must not erase a newer request saved after remounting.
      if (saved && saved.body.idempotency_key !== request.body.idempotency_key) return
      this.storage.removeItem(this.prefix + id)
      if (id === this.userId && this.pending?.body.idempotency_key === request.body.idempotency_key)
        this.pending = null
    } catch {
      if (id === this.userId) this.storageError = true
    }
  }

  async retry<T>(run: (request: Request) => Promise<T>): Promise<T | null> {
    if (this.busy || !this.pending || this.userId === null) return null
    const request = this.pending, id = this.userId, generation = this.generation
    this.busy = true
    try {
      const result = await run(request)
      this.clear(id, request)
      return id === this.userId && generation === this.generation ? result : null
    } catch (error) {
      const e = error as { status?: unknown; response?: { status?: unknown } } | null
      const status = Number(e?.response?.status ?? e?.status)
      // Auth/timeout/limit failures can precede lookup of an already committed order.
      if (status >= 400 && status < 500 && ![401, 403, 408, 429].includes(status))
        this.clear(id, request)
      throw error
    } finally {
      this.busy = false
    }
  }

  /** Called only after the user has checked their trades and account. */
  clearAfterReview(): boolean {
    if (this.busy || this.userId === null) return false
    try {
      if (!this.storage) throw new Error('Storage unavailable')
      this.storage.removeItem(this.prefix + this.userId)
      this.pending = null
      this.storageError = false
      return true
    } catch {
      this.storageError = true
      return false
    }
  }
}
