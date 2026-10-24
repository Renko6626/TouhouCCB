import { gsap } from 'gsap'
import { ScrambleTextPlugin } from 'gsap/ScrambleTextPlugin'
import { PowerGlitch } from 'powerglitch'

export function startTakeover(stage: HTMLElement, onComplete: () => void): () => void {
  const context = gsap.context(() => {
    gsap.registerPlugin(ScrambleTextPlugin)
    gsap.ticker.lagSmoothing(0)
    // Visual references: Peter Ankermann's Blackwall VFX; Ryoji Ikeda's test pattern;
    // Rosa Menkman's A Vernacular of File Formats. Original webpage assets are retained.
    const between = gsap.utils.random

    const attack = stage.querySelector<HTMLElement>('.fragments')!
    const coverage = stage.querySelector<HTMLElement>('.coverage')!
    const glyph = stage.querySelector<HTMLElement>('.glyph')!

    const motionPreference = matchMedia('(prefers-reduced-motion: reduce)')
    const effects: ReturnType<typeof PowerGlitch.glitch>[] = []
    const source = stage.querySelector<HTMLElement>('.source-view')!
    const clonedTargets = [...stage.querySelectorAll('.signal,.fault-source')]
    clonedTargets.forEach((target) => target.append(source.cloneNode(true)))
    function fillBuffer(target: HTMLElement) {
      const copy = source.cloneNode(true) as HTMLElement
      copy.classList.add('buffer-copy')
      copy.style.width = `${stage.clientWidth}px`
      copy.style.height = `${stage.clientHeight}px`
      copy.style.transform = `translate(${-between(0, stage.clientWidth * 0.65)}px,${-between(0, stage.clientHeight * 0.65)}px)`
      target.append(copy)
    }
    function glitch(target: Element | null, options: Parameters<typeof PowerGlitch.glitch>[1]) {
      const effect = PowerGlitch.glitch(target as HTMLElement, {
        createContainers: false,
        playMode: 'manual',
        hideOverflow: false,
        timing: { duration: 900, iterations: Infinity },
        glitchTimeSpan: false,
        shake: false,
        pulse: false,
        ...options,
      })
      effects.push(effect)
      return effect
    }
    const pageEffect = glitch(stage.querySelector('.page-layers'), {
      shake: { velocity: 18, amplitudeX: 0.003, amplitudeY: 0.001 },
      slice: { count: 6, velocity: 22, minHeight: 0.008, maxHeight: 0.08, hueRotate: false },
    })
    const signalEffect = glitch(stage.querySelector('.signal'), {
      shake: { velocity: 30, amplitudeX: 0.028, amplitudeY: 0.012 },
      slice: {
        count: 16,
        velocity: 30,
        minHeight: 0.018,
        maxHeight: 0.2,
        hueRotate: false,
        cssFilters: 'grayscale(1) contrast(2.4)',
      },
    })
    const localEffects = [...stage.querySelectorAll('.fault-source')].map((target) =>
      glitch(target, {
        shake: { velocity: 24, amplitudeX: 0.022, amplitudeY: 0.003 },
        slice: { count: 5, velocity: 26, minHeight: 0.025, maxHeight: 0.2, hueRotate: false },
      }),
    )
    // One finite progression, with short pauses between increasingly forceful bursts.
    const timeline = gsap.timeline({
      paused: true,
      defaults: { ease: 'none' },
      onComplete: () => {
        effects.forEach((effect) => effect.stopGlitch())
        onComplete()
        stage.dataset.phase = 'complete'
      },
    })
    timeline.set(stage, { attr: { 'data-phase': 'normal' } }, 0)
    timeline.call(() => pageEffect.startGlitch(), [], 0.34)
    timeline.set(stage, { attr: { 'data-phase': 'breach' } }, 0.34)
    localEffects.forEach((effect, i) => {
      const node = stage.querySelectorAll('.fault-window')[i]!
      const start = 0.41 + i * 0.23
      timeline.call(() => effect.startGlitch(), [], start)
      timeline
        .set(node, { opacity: 1 }, start)
        .set(node, { opacity: 0 }, start + 0.06)
        .set(node, { opacity: 1 }, start + 0.11)
        .set(node, { opacity: 0 }, start + 0.2)
      timeline.set(node, { opacity: 1 }, 1.05 + i * 0.07).set(node, { opacity: 0 }, 1.23 + i * 0.07)
    })
    timeline.set('.transmission', { opacity: 1, x: 0 }, 0.67)
    timeline.to(
      '.transmission',
      {
        duration: 0.45,
        scrambleText: {
          text: '会话正在断开',
          chars: '01/<>_连接异常',
          speed: 0.7,
          tweenLength: false,
        },
      },
      0.67,
    )
    timeline.set('.transmission', { opacity: 0 }, 1.19)
    timeline.call(() => signalEffect.startGlitch(), [], 1.01)
    for (const [on, off] of [
      [1.01, 1.08],
      [1.15, 1.24],
      [1.36, 1.49],
      [1.61, 1.79],
      [1.9, 2.09],
      [2.18, 2.4],
    ]) {
      timeline.set('.signal', { opacity: 1 }, on).set('.signal', { opacity: 0 }, off)
    }
    timeline.set(stage, { attr: { 'data-phase': 'overrun' } }, 1.36)
    // Many short-lived red rectangles jump independently across the full viewport.
    // Black patches remain larger; red patches no longer share cluster origins.
    for (let i = 0; i < 88; i++) {
      const black = i < 12
      const bar = !black && i % 6 === 0
      const patch = document.createElement('div')
      patch.className = `block ${black ? 'black-patch' : 'red-patch'}`
      patch.style.cssText = `left:${between(0, 94)}%;top:${between(0, 92)}%;width:${black ? between(18, 31) : bar ? between(9, 17) : between(3, 10)}%;height:${black ? between(20, 36) : bar ? between(1, 2.5) : between(4, 13)}%;--ink:${black ? '#000' : i % 7 ? '#ff1738' : '#93001d'}`
      const surface = black
        ? ''
        : i % 9 === 0
          ? 'buffer'
          : i % 4 === 0
            ? 'encoded'
            : i % 7 === 0
              ? 'columnar'
              : ''
      patch.innerHTML = `<div class="ink ${surface}"></div>`
      if (surface === 'buffer') fillBuffer(patch.firstElementChild as HTMLElement)
      patch.style.setProperty('--sample-x', `${between(10, 85)}%`)
      patch.style.setProperty('--sample-y', `${between(15, 90)}%`)
      attack.append(patch)
      const effect = glitch(patch, {
        timing: { duration: between(360, 690), iterations: Infinity },
        slice: {
          count: black ? 3 : 2,
          velocity: between(22, 32),
          minHeight: black ? 0.18 : 0.45,
          maxHeight: 0.95,
          hueRotate: false,
        },
      })
      const arrival = black ? between(0.72, 1.65) : between(0.4, 1.82)
      timeline.call(() => effect.startGlitch(), [], arrival)
      if (black) {
        timeline
          .set(patch, { opacity: 1 }, arrival)
          .set(patch, { opacity: 0 }, arrival + 0.07)
          .set(patch, { opacity: 1, xPercent: between(-24, 24) }, arrival + 0.14)
      } else {
        // Every patch still jumps independently; repeated frames create buffer stutter.
        let previous = null
        let step = 0
        for (let at = arrival; at < 2.66; at += between(0.11, 0.195), step++) {
          const current = {
            opacity: 1,
            left: `${between(-2, 96)}%`,
            top: `${between(1, 95)}%`,
            width: `${bar ? between(9, 17) : between(3, 10)}%`,
            height: `${bar ? between(1, 2.5) : between(4, 13)}%`,
          }
          timeline.set(patch, current, at)
          if (previous && step % 3 === 1) {
            timeline
              .set(patch, { opacity: 0 }, at + 0.03)
              .set(patch, previous, at + 0.055)
              .set(patch, current, at + 0.08)
          }
          timeline.set(patch, { opacity: 0 }, at + between(0.085, 0.105))
          previous = current
        }
      }
    }
    // Short frozen strips carry real page pixels through the corrupted field.
    for (let i = 0; i < 8; i++) {
      const strip = document.createElement('div')
      strip.className = 'block frame-memory'
      strip.style.cssText = `left:${between(2, 78)}%;top:${between(10, 85)}%;width:${between(12, 27)}%;height:${between(2.4, 5.5)}%;--ink:#ff1738;--sample-x:${between(5, 95)}%;--sample-y:${between(10, 90)}%`
      strip.innerHTML = '<div class="ink buffer"></div>'
      fillBuffer(strip.firstElementChild as HTMLElement)
      attack.append(strip)
      const effect = glitch(strip, {
        timing: { duration: 520, iterations: Infinity },
        slice: { count: 2, velocity: 22, minHeight: 0.5, maxHeight: 1, hueRotate: false },
      })
      const at = 0.93 + i * 0.17
      timeline.call(() => effect.startGlitch(), [], at)
      timeline
        .set(strip, { opacity: 1 }, at)
        .set(strip, { opacity: 0 }, at + 0.06)
        .set(strip, { opacity: 1, xPercent: i % 2 ? 22 : -17 }, at + 0.11)
        .set(strip, { opacity: 0 }, Math.min(2.4, at + 0.28))
    }
    // The remaining original image is carved away in irregular, overlapping regions.
    const coverageRects: [number, number, number, number, number][] = [
      [79, -3, 28, 30, 0.94],
      [61, 10, 46, 27, 1.22],
      [48, 29, 37, 29, 1.43],
      [-4, 72, 47, 35, 1.6],
      [27, 51, 57, 32, 1.79],
      [68, 58, 40, 48, 1.93],
      [12, -4, 64, 29, 2.08],
      [-4, 19, 34, 58, 2.21],
      [17, 35, 51, 43, 2.34],
    ]
    for (const [left, top, width, height, time] of coverageRects) {
      const rect = document.createElement('div')
      rect.className = 'breach'
      rect.style.cssText = `left:${left}%;top:${top}%;width:${width}%;height:${height}%`
      coverage.append(rect)
      timeline.set(rect, { visibility: 'visible' }, time)
      // A single missing frame exposes the damaged image beneath the first scars.
      if (time < 1.8)
        timeline
          .set(rect, { visibility: 'hidden' }, time + 0.055)
          .set(rect, { visibility: 'visible' }, time + 0.105)
    }
    // Separated final fragments retain air between the red rectangles.
    // Keep the title area clear while scattering fragments around the remaining space.
    const slabs: [number, number, number, number, string][] = [
      [63, 12, 7, 8, ''],
      [80, 7, 5, 5, ''],
      [91, 16, 8, 11, ''],
      [73, 25, 8, 6, ''],
      [60, 31, 5, 9, 'shadow'],
      [86, 34, 10, 4, ''],
      [68, 43, 6, 11, ''],
      [94, 47, 6, 8, ''],
      [79, 52, 8, 6, ''],
      [58, 59, 5, 4, ''],
      [88, 65, 10, 9, 'shadow'],
      [71, 69, 7, 10, ''],
      [62, 82, 6, 5, ''],
      [84, 86, 8, 8, ''],
      [95, 92, 6, 9, ''],
      [54, 18, 3, 4, ''],
      [77, 18, 3, 3, 'shadow'],
      [91, 28, 3, 3, ''],
      [77, 39, 4, 3, ''],
      [64, 66, 3, 3, ''],
      [84, 76, 3, 4, ''],
      [11, 15, 6, 3, ''],
      [29, 8, 4, 4, ''],
      [43, 18, 8, 2, ''],
      [4, 66, 3, 5, ''],
      [18, 77, 7, 3, 'shadow'],
      [36, 87, 8, 4, ''],
      [48, 72, 4, 6, ''],
      [2, 87, 5, 3, ''],
      [47, 43, 3, 4, ''],
    ]
    slabs.forEach(([left, top, width, height, type], i) => {
      const rect = document.createElement('div')
      const surface = type || ['', 'encoded', '', 'columnar', '', 'buffer'][i % 6]
      rect.className = `glyph-block ${surface}`
      rect.style.cssText = `left:${left}%;top:${top}%;width:${width}%;height:${height}%`
      glyph.append(rect)
      if (surface === 'buffer') fillBuffer(rect)
      const at = 2.43 + (i % 12) * 0.017
      timeline
        .set(rect, { opacity: 1, xPercent: i % 2 ? 110 : -95, yPercent: i % 3 ? 65 : -80 }, at)
        .set(rect, { opacity: 0 }, at + 0.035)
        .set(rect, { opacity: 1, xPercent: i % 2 ? -65 : 80, yPercent: 0 }, at + 0.075)
        .set(rect, { opacity: 0 }, at + 0.11)
        .set(rect, { opacity: 1, xPercent: 0 }, at + 0.16)
    })
    for (const [left, top, width] of [
      [7.5, 22, 5],
      [58, 70, 10],
      [75, 92, 19],
    ]) {
      const line = document.createElement('div')
      line.className = 'end-line'
      line.style.cssText = `left:${left}%;top:${top}%;width:${width}%`
      stage.querySelector('.end-scene')!.append(line)
      timeline
        .set(line, { opacity: 1 }, 2.63)
        .set(line, { opacity: 0 }, 2.67)
        .set(line, { opacity: 1 }, 2.86)
    }
    // Hard takeover comes before the final title resolves; there is no crossfade.
    timeline.set('.blackout,.end-scene', { opacity: 1 }, 2.43)
    timeline
      .set('.message', { opacity: 1, xPercent: -3 }, 2.48)
      .set('.message', { xPercent: 2 }, 2.54)
      .set('.message', { xPercent: 0 }, 2.62)
    timeline.to(
      '.message h1',
      {
        duration: 0.47,
        scrambleText: { text: '访问终止', chars: '访问终止/01<>', speed: 0.7, tweenLength: false },
      },
      2.48,
    )
    timeline
      .set('.end-chrome', { opacity: 1 }, 2.77)
      .set('.end-chrome', { opacity: 0 }, 2.8)
      .set('.end-chrome', { opacity: 1 }, 2.86)
    timeline.set(
      '.signal,.page-layers,.fault-window,.attack,.transmission',
      { visibility: 'hidden' },
      3,
    )
    timeline.set(stage, { attr: { 'data-phase': 'complete' } }, 3)

    function settle() {
      timeline.progress(1)
      effects.forEach((effect) => effect.stopGlitch())
    }
    const updateMotion = (event: MediaQueryListEvent) => {
      if (event.matches) settle()
    }
    motionPreference.addEventListener('change', updateMotion)
    if (motionPreference.matches) settle()
    else timeline.play(0)
    return () => {
      motionPreference.removeEventListener('change', updateMotion)
      effects.forEach((effect) => effect.stopGlitch())
      timeline.kill()
    }
  }, stage)
  return () => context.revert()
}
