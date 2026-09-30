import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

export type TradeSide = 'buy' | 'sell'

/** Scroll/focus a short heading; hide the dock while any part of its trade panel is visible. */
export function useMobileTradeEntry(onSide: (side: TradeSide) => void) {
  const tradePanelRef = ref<HTMLElement | null>(null)
  const tradePanelVisible = ref(false)
  let observer: IntersectionObserver | null = null

  watch(tradePanelRef, (anchor) => {
    observer?.disconnect()
    observer = null
    tradePanelVisible.value = false
    if (!anchor || typeof IntersectionObserver === 'undefined') return
    observer = new IntersectionObserver(
      ([entry]) => { tradePanelVisible.value = Boolean(entry?.isIntersecting) },
      // Exclude the sticky header and the dock itself from the usable viewport.
      { threshold: 0, rootMargin: '-72px 0px -96px 0px' },
    )
    // threshold 0 works even when the form is taller than the viewport.
    observer.observe(anchor.parentElement ?? anchor)
  }, { flush: 'post' })

  const openTrade = async (side: TradeSide) => {
    onSide(side)
    await nextTick()
    const anchor = tradePanelRef.value
    if (!anchor) return
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    anchor.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
    // Focusing the heading gives keyboard/screen-reader context without opening a keyboard.
    anchor.focus({ preventScroll: true })
  }

  onBeforeUnmount(() => observer?.disconnect())

  return { tradePanelRef, tradePanelVisible, openTrade }
}
