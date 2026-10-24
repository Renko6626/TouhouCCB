<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { startTakeover } from './takeover'
const stage = ref<HTMLElement | null>(null)
const finished = ref(false)
let dispose: (() => void) | undefined
onMounted(() => {
  if (stage.value)
    dispose = startTakeover(stage.value, () => {
      finished.value = true
    })
})
onBeforeUnmount(() => dispose?.())
</script>

<template>
  <main ref="stage" class="stage site-takeover" data-phase="normal" aria-label="交易系统暂时下线">
    <div class="page-layers" aria-hidden="true">
      <div class="source-view" inert><slot name="source" /></div>
    </div>
    <div class="signal" aria-hidden="true"></div>
    <div class="fault-window" style="--crop: 9% 49% 59% 3%" aria-hidden="true">
      <div class="fault-source"></div>
    </div>
    <div class="fault-window tint" style="--crop: 48% 4% 22% 7%" aria-hidden="true">
      <div class="fault-source"></div>
    </div>
    <div class="attack coverage" aria-hidden="true"></div>
    <div class="attack fragments" aria-hidden="true"></div>
    <div class="transmission" aria-hidden="true">连接异常</div>
    <div class="blackout" aria-hidden="true"></div>
    <section class="end-scene" :inert="!finished" aria-label="访问终止">
      <div class="glyph" aria-hidden="true"></div>
      <div class="message">
        <h1 aria-label="访问终止">访问终止</h1>
      </div>
      <div class="end-chrome">
        <div class="end-header">
          <span class="identity">东方fx天则</span
          ><span class="session" aria-hidden="true">ACCESS / DENIED</span>
        </div>
        <div class="access-actions"><slot name="actions" /></div>
        <div class="end-footer" aria-hidden="true">
          <span>ACCESS REVOKED</span><span>NO SIGNAL _</span>
        </div>
      </div>
    </section>
  </main>
</template>

