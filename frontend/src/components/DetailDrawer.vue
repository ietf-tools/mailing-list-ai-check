<script setup>
// The slide-over message-detail drawer.
//
// Fetches GET /api/messages/:id on mount and whenever messageId changes, and
// renders: a top bar that steps ↑/↓ through the messages store's current
// filtered+sorted result set, the message metadata grid, the analysis card
// (prediction pill, headline, engine version and a per-window table), and one
// text card holding the extracted text — furniture lines hidden by default —
// plus the raw body.
//
// Each Pangram window is marked in the text twice: a numbered box at its first
// character, and a bracket down the right-hand wire gutter spanning its lines.
// Both hover to the window's table row. Window positions come from the API as
// {line, col} pairs in extracted-text coordinates (see _window_details).
//
// Contract: props { messageId: Number }, emits ['close'].
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get } from '../api'
import { useMessagesStore } from '../stores/messages'
import { useFiltersStore } from '../stores/filters'
import { useUiStore } from '../stores/ui'
import { LABEL_COLORS, OBSERVABLE_10, windowBucket } from '../lib/labels'
import { fmtDate, fmtInt } from '../lib/format'
import WindowMarker from './WindowMarker.vue'

const props = defineProps({
  messageId: { type: Number, required: true },
})
const emit = defineEmits(['close'])

const route = useRoute()
const router = useRouter()
const messages = useMessagesStore()
const filters = useFiltersStore()
const ui = useUiStore()

// --- detail fetch ---
const detail = ref(null)
const loading = ref(false)
const error = ref(null)

async function load(id) {
  loading.value = true
  error.value = null
  try {
    detail.value = await get(`/messages/${id}`)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    detail.value = null
  } finally {
    loading.value = false
  }
}

watch(
  () => props.messageId,
  (id) => {
    if (id != null) load(id)
  },
  { immediate: true },
)

// --- prev / next stepping through the current filtered+sorted result set ---
// The messages store's `items` array IS that set (same filters/sort, paged in).
const curIdx = computed(() =>
  messages.items.findIndex((x) => x.id === props.messageId),
)

const posText = computed(() => {
  const n = messages.total
  // Deep link to a message not in the loaded set: "–/{n}", both arrows off.
  return curIdx.value >= 0 ? `${curIdx.value + 1}/${n} in view` : `–/${n}`
})

const prevDisabled = computed(() => curIdx.value <= 0)
const nextDisabled = computed(() => {
  const idx = curIdx.value
  if (idx < 0) return true
  // Enabled at the last loaded row only when more pages can still be fetched.
  return idx >= messages.items.length - 1 && !messages.hasMore
})

function goTo(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}

function drawerPrev() {
  const idx = curIdx.value
  if (idx > 0) goTo(messages.items[idx - 1].id)
}

async function drawerNext() {
  const idx = curIdx.value
  if (idx < 0) return
  if (idx < messages.items.length - 1) {
    goTo(messages.items[idx + 1].id)
  } else if (messages.hasMore) {
    // At the last loaded row but more exist: pull the next page, then step.
    await messages.loadMore()
    if (idx + 1 < messages.items.length) goTo(messages.items[idx + 1].id)
  }
}

// --- metadata ---
const fromName = computed(() => {
  const d = detail.value
  if (!d) return ''
  return d.person?.name || d.from?.display_name || d.from?.address || ''
})
const fromEmail = computed(() => detail.value?.from?.address || '')

const dateFull = computed(() => {
  const iso = detail.value?.date
  if (!iso) return ''
  const dt = new Date(iso)
  return Number.isNaN(dt.getTime()) ? String(iso) : dt.toUTCString()
})

// Reply-timing tooltips: the thresholds live in store.py (TIMING_*_CPM).
const TIMING_TITLES = {
  implausible:
    'New text implies ≥ 250 chars/minute since the parent message — too fast to have been composed in the window',
  suspicious: 'New text implies ≥ 100 chars/minute since the parent message',
  normal: 'New text implies < 100 chars/minute since the parent message',
}
const timingTitle = computed(() => TIMING_TITLES[detail.value?.timing] || '')