<style>
.site-takeover {
  & {
    --red: #ff1738;
    --deep-red: #93001d;
    --black: #000;
    --paper: #e5dfe0;
    position: fixed;
    inset: 0;
    z-index: 2000;
    width: 100%;
    height: 100svh;
    overflow: hidden;
    background: var(--black);
    isolation: isolate;
    font-family: Arial, 'Microsoft YaHei', sans-serif;
  }
  .page-layers,
  .signal,
  .fault-window,
  .fault-source,
  .attack,
  .blackout,
  .end-scene {
    position: absolute;
    inset: 0;
  }
  .homepage {
    display: block;
    width: 100%;
    height: 100%;
  }
  .signal,
  .fault-window {
    opacity: 0;
    pointer-events: none;
  }
  .fault-window {
    clip-path: inset(var(--crop));
  }
  .fault-window.tint {
    filter: grayscale(1) sepia(1) saturate(8) hue-rotate(305deg);
  }
  .attack {
    pointer-events: none;
    overflow: hidden;
  }
  .block {
    position: absolute;
    opacity: 0;
  }
  .ink {
    width: 100%;
    height: 100%;
    background: var(--ink);
  }
  .ink.encoded {
    background: repeating-linear-gradient(
      0deg,
      var(--ink) 0 12%,
      transparent 12% 17%,
      var(--ink) 17% 28%,
      transparent 28% 32%
    );
  }
  .ink.columnar {
    background: repeating-linear-gradient(
      90deg,
      var(--ink) 0 17%,
      transparent 17% 22%,
      var(--ink) 22% 45%,
      transparent 45% 49%
    );
  }
  .ink.buffer,
  .glyph-block.buffer {
    overflow: hidden;
    isolation: isolate;
  }
  .buffer-copy {
    position: absolute !important;
    inset: auto !important;
    top: 0 !important;
    left: 0 !important;
    max-width: none !important;
    mix-blend-mode: multiply;
    pointer-events: none;
  }
  .block > .ink:first-child {
    visibility: hidden;
  }
  .breach {
    position: absolute;
    background: var(--black);
    visibility: hidden;
  }
  .blackout {
    opacity: 0;
    background: var(--black);
  }
  .end-scene {
    opacity: 0;
    color: var(--red);
    overflow: hidden;
  }
  .end-chrome {
    position: absolute;
    inset: 0;
    opacity: 0;
  }
  .end-header {
    position: absolute;
    top: 7.5%;
    left: 7.5%;
    right: 7.5%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: clamp(7px, 0.85vw, 16px);
  }
  .identity {
    display: flex;
    gap: 0.65em;
    align-items: center;
    font-weight: 700;
  }
  .identity::before {
    content: '';
    width: 0.75em;
    height: 0.75em;
    background: var(--red);
  }
  .session {
    font-family: Consolas, 'Courier New', monospace;
    font-size: 0.9em;
  }
  .message {
    position: absolute;
    top: 27%;
    left: 7.5%;
    width: 58%;
    opacity: 0;
  }
  .message h1 {
    margin: 0;
    font-weight: 900;
    letter-spacing: -0.065em;
    line-height: 1.06;
    font-size: clamp(32px, 8.3vw, 160px);
    white-space: nowrap;
  }
  .access-actions {
    position: absolute;
    left: 7.5%;
    bottom: 12%;
    max-width: 52%;
    background: #000;
    padding: 8px 12px 8px 0;
  }
  .message-note {
    position: absolute;
    left: 7.5%;
    bottom: 12%;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 1.25em;
    font-size: clamp(8px, 1.05vw, 20px);
  }
  .message-note::before {
    content: '';
    display: block;
    width: 2.5em;
    height: 0.2em;
    background: var(--red);
  }
  .end-footer {
    position: absolute;
    left: 7.5%;
    right: 7.5%;
    bottom: 6%;
    display: flex;
    justify-content: space-between;
    font:
      clamp(6px, 0.7vw, 13px) Consolas,
      'Courier New',
      monospace;
    color: #9a263a;
  }
  .glyph {
    position: absolute;
    inset: 0;
  }
  .glyph-block {
    position: absolute;
    opacity: 0;
    background: var(--red);
  }
  .glyph-block.shadow {
    background: var(--deep-red);
  }
  .glyph-block.encoded {
    background: repeating-linear-gradient(
      0deg,
      var(--red) 0 13%,
      transparent 13% 18%,
      var(--red) 18% 31%,
      transparent 31% 35%
    );
  }
  .glyph-block.columnar {
    background: repeating-linear-gradient(
      90deg,
      var(--red) 0 17%,
      transparent 17% 24%,
      var(--red) 24% 49%,
      transparent 49% 54%
    );
  }

  .identity,
  .session,
  .end-footer span,
  .message-note {
    background: #000;
  }
  .session {
    padding: 4px 0 4px 8px;
  }
  .glyph-block.cut {
    background: var(--black);
  }
  .end-line {
    position: absolute;
    height: 0.22%;
    background: var(--red);
    opacity: 0;
  }
  .transmission {
    position: absolute;
    top: 11%;
    right: 7%;
    padding: 0.55em 0.9em;
    border: 1px solid var(--red);
    background: var(--black);
    color: var(--red);
    font:
      clamp(8px, 1.1vw, 20px) Consolas,
      'Microsoft YaHei',
      sans-serif;
    opacity: 0;
  }

  .source-view {
    position: relative;
    width: 100%;
    height: 100%;
    overflow: hidden;
  }
  &,
  & * {
    box-sizing: border-box;
  }
  @media (max-width: 600px) {
    .message {
      top: 27%;
      width: 75%;
    }
    .message h1 {
      font-size: 14vw;
    }
    .access-actions {
      max-width: 85%;
      right: 7.5%;
      bottom: 13%;
    }
    .end-header {
      font-size: 10px;
    }
    .end-footer {
      font-size: 8px;
    }
    .session {
      font-size: 8px;
    }
  }
}
</style>