// --- analysis card ---
const scored = computed(() => {
  const sc = detail.value?.score
  return sc != null && sc.fraction_ai != null
})
// The prediction bucket (Human / Mixed / AI) and its pill colour.
const predShort = computed(() => detail.value?.score?.prediction_short || '')
const predBg = computed(() => LABEL_COLORS[predShort.value] || LABEL_COLORS.unscored)
const headline = computed(() => detail.value?.score?.headline || '')

// The engine that produced the verdict. Named because a second one is coming.
const engine = computed(() => {
  const sc = detail.value?.score
  if (!sc) return ''
  return `Pangram detector ${sc.detector_version || '?'}`
})
const scoredAt = computed(() => {
  const sc = detail.value?.score
  return sc?.scored_at ? `scored ${fmtDate(sc.scored_at)}` : ''
})

// The part check: present when the verdict was on text written in several
// parts between quoted passages and each part was scored separately. When the
// parts' combined verdict differed it replaced the whole-text verdict, and the
// windows below still describe the whole text.
const partCheck = computed(() => detail.value?.score?.part_check || null)
const fmtAi = (v) => (v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(2))
const partRows = computed(() =>
  (partCheck.value?.parts || []).map((p, i) => ({
    index: i + 1,
    words: p.words == null ? '—' : fmtInt(p.words),
    ai: fmtAi(p.fraction_ai),
    label: p.label || '—',
    color: LABEL_COLORS[p.label] || LABEL_COLORS.unscored,
  })),
)
const partCheckNote = computed(() => {
  const pc = partCheck.value
  if (!pc) return ''
  const whole = pc.whole || {}
  const combined = pc.combined || {}
  const n = (pc.parts || []).length
  const wholeDesc = `${whole.label || '—'} (AI ${fmtAi(whole.fraction_ai)})`
  const combinedDesc = `${combined.label || '—'} (AI ${fmtAi(combined.fraction_ai)})`
  return pc.replaced
    ? `The text is written in ${n} parts between quoted passages. Scored as one text it was ${wholeDesc}; ` +
        `scored part by part, weighted by word count, it is ${combinedDesc}, which replaces the whole-text verdict. ` +
        'The windows below describe the whole text.'
    : `The text is written in ${n} parts between quoted passages. Scored part by part, weighted by word count, ` +
        `it is ${combinedDesc}, the same label as the whole-text verdict ${wholeDesc}, which stands.`
})

// One row per Pangram window, for the analysis table and the text markers.
const windows = computed(() =>
  (detail.value?.score?.windows || []).map((w) => {
    const s = Number(w.ai_assistance_score)
    const h = Number(w.humanizer_score)
    const bucket = windowBucket(w.label)
    return {
      ...w,
      score: Number.isFinite(s) ? s.toFixed(2) : '—',
      humanizerScoreStr: Number.isFinite(h) ? h.toFixed(2) : '—',
      charsStr: w.chars == null ? '—' : fmtInt(w.chars),
      // The verdict colour is used only for the table's label swatch; the
      // numbers and brackets are grey (see WindowMarker).
      labelColor: bucket ? LABEL_COLORS[bucket] : LABEL_COLORS.unscored,
      located: w.start != null && w.end != null,
    }
  }),
)

// The humanizer fields arrived with the detector's v4 model: rows scored under
// v3 carry null for both, so the column is shown only when at least one window
// has a verdict. Its swatch is the one Observable 10 colour the label
// vocabulary does not use, so it cannot be read as a label.
const anyHumanized = computed(() => windows.value.some((w) => w.is_humanized != null))
const HUMANIZED_COLOR = OBSERVABLE_10.purple

// The window under the pointer, highlighted everywhere it appears: its number
// boxes in the table, in the text and beside its bracket, and the bracket.
const activeWindow = ref(null)
const WIRE_IDLE = OBSERVABLE_10.grey
const WIRE_ACTIVE = OBSERVABLE_10.lightBlue

// Scroll a window's inline marker into view and flash it. The scroll is
// deliberately instant: smooth scrolling is a no-op wherever the browser or the
// user's motion preference disables it, which loses the jump entirely.
const flashed = ref(null)
function goToWindow(win) {
  const el = document.getElementById(`win-marker-${win.index}`)
  if (!el) return
  el.scrollIntoView({ block: 'center' })
  flashed.value = win.index
  setTimeout(() => {
    if (flashed.value === win.index) flashed.value = null
  }, 1200)
}

// Wording for "Not scored (…)" and for a missing extraction elsewhere.
const extStatus = computed(() =>
  detail.value?.extraction ? detail.value.extraction.status : 'no extraction',
)

// --- text card meta ---
// The ignored-line count lives on the toggle, not here.
const extMeta = computed(() => {
  const ex = detail.value?.extraction
  if (!ex) return 'no extraction'
  if (ex.status === 'ok') {
    return (
      ex.status + ' · ' + ex.method + ' · ' + (ex.char_count || 0).toLocaleString() + ' chars'
    )
  }
  return ex.status
})

// --- text card ---
// One box, two views. Off (the default) it shows only what the detector saw. On,
// it shows the whole message with everything the detector did not see dimmed —
// quoted replies, signatures, the greeting lines the scoring stage drops. No
// distinction is drawn between what extraction removed and what post-processing
// removed: either way the detector never saw it.
const showIgnored = ref(false)

const extractedLines = computed(() => {
  const text = detail.value?.extraction?.extracted_text
  return text ? text.split('\n') : []
})
const ignoredSet = computed(() => new Set(detail.value?.extraction?.ignored_lines || []))
const rawBodyLines = computed(() => (detail.value?.raw_body || '').split('\n'))

// Where each extracted line sits in the raw body. The extracted text is the
// message's own new text, so its lines appear in the raw body in order — walk
// both and match on the trimmed line. Quoted copies never match, since they
// carry their '>' prefix.
const extractedToRaw = computed(() => {
  const map = new Map()
  const raw = rawBodyLines.value
  let next = 0
  extractedLines.value.forEach((line, i) => {
    const key = line.trim()
    if (!key) return
    for (let j = next; j < raw.length; j++) {
      if (raw[j].trim() === key) {
        map.set(i, j)
        next = j + 1
        return
      }
    }
  })
  return map
})

// Whether the raw body can stand in for the whole message. It cannot when the
// extracted text came from the HTML part: those lines are nowhere in the
// plain-text body, so the full-message view falls back to the extracted text.
const rawAligns = computed(() => {
  const wanted = extractedLines.value.filter((l) => l.trim()).length
  if (!wanted) return false
  return extractedToRaw.value.size >= wanted / 2
})

const ignoredCount = computed(() => {
  if (!rawAligns.value) return ignoredSet.value.size
  const shown = new Set(
    [...extractedToRaw.value.entries()].filter(([e]) => !ignoredSet.value.has(e)).map(([, r]) => r),
  )
  return rawBodyLines.value.filter((l, j) => l.trim() && !shown.has(j)).length
})

// Give a display row its window markers and wire segments.
//
// Wire geometry: a window's bracket runs from its first line to its last. Where
// several windows touch one line — one ending as the next begins — the line's
// height is shared between them in order, so one bracket closes and the next
// opens on that line.
// `colShift` corrects the marker offset when the rendered line is the raw one:
// window columns are offsets into the extracted line, which may be indented
// differently (the two are matched on their trimmed form).
function decorate(text, extLine, colShift = 0) {
  const located = extLine == null ? [] : windows.value.filter((w) => w.located)
  const covering = located.filter((w) => w.start.line <= extLine && w.end.line >= extLine)
  const share = covering.length || 1
  const wires = covering.map((w, k) => ({
    win: w,
    top: `${(k / share) * 100}%`,
    height: `${(1 / share) * 100}%`,
    isStart: w.start.line === extLine,
    isEnd: w.end.line === extLine,
  }))

  // Split the line at each window start so a marker can sit inline there.
  const starts = located
    .filter((w) => w.start.line === extLine)
    .map((w) => ({ col: Math.max(0, Math.min(w.start.col + colShift, text.length)), win: w }))
    .sort((a, b) => a.col - b.col)
  const parts = []
  let cursor = 0
  for (const s of starts) {
    if (s.col > cursor) parts.push({ text: text.slice(cursor, s.col) })
    parts.push({ marker: s.win })
    cursor = s.col
  }
  parts.push({ text: text.slice(cursor) || (parts.length ? '' : ' ') })

  return { parts, wires }
}

// The rendered lines: the whole message when "Show ignored" is on (raw body
// where it aligns, otherwise the extracted text), and only the analysed lines
// when it is off.
const textRows = computed(() => {
  if (!extractedLines.value.length) return []

  if (showIgnored.value && rawAligns.value) {
    const rawToExt = new Map([...extractedToRaw.value.entries()].map(([e, r]) => [r, e]))
    const indent = (s) => s.length - s.trimStart().length
    return rawBodyLines.value.map((text, j) => {
      const extLine = rawToExt.has(j) ? rawToExt.get(j) : null
      const seen = extLine != null && !ignoredSet.value.has(extLine)
      const shift = seen ? indent(text) - indent(extractedLines.value[extLine]) : 0
      return {
        key: `r${j}`,
        num: j + 1,
        // Blank lines carry no text to dim: banding them just stripes the view.
        dimmed: !seen && !!text.trim(),
        ...decorate(text, seen ? extLine : null, shift),
      }
    })
  }

  return extractedLines.value
    .map((text, i) => ({
      key: `e${i}`,
      num: i + 1,
      extLine: i,
      dimmed: ignoredSet.value.has(i),
      ...decorate(text, ignoredSet.value.has(i) ? null : i),
    }))
    .filter((row) => showIgnored.value || !row.dimmed)
})

// --- interactions ---
function filterList() {
  if (detail.value?.list) filters.setFilter('list', detail.value.list)
  emit('close')
}

function onKey(e) {
  // The prototype binds no drawer keys; Escape→close is a sensible addition.
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div>
    <div class="drawer-overlay" @click="emit('close')"></div>
    <div class="drawer-panel">
      <div class="drawer-topbar">
        <button
          class="drawer-nav-btn"
          :disabled="prevDisabled"
          title="Previous message"
          @click="drawerPrev"
        >
          ↑
        </button>
        <button
          class="drawer-nav-btn"
          :disabled="nextDisabled"
          title="Next message"
          @click="drawerNext"
        >
          ↓
        </button>
        <span class="drawer-pos">{{ posText }}</span>
        <span style="flex: 1;"></span>
        <button class="drawer-close-btn" @click="emit('close')">Close ✕</button>
      </div>

      <div class="drawer-body">
        <div v-if="loading && !detail" style="color: #8a929b;">Loading…</div>
        <div v-else-if="error" style="color: #8a929b;">{{ error }}</div>

        <template v-else-if="detail">
          <h2 style="font-size: 15px; margin: 0 0 8px; line-height: 1.35;">
            {{ detail.subject }}
          </h2>

          <div
            style="display: grid; grid-template-columns: 88px 1fr; gap: 3px 12px; font-size: 12px; margin-bottom: 12px;"
          >
            <span class="meta-key">List</span>
            <span
              ><a href="#" @click.prevent="filterList">{{ detail.list }}</a></span
            >

            <template v-if="!ui.anonymous">
              <span class="meta-key">From</span>
              <span
                >{{ fromName }}
                <span
                  style="color: #8a929b; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11px;"
                  >&lt;{{ fromEmail }}&gt;</span
                ></span
              >
            </template>

            <span class="meta-key">Date</span>
            <span>{{ dateFull }}</span>

            <template v-if="detail.timing">
              <span class="meta-key">Timing</span>
              <span
                ><span class="timing-pill" :class="'timing-' + detail.timing" :title="timingTitle">{{
                  detail.timing
                }}</span></span
              >
            </template>

            <span class="meta-key">Message-ID</span>
            <span
              style="font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 10.5px; word-break: break-all;"
              >{{ detail.message_id }}</span
            >
          </div>

          <!-- Analysis card -->
          <div class="drawer-card">
            <div class="analysis-head">
              <span style="font-size: 11.5px; font-weight: 700;">Analysis</span>
              <span v-if="scored" class="pred-pill" :style="{ background: predBg }">{{
                predShort
              }}</span>
              <span v-if="scored" style="font-size: 11.5px;">{{ headline }}</span>
              <span v-if="scored" class="analysis-meta">{{ engine }}</span>
              <span v-if="scored" class="analysis-meta">· {{ scoredAt }}</span>
            </div>

            <template v-if="partCheck">
              <div class="win-head">Checked by parts</div>
              <p class="win-note">{{ partCheckNote }}</p>
              <table class="win-table">
                <thead>
                  <tr>
                    <th style="width: 34px;">Part</th>
                    <th style="width: 62px; text-align: right;">Words</th>
                    <th style="width: 54px; text-align: right;">AI</th>
                    <th>Label</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="p in partRows" :key="p.index">
                    <td>{{ p.index }}</td>
                    <td style="text-align: right;">{{ p.words }}</td>
                    <td style="text-align: right;">{{ p.ai }}</td>
                    <td>
                      <span class="part-swatch" :style="{ background: p.color }"></span>{{ p.label }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </template>

            <template v-if="windows.length">
              <div class="win-head">Windows</div>
              <p class="win-note">
                Pangram splits the analysed text into non-overlapping windows of at most 500
                tokens, and returns a score and a confidence for each window.
              </p>
            </template>

            <table v-if="windows.length" class="win-table">
              <thead>
                <tr>
                  <th style="width: 34px;">#</th>
                  <th style="width: 62px; text-align: right;">Chars</th>
                  <th style="width: 54px; text-align: right;">Score</th>
                  <th style="width: 74px;">Confidence</th>
                  <th
                    v-if="anyHumanized"
                    style="width: 90px;"
                    title="Whether the window's text reads as AI output passed through a humanizer tool, and the humanizer score (0–1)"
                  >
                    Humanized
                  </th>
                  <th>Label</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="w in windows" :key="w.index">
                  <td>
                    <WindowMarker
                      :win="w"
                      variant="table"
                      :active="activeWindow === w.index"
                      :clickable="w.located"
                      @activate="activeWindow = w.index"
                      @deactivate="activeWindow = null"
                      @jump="goToWindow"
                    />
                  </td>
                  <td style="text-align: right;">{{ w.charsStr }}</td>
                  <td style="text-align: right;">{{ w.score }}</td>
                  <td>{{ w.confidence || '—' }}</td>
                  <td v-if="anyHumanized">
                    <template v-if="w.is_humanized"
                      ><span class="win-swatch" :style="{ background: HUMANIZED_COLOR }"></span>yes
                      {{ w.humanizerScoreStr }}</template
                    >
                    <template v-else>{{ w.humanizerScoreStr }}</template>
                  </td>
                  <td>
                    <span class="win-swatch" :style="{ background: w.labelColor }"></span
                    >{{ w.label || '—' }}
                  </td>
                </tr>
              </tbody>
            </table>

            <div v-if="!scored" style="font-size: 11.5px; color: #8a929b;">
              Not scored ({{ extStatus }}).
            </div>
          </div>

          <!-- Text card: one box. "Show ignored" widens it from the analysed
               text to the whole message, dimming what the detector never saw. -->
          <div class="drawer-card" style="margin-bottom: 0;">
            <div class="text-head">
              <span style="font-size: 11.5px; font-weight: 700;">Text</span>
              <span class="analysis-meta">· {{ extMeta }}</span>
              <span style="flex: 1;"></span>
              <label class="show-toggle">
                <input type="checkbox" v-model="showIgnored" />
                Show ignored<span v-if="ignoredCount"> ({{ ignoredCount }})</span>
              </label>
            </div>

            <div v-if="showIgnored && !rawAligns" class="text-note">
              The extracted text is not in the plain-text body (HTML message): showing the
              extracted text with its ignored lines dimmed.
            </div>

            <div v-if="textRows.length" class="code-block" style="background: #fbfdff;">
              <div v-for="ln in textRows" :key="ln.key" class="code-line">
                <span class="code-gutter">{{ ln.num }}</span>
                <span
                  class="code-text"
                  :class="{ 'code-text-ignored': ln.dimmed }"
                  :title="ln.dimmed ? 'Not sent to the checking service' : ''"
                >
                  <template v-for="(p, i) in ln.parts" :key="i">
                    <WindowMarker
                      v-if="p.marker"
                      :win="p.marker"
                      variant="box"
                      :marker-id="`win-marker-${p.marker.index}`"
                      :active="activeWindow === p.marker.index || flashed === p.marker.index"
                      @activate="activeWindow = p.marker.index"
                      @deactivate="activeWindow = null"
                    />
                    <template v-else>{{ p.text }}</template>
                  </template>
                </span>
                <span class="code-wire">
                  <span
                    v-for="w in ln.wires"
                    :key="w.win.index"
                    class="wire-seg"
                    :style="{
                      top: w.top,
                      height: w.height,
                      borderColor: activeWindow === w.win.index ? WIRE_ACTIVE : WIRE_IDLE,
                    }"
                  >
                    <span
                      v-if="w.isStart"
                      class="wire-arm wire-arm-top"
                      :style="{ background: activeWindow === w.win.index ? WIRE_ACTIVE : WIRE_IDLE }"
                    ></span>
                    <span
                      v-if="w.isEnd"
                      class="wire-arm wire-arm-bottom"
                      :style="{ background: activeWindow === w.win.index ? WIRE_ACTIVE : WIRE_IDLE }"
                    ></span>
                    <WindowMarker
                      v-if="w.isStart"
                      :win="w.win"
                      variant="wire"
                      :active="activeWindow === w.win.index"
                      @activate="activeWindow = w.win.index"
                      @deactivate="activeWindow = null"
                    />
                  </span>
                </span>
              </div>
            </div>
            <div v-else style="font-size: 11.5px; color: #8a929b;">(no extracted text)</div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.drawer-nav-btn {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid #e2e5e9;
  border-radius: 3px;
  background: #ffffff;
  cursor: pointer;
}
.drawer-nav-btn:disabled {
  cursor: default;
  opacity: 0.4;
}
.drawer-pos {
  font-size: 10.5px;
  color: #8a929b;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}
.meta-key {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #8a929b;
  font-weight: 700;
  padding-top: 2px;
}
.drawer-card {
  border: 1px solid #e2e5e9;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.code-block {
  border: 1px solid #e2e5e9;
  border-radius: 4px;
  padding: 8px 10px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 11px;
  line-height: 1.55;
}
.code-gutter {
  flex: none;
  width: 3ch;
  text-align: right;
  padding-right: 9px;
  margin-right: 9px;
  border-right: 1px solid #e2e5e9;
  color: #b3b9c0;
  user-select: none;
}

/* --- analysis card --- */
.analysis-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.pred-pill {
  padding: 0 7px;
  border-radius: 3px;
  font-size: 10.5px;
  font-weight: 700;
  line-height: 16px;
  color: #ffffff;
}
.analysis-meta {
  font-size: 10.5px;
  color: #8a929b;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}
/* Reply-timing classification band as a pill; the message table shows the
   chars/minute rate the band was derived from instead. */
.timing-pill {
  padding: 0 7px;
  border-radius: 3px;
  font-size: 10.5px;
  font-weight: 700;
  line-height: 16px;
  text-transform: capitalize;
}
.timing-implausible {
  background: #c93a3a;
  color: #ffffff;
}
.timing-suspicious {
  background: #f2c744;
  color: #4a3600;
}
.timing-normal {
  padding: 0;
  font-weight: 400;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 10.5px;
  color: #8a929b;
  text-transform: none;
}
/* Section heading and its one-line note above the per-window table. */
.part-swatch {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 6px;
  vertical-align: baseline;
}
.win-head {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #626a72;
  margin-bottom: 3px;
}
.win-note {
  margin: 0 0 6px;
  font-size: 10.5px;
  color: #8a929b;
}
.win-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.win-table th {
  text-align: left;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #8a929b;
  padding: 0 6px 3px 0;
  border-bottom: 1px solid #eef0f3;
}
.win-table td {
  padding: 2px 6px 2px 0;
  border-bottom: 1px solid #f5f6f8;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}
.win-swatch {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 2px;
  margin-right: 5px;
}

/* --- text card --- */
.text-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.show-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: #626a72;
  cursor: pointer;
  user-select: none;
}
.text-note {
  font-size: 10.5px;
  color: #8a929b;
  margin-bottom: 6px;
}
.code-line {
  display: flex;
  align-items: stretch;
}
.code-text {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.code-text-ignored {
  background: #eef0f3;
  color: #8a929b;
  opacity: 0.75;
  border-radius: 2px;
}

/* The wire gutter: one bracket per window, down the right-hand side. */
.code-wire {
  position: relative;
  flex: none;
  width: 26px;
  margin-left: 8px;
}
.wire-seg {
  position: absolute;
  left: 4px;
  border-left: 2px solid;
}
.wire-arm {
  position: absolute;
  left: -2px;
  width: 6px;
  height: 2px;
}
.wire-arm-top {
  top: 0;
}
.wire-arm-bottom {
  bottom: 0;
}
</style>
