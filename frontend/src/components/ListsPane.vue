<script setup>
// The left / lower "Lists" pane. Two mutually exclusive modes driven by the
// shared filter state:
//
//   1. Lists index (default)  — every list with a mix bar (GET /api/lists).
//      "+ Add list" and "Regenerate index" live in the pane header, in every
//      mode (POST /api/lists/regenerate).
//   2. List stats (a `list` filter) — per-list aggregates from GET /api/summary
//      (stat tiles, detection-mix summary, full-history rug, Add footer).
//
// "Run process ($)" buttons (the Add-list form and the Add popover) do not
// pull-and-score in one call. They close their own UI and open the
// RunProcessModal, which drives the three pipeline stages sequentially via
// separate endpoints: fetch (POST /api/pull/fetch or /api/pull/range/fetch),
// then extract (POST /api/extract), then check (POST /api/score). The Add
// popover opens from each index row's "Add" button and from the list-stats
// footer's "Add" button; only "Regenerate index" keeps its older single-call
// flow.
//
// Sender (person/address) details live in the Senders pane, not here.
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { get, postJson } from '../api'
import { fmtDate, fmtInt } from '../lib/format'
import { TIMELINE_BUCKETS, aiCount } from '../lib/labels'
import { useFiltersStore } from '../stores/filters'
import MixBar from './MixBar.vue'
import MixSummary from './MixSummary.vue'
import RunProcessModal from './RunProcessModal.vue'
import ThreadGraph from './ThreadGraph.vue'
import TimelineRug from './TimelineRug.vue'

const filters = useFiltersStore()
const route = useRoute()
const router = useRouter()

// --- mode -------------------------------------------------------------------
const mode = computed(() => (filters.list ? 'list' : 'index'))
const contextSub = computed(() => (mode.value === 'list' ? 'per-list aggregates' : 'lists index'))

// --- data -------------------------------------------------------------------
const lists = ref([]) // [{name, message_count, label_counts, last_synced_at, earliest_message_at, ...}]
const summary = ref(null) // GET /api/summary for the selected list
const summaryLoading = ref(false)
const summaryError = ref(null)

async function loadLists() {
  try {
    const data = await get('/lists')
    lists.value = data?.lists || []
  } catch {
    lists.value = []
  }
  loadIndexTimelines()
}

// --- per-list history rugs (index rows) ---------------------------------------
// GET /api/lists/timelines without a list returns every list's dated messages
// as slim [id, t, bucket] points plus the corpus-wide dated extent — the shared
// x-domain, so every rug in the index places a given month at the same
// position. One fetch decorates all rows; refreshed with loadLists so the rugs
// track the same runs the counts do.
const indexRugs = ref({}) // list name -> [{id, t, bucket}]
const indexRugStart = ref(null) // shared domain, epoch ms (null → per-rug extent)
const indexRugEnd = ref(null)
let indexRugToken = 0
async function loadIndexTimelines() {
  const token = ++indexRugToken
  try {
    const data = await get('/lists/timelines')
    if (token !== indexRugToken) return
    indexRugStart.value = data?.start != null ? data.start * 1000 : null
    indexRugEnd.value = data?.end != null ? data.end * 1000 : null
    const byName = {}
    for (const entry of data?.lists || []) {
      byName[entry.list] = (entry.points || []).map(([id, t, bucket]) => ({
        id,
        t: t * 1000,
        bucket: TIMELINE_BUCKETS[bucket] || 'unscored',
      }))
    }
    indexRugs.value = byName
  } catch {
    // The rugs decorate rows the index already shows; a failed fetch just
    // leaves the bars alone.
  }
}

// A binned column of an index row's rug was clicked: filter the messages pane
// to that list and the bin's date span.
function applyIndexRugRange(list, range) {
  filters.patch({ list, date_from: range.from, date_to: range.to })
}

// The card aggregates over the list alone, regardless of the other filters
// active on the messages table.
const summaryParams = computed(() => (mode.value === 'list' ? { list: filters.list } : null))

let summaryToken = 0
async function loadSummary() {
  const params = summaryParams.value
  if (!params) return
  const token = ++summaryToken
  summaryLoading.value = true
  summaryError.value = null
  try {
    const data = await get('/summary', params)
    if (token === summaryToken) summary.value = data
  } catch (err) {
    if (token === summaryToken) {
      summary.value = null
      summaryError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    if (token === summaryToken) summaryLoading.value = false
  }
}

// --- rug plot (the selected list's full history) -----------------------------
// GET /api/lists/timelines?list= returns every message as a slim point
// [id, epoch-seconds, bucket, subject]; TimelineRug adapts to the volume, so
// no cap applies. Undated messages carry no point and are reported separately.
const rugPoints = ref([])
const rugUndated = ref(0)
let rugToken = 0
async function loadRug() {
  if (!filters.list) return
  const token = ++rugToken
  try {
    const data = await get('/lists/timelines', { list: filters.list })
    if (token !== rugToken) return
    const entry = data?.lists?.[0]
    rugUndated.value = entry?.undated || 0
    rugPoints.value = (entry?.points || []).map(([id, t, bucket, subject]) => ({
      id,
      t: t * 1000,
      bucket: TIMELINE_BUCKETS[bucket] || 'unscored',
      subject: subject || '(no subject)',
    }))
  } catch {
    if (token === rugToken) {
      rugPoints.value = []
      rugUndated.value = 0
    }
  }
}

function openRugMessage(id) {
  router.push({ path: `/messages/${id}`, query: route.query })
}

// A binned rug column was clicked: filter the messages pane (already scoped to
// this list) to the bin's date span.
function applyRugRange(range) {
  filters.patch({ date_from: range.from, date_to: range.to })
}

// The Timelines screen: one adaptive rug per list with messages, stacked.
function openTimelines() {
  router.push({ path: '/timelines', query: route.query })
}

// --- thread graph (the list's messages grouped into reply threads) -----------
// The graph is fetched lazily: only when its lightbox opens, and again when the
// window slider is released. The panel itself shows just the button that opens
// it, so neither mounting the pane, switching list nor a pipeline run pays for
// the query.
//
// GET /api/lists/thread-graph?list=&start=&end= takes 0-based inclusive message
// ranks in receipt order over the whole list (0 = furthest back) and returns
// {list, list_total, start, end, total, first_date, last_date, threads}.
// Omitting start/end asks for the whole list, and an explicit span is served at
// whatever width it asks for — no maximum is imposed. The echoed start/end are
// still the effective window (the server holds the range inside the list's own
// bounds), so the slider syncs to the response rather than to what it asked for.
const threadGraph = ref(null) // GET /api/lists/thread-graph payload
const graphOpen = ref(false) // the 80%-wide lightbox
const graphLoading = ref(false)
const graphError = ref('')
const winStart = ref(null) // slider handles: 0-based receipt ranks, inclusive
const winEnd = ref(null)

let graphToken = 0
async function loadThreadGraph(start = null, end = null) {
  if (!filters.list) return
  const token = ++graphToken
  graphLoading.value = true
  graphError.value = ''
  try {
    const data = await get('/lists/thread-graph', { list: filters.list, start, end })
    if (token !== graphToken) return
    threadGraph.value = data
    winStart.value = data?.start ?? null
    winEnd.value = data?.end ?? null
    cacheRankDates(data)
  } catch (err) {
    if (token === graphToken) {
      threadGraph.value = null
      graphError.value = err instanceof Error ? err.message : String(err)
    }
  } finally {
    if (token === graphToken) graphLoading.value = false
  }
}

// --- rank → date lookup (for the slider handles' hover) ----------------------
// Each response carries every message in the window with its `seq`, the 0-based
// rank inside that window, so absolute rank = response start + seq. Those dates
// are kept in an array indexed by absolute rank and reused to say what date a
// handle is sitting on while it is dragged.
//
// A response covering ranks 0..list_total-1 — which is what the first open now
// asks for, the default window being the whole list — is the complete lookup, so
// it is cached and later narrower windows leave it alone. Until such a response
// lands, a partial window fills only its own ranks. The cache is discarded with
// the rest of the graph state when the selected list changes.
const rankDates = ref([]) // absolute receipt rank -> ISO date string (holes allowed)
const rankDatesFull = ref(false) // the cache spans the whole list

function cacheRankDates(data) {
  if (!data) return
  const start = data.start ?? 0
  const isFull = start === 0 && data.end === (data.list_total || 0) - 1
  if (rankDatesFull.value && !isFull) return
  const dates = []
  for (const t of data.threads || []) {
    for (const m of t.messages || []) {
      if (m.date) dates[start + m.seq] = m.date
    }
  }
  rankDates.value = dates
  rankDatesFull.value = isFull
}

// The date a rank sits on, formatted, or '' when nothing is known. An undated
// message (and any rank outside the cached window) has no date of its own, so
// the nearest known rank on either side stands in rather than a wrong or blank
// stamp; the scan stops at the ends of the cache.
function dateForRank(rank) {
  const dates = rankDates.value
  if (!dates.length || rank == null) return ''
  if (dates[rank]) return fmtDate(dates[rank])
  for (let d = 1; d <= dates.length; d++) {
    const lo = rank - d
    const hi = rank + d
    if (lo < 0 && hi >= dates.length) break
    if (lo >= 0 && dates[lo]) return fmtDate(dates[lo])
    if (hi < dates.length && dates[hi]) return fmtDate(dates[hi])
  }
  return ''
}

// Reopening keeps the window the slider was left on; the first open (or one
// after the list changed) takes the server's default, which is the whole list.
function openGraph() {
  graphOpen.value = true
  loadThreadGraph(winStart.value, winEnd.value)
}

// Slider geometry. Ranks run 0..list_total-1; an empty list has no window at
// all (list_total 0, start/end null) and shows no slider.
const graphTotal = computed(() => threadGraph.value?.list_total || 0)
const winMax = computed(() => Math.max(graphTotal.value - 1, 0))
const hasWindow = computed(
  () => graphTotal.value > 0 && winStart.value !== null && winEnd.value !== null,
)
// Percentages for the highlighted span of the slider rail.
const winFillStyle = computed(() => {
  const span = winMax.value || 1
  const left = (winStart.value / span) * 100
  const right = (winEnd.value / span) * 100
  return { left: `${left}%`, width: `${Math.max(right - left, 0)}%` }
})
const winCaption = computed(() => {
  if (!hasWindow.value) return ''
  const span = `${fmtInt(winStart.value + 1)}–${fmtInt(winEnd.value + 1)}`
  return `messages ${span} of ${fmtInt(graphTotal.value)}`
})

// The two overlaid range inputs are clamped against each other so the left
// handle can never pass the right one. Vue will not re-patch an input whose
// bound value did not change, so a clamped drag is written back to the element
// directly, keeping the native thumb where the model says it is.
function setHandle(which, event) {
  const raw = parseInt(event.target.value, 10)
  if (!Number.isFinite(raw)) return
  if (which === 'start') winStart.value = Math.min(raw, winEnd.value)
  else winEnd.value = Math.max(raw, winStart.value)
  event.target.value = which === 'start' ? winStart.value : winEnd.value
}
// Refetch on release, not on every drag tick.
function commitWindow() {
  loadThreadGraph(winStart.value, winEnd.value)
}

// While a handle is held or dragged, a small bubble over the rail names the date
// that handle's rank sits on (the ranks themselves are in the caption below).
// It follows the handle by the same percentage the fill uses, and is suppressed
// entirely when no date is known for the rank.
const heldHandle = ref(null) // 'start' | 'end' while held, else null
function holdHandle(which) {
  heldHandle.value = which
}
function releaseHandle() {
  heldHandle.value = null
}
// A handle moved: keep showing the bubble for it. Arrow keys move a handle with
// no pointer down at all, so the move itself — not only the grab — raises it.
function onHandleInput(which, event) {
  heldHandle.value = which
  setHandle(which, event)
}
// A drag that ends off the input still has to lower the bubble, so the release
// is watched on the document (capture phase) for as long as a handle is held.
watch(heldHandle, (which) => {
  if (which) {
    document.addEventListener('pointerup', releaseHandle, true)
    document.addEventListener('pointercancel', releaseHandle, true)
  } else {
    document.removeEventListener('pointerup', releaseHandle, true)
    document.removeEventListener('pointercancel', releaseHandle, true)
  }
})
const handleTipRank = computed(() =>
  heldHandle.value === 'start' ? winStart.value : heldHandle.value === 'end' ? winEnd.value : null,
)
const handleTipDate = computed(() => (heldHandle.value ? dateForRank(handleTipRank.value) : ''))
const handleTipStyle = computed(() => {
  const span = winMax.value || 1
  const pct = Math.min(Math.max((handleTipRank.value / span) * 100, 0), 100)
  // Centred over the handle in the middle of the rail, and progressively
  // shifted to its own left/right edge towards the ends, so the bubble stays
  // inside the rail instead of hanging off it.
  return { left: `${pct}%`, transform: `translateX(-${pct}%)` }
})

// Opening a message from the lightbox: close it so the detail drawer is not
// buried under the overlay.
function openGraphMessage(id) {
  graphOpen.value = false
  openRugMessage(id)
}

function onGraphKeydown(e) {
  if (e.key === 'Escape') graphOpen.value = false
}
watch(graphOpen, (open) => {
  if (open) document.addEventListener('keydown', onGraphKeydown)
  else document.removeEventListener('keydown', onGraphKeydown)
})

// Refetch whenever the selected list changes. The graph is not refetched, only
// discarded: the next open loads it for the new list.
watch(
  () => filters.list,
  () => {
    graphOpen.value = false
    threadGraph.value = null
    graphError.value = ''
    winStart.value = null
    winEnd.value = null
    rankDates.value = []
    rankDatesFull.value = false
    heldHandle.value = null
    loadSummary()
    loadRug()
  },
)

onMounted(() => {
  loadLists()
  loadSummary()
  loadRug()
})

// --- small helpers ----------------------------------------------------------
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
// Compact "MMM DD HH:mm" stamp for last_synced_at (ISO), in UTC like every
// other date on the dashboard, or "never".
function fmtSynced(iso) {
  if (!iso) return 'never'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const mm = MONTHS[d.getUTCMonth()]
  const dd = String(d.getUTCDate()).padStart(2, '0')
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mi = String(d.getUTCMinutes()).padStart(2, '0')
  return `${mm} ${dd} ${hh}:${mi}`
}

// --- pull / regenerate status (shared by index + list-stats footer) --------
const pullMsg = ref('')
const regenerating = ref(false)
const regenMsg = ref('')
const statusMsg = computed(() =>
  regenerating.value ? 'enumerating server folders…' : pullMsg.value || regenMsg.value,
)

// --- staged run modal (fetch → extract → check) -----------------------------
// The "Run process ($)" buttons drive the pipeline in three sequential POSTs,
// surfacing each stage's status in RunProcessModal. runProcess owns the
// reactive stage state; the modal is presentational. The Add-list form and the
// popover differ only in the fetch endpoint/body and detail formatter.
const STAGE_DEFS = [
  { key: 'fetch', label: 'Fetch' },
  { key: 'extract', label: 'Extract' },
  { key: 'check', label: 'Check' },
]
const processRunning = ref(false) // a stage is in flight → guard + disable Close
const processModalOpen = ref(false)
const processTitle = ref('')
const processStages = ref([])
const processModalTitle = computed(() => `Run process — ${processTitle.value}`)

function initStages() {
  processStages.value = STAGE_DEFS.map((d) => ({ ...d, status: 'pending', detail: '' }))
}
function setStage(key, status, detail) {
  const s = processStages.value.find((x) => x.key === key)
  if (!s) return
  s.status = status
  if (detail !== undefined) s.detail = detail
}
function errMsg(err) {
  return err instanceof Error ? err.message : String(err)
}

// Fetch-stage detail lines. Plain: the Add-list form flow; range: the popover
// flow (prepends the matched count, appends the cap note when the API capped).
function fetchDetailPlain(r) {
  return (
    `fetched ${fmtInt(r.fetched)} · duplicates ${fmtInt(r.duplicates)} · ` +
    `parse errors ${fmtInt(r.parse_errors)}`
  )
}
function fetchDetailRange(r) {
  let line =
    `matched ${fmtInt(r.matched)} · fetched ${fmtInt(r.fetched)} · ` +
    `duplicates ${fmtInt(r.duplicates)} · parse errors ${fmtInt(r.parse_errors)}`
  if (r.capped) line += ' · capped at 1,000'
  return line
}

// Run the three stages strictly in order. fetchFn returns the fetch response
// (which echoes the `limit` fed to extract/score); fetchDetailFn formats it. On
// any stage error the run stops, that stage shows the message, later stages stay
// pending, and Close is re-enabled. The pane refreshes whether or not the run
// completed: an error after (or during) the fetch stage may still have
// inserted messages.
async function runProcess(name, fetchFn, fetchDetailFn) {
  if (processRunning.value) return
  processTitle.value = name
  initStages()
  processModalOpen.value = true
  processRunning.value = true
  try {
    // Fetch
    setStage('fetch', 'running')
    let fetchRes
    try {
      fetchRes = await fetchFn()
    } catch (err) {
      setStage('fetch', 'error', errMsg(err))
      return
    }
    setStage('fetch', 'done', fetchDetailFn(fetchRes))

    const limit = fetchRes.limit
    if (limit === 0) {
      // Range matched nothing — nothing to extract or score.
      setStage('extract', 'done', 'nothing to process')
      setStage('check', 'done', 'nothing to process')
    } else {
      // Extract
      setStage('extract', 'running')
      let extractRes
      try {
        extractRes = await postJson('/extract', { limit })
      } catch (err) {
        setStage('extract', 'error', errMsg(err))
        return
      }
      setStage(
        'extract',
        'done',
        `extracted ${fmtInt(extractRes.extracted)} · empty ${fmtInt(extractRes.empty)}`,
      )

      // Check
      setStage('check', 'running')
      let scoreRes
      try {
        scoreRes = await postJson('/score', { limit })
      } catch (err) {
        setStage('check', 'error', errMsg(err))
        return
      }
      if (scoreRes.scoring_skipped) {
        setStage('check', 'skipped', 'skipped (no Pangram API key)')
      } else {
        setStage(
          'check',
          'done',
          `scored ${fmtInt(scoreRes.scored)} · cache hits ${fmtInt(scoreRes.cache_hits)} · ` +
            `API calls ${fmtInt(scoreRes.api_calls)} · too short ${fmtInt(scoreRes.too_short)}`,
        )
      }
    }
  } finally {
    processRunning.value = false
    await loadLists()
    if (mode.value === 'list') await Promise.all([loadSummary(), loadRug()])
  }
}

function closeProcessModal() {
  if (processRunning.value) return
  processModalOpen.value = false
}

// --- index mode -------------------------------------------------------------
// Two-state visibility toggle over the already-loaded lists array (client-side,
// not persisted). "Show active" keeps only tracked, recently-active lists;
// "Show all" shows every enumerated folder. Default: active.
const showActive = ref(true)
const ACTIVE_WINDOW_MS = 90 * 24 * 60 * 60 * 1000

// A list is "active" when it is tracked (message_count > 0) AND either has no
// server-activity timestamp yet (first check not run → stay visible) or its
// newest server message is within the last 90 days.
function isActiveList(l) {
  if (!(Number(l.message_count) > 0)) return false
  const lm = l.last_message_at
  if (lm == null) return true
  const t = new Date(lm).getTime()
  if (Number.isNaN(t)) return true // unparseable → keep visible rather than hide data
  return t >= Date.now() - ACTIVE_WINDOW_MS
}

// Search over the index, as the Senders pane searches senders: a
// case-insensitive substring over the list name or its server folder (the
// folder being to a list what an address is to a sender). The whole index is
// already loaded, so this filters in place and needs no debounce or refetch.
const listQuery = ref('')
function matchesQuery(l) {
  const needle = listQuery.value.trim().toLowerCase()
  if (!needle) return true
  return (
    String(l.name || '').toLowerCase().includes(needle) ||
    String(l.folder || '').toLowerCase().includes(needle)
  )
}

const visibleLists = computed(() =>
  showActive.value ? lists.value.filter(isActiveList) : lists.value,
)
const filteredLists = computed(() => visibleLists.value.filter(matchesQuery))
const totalListCount = computed(() => lists.value.length)
// Filtering hid everything, but the API did return rows → show a friendly hint
// instead of a bare, rowless index. The two causes need different hints: a
// search that matched nothing is not the same as a list index with nothing
// active in it.
const showActiveEmpty = computed(
  () => showActive.value && lists.value.length > 0 && visibleLists.value.length === 0,
)
const showSearchEmpty = computed(
  () =>
    !showActiveEmpty.value &&
    !!listQuery.value.trim() &&
    visibleLists.value.length > 0 &&
    filteredLists.value.length === 0,
)

// Index ordering. Three sortable captions — List (name), Msgs (message count,
// the default, descending) and Aggregate analysis (AI share). Clicking a new
// caption applies its natural first order (names ascending, counts and shares
// descending); clicking the active one flips the order.
const indexSort = ref('count') // 'count' | 'name' | 'ai'
const indexOrder = ref('desc')
function sortIndex(col, firstOrder) {
  if (indexSort.value === col) {
    indexOrder.value = indexOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    indexSort.value = col
    indexOrder.value = firstOrder
  }
}
const indexInd = (col) =>
  indexSort.value === col ? (indexOrder.value === 'asc' ? ' ▲' : ' ▼') : ''
const nameInd = computed(() => indexInd('name'))
const countInd = computed(() => indexInd('count'))
const aiInd = computed(() => indexInd('ai'))

const listRows = computed(() => {
  // Stable base: message count descending (the default order), so ties under
  // any other sort keep it.
  const sorted = [...filteredLists.value].sort(
    (a, b) => (b.message_count || 0) - (a.message_count || 0),
  )
  const dir = indexOrder.value === 'asc' ? 1 : -1
  if (indexSort.value === 'name') {
    sorted.sort((a, b) => dir * String(a.name || '').localeCompare(String(b.name || '')))
  } else if (indexSort.value === 'ai') {
    // Equal AI + Mixed counts — including the many lists sharing a 0 — fall
    // back to message count descending. The tie-break is part of the comparator
    // rather than left to the base order, so it points the same way whichever
    // direction the count itself is sorted in.
    sorted.sort((a, b) => {
      const countDiff = aiCount(a.label_counts) - aiCount(b.label_counts)
      if (countDiff !== 0) return dir * countDiff
      return (b.message_count || 0) - (a.message_count || 0)
    })
  } else if (indexOrder.value === 'asc') {
    sorted.reverse()
  }
  return sorted.map((l) => ({
    name: l.name,
    count: fmtInt(l.message_count || 0),
    counts: l.label_counts || {},
    // The history rug's points, once the shared timelines fetch has landed.
    history: indexRugs.value[l.name] || null,
    // Gated under the reliability floor: the mix bar's trailing grey segment.
    tooShort: l.too_short_count || 0,
    // Oldest stored message date for the list (the message's own date), or an
    // em-dash when the list has no dated messages.
    earliest: l.earliest_message_at ? fmtDate(l.earliest_message_at) : '—',
    synced: fmtSynced(l.last_synced_at),
  }))
})

const pullFormOpen = ref(false)
const pullName = ref('')
const pullCount = ref(50)
function openPullForm() {
  pullFormOpen.value = true
  pullMsg.value = ''
  regenMsg.value = ''
}
function cancelPull() {
  pullFormOpen.value = false
}
function submitPull() {
  const name = pullName.value.trim()
  if (!name) {
    pullMsg.value = 'enter a list name'
    return
  }
  let count = parseInt(pullCount.value, 10)
  if (!Number.isFinite(count)) count = 50
  count = Math.min(1000, Math.max(1, count))
  // Close the form and clear the name input, then drive the staged run in the
  // modal (fetch → extract → check).
  pullFormOpen.value = false
  pullName.value = ''
  runProcess(name, () => postJson('/pull/fetch', { list: name, count }), fetchDetailPlain)
}
// Summarise the POST /api/lists/regenerate response. Appends the per-list
// server-activity check counts when the backend reports them.
function regenSummary(c) {
  let msg =
    `${fmtInt(c.total)} lists · +${fmtInt(c.added)} added · ` +
    `+${fmtInt(c.restored)} restored · −${fmtInt(c.deleted)} removed`
  if (c.activity_checked) msg += ` · activity checked ${fmtInt(c.activity_checked)}`
  if (c.activity_failed > 0) msg += ` · activity failed ${fmtInt(c.activity_failed)}`
  return msg
}
async function regenerate() {
  if (regenerating.value) return
  regenerating.value = true
  regenMsg.value = ''
  pullMsg.value = ''
  try {
    const c = await postJson('/lists/regenerate', {})
    regenMsg.value = regenSummary(c)
    await loadLists()
  } catch (err) {
    regenMsg.value = err instanceof Error ? err.message : String(err)
  } finally {
    regenerating.value = false
  }
}

// --- add-and-check popover ---------------------------------------------------
// One popover open at a time, anchored under the Add button that opened it (an
// index row's or the list-stats footer's). Its two tabs
// preview server-side messages ("new since last fetch" / "before last fetch").
// Each "Run process ($)" button closes the popover and hands the chosen range
// to runProcess, which drives the staged fetch/extract/check run in the modal.
const popoverList = ref(null) // name of the list whose Add popover is open
const popoverTab = ref('new') // 'new' | 'before'
// Inline fixed-position style for the teleported popover (see toggleAddPopover).
const popoverStyle = ref({})
const POPOVER_WIDTH = 400

// Tab 1 — "new since last fetch".
const newPreview = ref(null) // {mode, list, total, shown, more, messages}
const newPreviewLoading = ref(false)
const newPreviewError = ref('')
const newCountInput = ref('all') // "all" or a positive integer (as a string)

// Tab 2 — "before last fetch".
const beforePreview = ref(null)
const beforePreviewLoading = ref(false)
const beforePreviewError = ref('')
const beforeCount = ref(25) // requested preview window (1..1000)
const beforePreviewedCount = ref(0) // the count the last successful preview used
const beforePreviewed = ref(false) // a preview has run → enable "Run process ($)"

function senderName(m) {
  return m.from_name || m.from_email || '(unknown)'
}
// The other half of the sender pair as a tooltip (email when a name is shown).
function senderTitle(m) {
  return m.from_name ? m.from_email || '' : ''
}

function resetPopover() {
  newPreview.value = null
  newPreviewError.value = ''
  newCountInput.value = 'all'
  beforePreview.value = null
  beforePreviewError.value = ''
  beforeCount.value = 25
  beforePreviewedCount.value = 0
  beforePreviewed.value = false
}

function closeAddPopover() {
  popoverList.value = null
}
// Fixed-position style anchored to the clicked Add button. The popover is
// teleported to <body>, so no ancestor overflow clips it; it right-aligns to
// the button, clamps to the viewport, and flips above when room below is tight.
function computePopoverStyle(btn) {
  const rect = btn.getBoundingClientRect()
  const gap = 4
  const margin = 8
  // Horizontal clamping is left to CSS (100vw resolves in the layout engine,
  // which knows the real viewport even in embedded webviews where
  // window.innerWidth reports 0).
  const style = {
    position: 'fixed',
    width: `min(${POPOVER_WIDTH}px, calc(100vw - ${2 * margin}px))`,
    left: `clamp(${margin}px, ${Math.round(rect.right - POPOVER_WIDTH)}px, calc(100vw - ${POPOVER_WIDTH + margin}px))`,
  }
  const viewH = window.innerHeight || document.documentElement.clientHeight
  const spaceBelow = viewH - rect.bottom
  // Open upward (anchored by its bottom edge) only when below is tight and
  // above has more room; without a usable JS viewport height (viewH 0) open
  // downward. The popover grows with its content either way.
  if (viewH && spaceBelow < 260 && rect.top > spaceBelow) {
    style.bottom = `calc(100vh - ${Math.round(rect.top - gap)}px)`
  } else {
    style.top = `${Math.round(rect.bottom + gap)}px`
  }
  return style
}

// Toggle from an "Add" button (index row or list-stats footer); clicking a
// different list's Add moves the popover there.
function toggleAddPopover(name, event) {
  if (popoverList.value === name) {
    closeAddPopover()
    return
  }
  if (event && event.currentTarget) {
    popoverStyle.value = computePopoverStyle(event.currentTarget)
  }
  popoverList.value = name
  popoverTab.value = 'new'
  resetPopover()
  loadNewPreview()
}
function setPopoverTab(tab) {
  popoverTab.value = tab
  // Lazily load tab 1 the first time it is shown (or after an error clears).
  if (tab === 'new' && !newPreview.value && !newPreviewLoading.value && !newPreviewError.value) {
    loadNewPreview()
  }
}

async function loadNewPreview() {
  const name = popoverList.value
  if (!name) return
  newPreviewLoading.value = true
  newPreviewError.value = ''
  try {
    newPreview.value = await postJson('/lists/preview', { list: name, mode: 'new' })
  } catch (err) {
    newPreview.value = null
    newPreviewError.value = err instanceof Error ? err.message : String(err)
  } finally {
    newPreviewLoading.value = false
  }
}

async function runBeforePreview() {
  const name = popoverList.value
  if (!name || beforePreviewLoading.value) return
  let count = parseInt(beforeCount.value, 10)
  if (!Number.isFinite(count)) count = 25
  count = Math.min(1000, Math.max(1, count))
  beforeCount.value = count
  beforePreviewLoading.value = true
  beforePreviewError.value = ''
  try {
    beforePreview.value = await postJson('/lists/preview', { list: name, mode: 'before', count })
    beforePreviewedCount.value = count
    beforePreviewed.value = true
  } catch (err) {
    beforePreview.value = null
    beforePreviewError.value = err instanceof Error ? err.message : String(err)
    beforePreviewed.value = false
  } finally {
    beforePreviewLoading.value = false
  }
}

// Restore the tab-1 fetch input to "all" when it is cleared.
function normaliseNewCount() {
  if (String(newCountInput.value).trim() === '') newCountInput.value = 'all'
}

// A "Run process ($)" button in the popover: compute the range the same way the
// preview tabs do, close the popover, and hand the run to the staged modal.
// 'new': input "all"/empty → null (all new). 'before': the previewed count.
function startPopoverProcess(mode) {
  const name = popoverList.value
  if (!name) return
  let count
  if (mode === 'new') {
    const raw = String(newCountInput.value).trim()
    if (raw === '' || raw === 'all') {
      count = null
    } else {
      count = parseInt(raw, 10)
      if (!Number.isFinite(count) || count < 1) count = null
    }
  } else {
    count = beforePreviewedCount.value
  }
  closeAddPopover()
  runProcess(name, () => postJson('/pull/range/fetch', { list: name, mode, count }), fetchDetailRange)
}

// Close on Escape or a click outside the open popover (the row's own Add button
// handles its own toggle). Capture phase so it runs regardless of @click.stop.
function onPopoverKeydown(e) {
  if (e.key === 'Escape') closeAddPopover()
}
function onPopoverDocClick(e) {
  if (!popoverList.value) return
  const el = e.target
  if (el.closest && (el.closest('.add-popover') || el.closest('.row-add-btn'))) return
  closeAddPopover()
}
// The anchor row can scroll out from under a fixed-position popover, so close
// on any scroll that does not originate inside the popover itself.
function onPopoverScroll(e) {
  if (!popoverList.value) return
  const el = e.target
  if (el && el.nodeType === 1 && el.closest && el.closest('.add-popover')) return
  closeAddPopover()
}
watch(popoverList, (open) => {
  if (open) {
    document.addEventListener('keydown', onPopoverKeydown)
    document.addEventListener('click', onPopoverDocClick, true)
    document.addEventListener('scroll', onPopoverScroll, true)
  } else {
    document.removeEventListener('keydown', onPopoverKeydown)
    document.removeEventListener('click', onPopoverDocClick, true)
    document.removeEventListener('scroll', onPopoverScroll, true)
  }
})
onUnmounted(() => {
  document.removeEventListener('keydown', onPopoverKeydown)
  document.removeEventListener('click', onPopoverDocClick, true)
  document.removeEventListener('scroll', onPopoverScroll, true)
  document.removeEventListener('keydown', onGraphKeydown)
  document.removeEventListener('pointerup', releaseHandle, true)
  document.removeEventListener('pointercancel', releaseHandle, true)
})

// --- list-stats mode --------------------------------------------------------
const listMeta = computed(() => lists.value.find((l) => l.name === filters.list) || null)
const listCard = computed(() => {
  const s = summary.value
  if (!s) return null
  return {
    name: filters.list,
    synced: fmtSynced(listMeta.value?.last_synced_at),
    total: fmtInt(s.total),
    scored: fmtInt(s.scored),
    mix: s.label_distribution || {},
    tooShort: s.too_short || 0,
  }
})

function closeList() {
  filters.setFilter('list', '')
}
</script>

<template>
  <div class="card">
    <div class="pane-header">
      <span class="pane-title">Lists</span>
      <span class="pane-subtitle">{{ contextSub }}</span>
      <label
        v-if="mode !== 'list'"
        class="show-all"
        :title="`Show all (${fmtInt(totalListCount)})`"
      >
        <input
          type="checkbox"
          class="show-all-input"
          role="switch"
          :checked="!showActive"
          :aria-checked="!showActive"
          @change="(e) => (showActive = !e.target.checked)"
        />
        <span class="switch" aria-hidden="true"><span class="switch-knob"></span></span>
        <span class="show-all-text">Show All</span>
      </label>
      <input
        v-if="mode !== 'list'"
        v-model="listQuery"
        type="search"
        placeholder="search lists…"
        class="lists-search"
      />
      <span class="header-actions">
        <button class="io-btn" title="One timeline per list, stacked" @click="openTimelines">
          Timelines
        </button>
        <button v-if="!pullFormOpen" class="io-btn" @click="openPullForm">
          Add list
        </button>
        <button class="io-btn" :disabled="regenerating" @click="regenerate">
          Rebuild index
        </button>
      </span>
    </div>
    <div class="pane-body ctx-body">
      <div v-if="pullFormOpen" class="pull-form">
        <div class="pull-form-row">
          <input
            type="text"
            placeholder="list name"
            :value="pullName"
            class="pull-name mono"
            @input="(e) => (pullName = e.target.value)"
          />
          <input
            type="number"
            min="1"
            max="1000"
            :value="pullCount"
            class="pull-count"
            @input="(e) => (pullCount = e.target.value)"
          />
          <button class="btn-primary btn-go" :disabled="processRunning" @click="submitPull">
            Run process ($)
          </button>
          <button class="btn-cancel" @click="cancelPull">✕</button>
        </div>
        <div class="pull-note">Scoring sends extracted text to the paid Pangram API.</div>
      </div>
      <div v-if="statusMsg && mode !== 'list'" class="status-mono status-mono-dark status-line">
        {{ statusMsg }}
      </div>

      <!-- list stats -->
      <template v-if="mode === 'list'">
        <div class="card-head">
          <div class="card-name mono">{{ filters.list }}</div>
          <button class="close-x" title="Clear list filter" @click="closeList">×</button>
        </div>
        <div v-if="summaryLoading && !listCard" class="ctx-status">loading…</div>
        <div v-else-if="summaryError" class="ctx-status ctx-error">{{ summaryError }}</div>
        <template v-else-if="listCard">
          <div class="synced-line">
            last synced {{ listCard.synced
            }}{{ listCard.synced === 'never' ? '' : ' UTC' }}
          </div>
          <div class="stats-row">
            <div class="tile">
              <div class="tile-val tile-val-sm">{{ listCard.total }}</div>
              <div class="tile-cap tile-cap-sm">Msgs</div>
            </div>
            <div class="tile">
              <div class="tile-val tile-val-sm">{{ listCard.scored }}</div>
              <div class="tile-cap tile-cap-sm">Scored</div>
            </div>
            <MixSummary
              :counts="listCard.mix"
              :too-short="listCard.tooShort"
              :clickable="true"
              class="stats-mix"
              @select="(l) => filters.setFilter('label', l)"
            />
          </div>
          <div class="section-head">
            All {{ fmtInt(rugPoints.length) }} messages
            <span class="rug-note"
              >oldest → newest · binned by time where emails outnumber pixels{{
                rugUndated ? ` · ${fmtInt(rugUndated)} undated not shown` : ''
              }}</span
            >
          </div>
          <TimelineRug
            class="rug"
            :points="rugPoints"
            :height="30"
            @open="openRugMessage"
            @range="applyRugRange"
          />
          <div class="threads-row">
            <button type="button" class="io-btn" @click="openGraph">Show thread chart</button>
          </div>
          <div class="pull-footer">
            <button
              type="button"
              class="io-btn row-add-btn"
              @click.stop="toggleAddPopover(filters.list, $event)"
            >
              Add
            </button>
            <span class="status-mono">{{ statusMsg }}</span>
          </div>
        </template>
      </template>

      <!-- lists index -->
      <template v-else>
        <div class="index-caption">
          <span class="sortable" title="Sort by list name" @click="sortIndex('name', 'asc')"
            >List{{ nameInd }}</span
          >
          <span
            class="sortable"
            style="text-align: right;"
            title="Sort by message count"
            @click="sortIndex('count', 'desc')"
            >Msgs{{ countInd }}</span
          >
          <span class="sortable" title="Sort by AI + Mixed count" @click="sortIndex('ai', 'desc')"
            >Aggregate analysis{{ aiInd }}</span
          >
          <span style="text-align: right;">Earliest</span>
          <span style="text-align: right;" title="Last synced (UTC)">Synced</span>
          <span></span>
        </div>
        <div v-if="showActiveEmpty" class="index-empty">
          No active lists — add a list, or switch to Show all.
        </div>
        <div v-else-if="showSearchEmpty" class="index-empty">
          No lists match “{{ listQuery.trim() }}”.
        </div>
        <div
          v-for="l in listRows"
          :key="l.name"
          class="index-row hover-row"
          @click="filters.setFilter('list', l.name)"
        >
          <span class="index-name mono" :title="l.name">{{ l.name }}</span>
          <span class="index-count mono">{{ l.count }}</span>
          <span class="agg-cell">
            <TimelineRug
              v-if="l.history && l.history.length"
              :points="l.history"
              :start="indexRugStart"
              :end="indexRugEnd"
              :height="12"
              @open="openRugMessage"
              @range="(rg) => applyIndexRugRange(l.name, rg)"
            />
            <MixBar :counts="l.counts" :too-short="l.tooShort" :height="9" />
          </span>
          <span class="index-earliest mono">{{ l.earliest }}</span>
          <span class="index-synced mono">{{ l.synced }}</span>
          <button
            type="button"
            class="io-btn row-add-btn"
            @click.stop="toggleAddPopover(l.name, $event)"
          >
            Add
          </button>
        </div>
      </template>
    </div>

    <!-- add-and-check popover, anchored to whichever Add button opened it (an
         index row's or the list-stats footer's); teleported to <body> so no
         ancestor overflow (scrolling pane body, clipped card) can hide it -->
    <Teleport to="body">
      <div v-if="popoverList" class="add-popover" :style="popoverStyle" @click.stop>
        <div class="pop-head">
          <div class="pop-tabs" role="tablist">
            <button
              type="button"
              class="pop-tab"
              :class="{ 'pop-tab-on': popoverTab === 'new' }"
              :aria-selected="popoverTab === 'new'"
              @click="setPopoverTab('new')"
            >
              New since last fetch
            </button>
            <button
              type="button"
              class="pop-tab"
              :class="{ 'pop-tab-on': popoverTab === 'before' }"
              :aria-selected="popoverTab === 'before'"
              @click="setPopoverTab('before')"
            >
              Before last fetch
            </button>
          </div>
          <button type="button" class="pop-close" title="Close" @click="closeAddPopover">✕</button>
        </div>

        <!-- Tab 1 — new since last fetch -->
        <div v-if="popoverTab === 'new'" class="pop-view">
          <div v-if="newPreviewLoading" class="pop-status">checking server…</div>
          <div v-else-if="newPreviewError" class="pop-status pop-error">{{ newPreviewError }}</div>
          <template v-else-if="newPreview">
            <div v-if="newPreview.total === 0" class="pop-status">
              No new messages since the last fetch.
            </div>
            <template v-else>
              <div class="pop-list">
                <div v-for="(m, i) in newPreview.messages" :key="i" class="pop-msg">
                  <span class="pop-from" :title="senderTitle(m)">{{ senderName(m) }}</span>
                  <span class="pop-subj">{{ m.subject || '(no subject)' }}</span>
                  <span class="pop-date mono">{{ fmtDate(m.date) }}</span>
                </div>
              </div>
              <div v-if="newPreview.more > 0" class="pop-more">
                + {{ fmtInt(newPreview.more) }} more not shown
              </div>
            </template>
          </template>
          <div class="pop-fetch-row">
            <label class="pop-label">
              Messages to fetch:
              <input
                type="text"
                class="pop-input"
                :value="newCountInput"
                @input="(e) => (newCountInput = e.target.value)"
                @change="normaliseNewCount"
                @blur="normaliseNewCount"
              />
            </label>
            <button type="button" class="io-btn" @click="startPopoverProcess('new')">
              Run process ($)
            </button>
          </div>
        </div>

        <!-- Tab 2 — before last fetch -->
        <div v-else class="pop-view">
          <div class="pop-fetch-row">
            <label class="pop-label">
              Messages to preview:
              <input
                type="number"
                min="1"
                max="1000"
                class="pop-input pop-input-num"
                :value="beforeCount"
                @input="(e) => (beforeCount = e.target.value)"
              />
            </label>
            <button
              type="button"
              class="io-btn"
              :disabled="beforePreviewLoading"
              @click="runBeforePreview"
            >
              Preview
            </button>
          </div>
          <div v-if="beforePreviewLoading" class="pop-status">checking server…</div>
          <div v-else-if="beforePreviewError" class="pop-status pop-error">
            {{ beforePreviewError }}
          </div>
          <template v-else-if="beforePreview">
            <div v-if="beforePreview.total === 0" class="pop-status">No messages found.</div>
            <template v-else>
              <div class="pop-list">
                <div v-for="(m, i) in beforePreview.messages" :key="i" class="pop-msg">
                  <span class="pop-from" :title="senderTitle(m)">{{ senderName(m) }}</span>
                  <span class="pop-subj">{{ m.subject || '(no subject)' }}</span>
                  <span class="pop-date mono">{{ fmtDate(m.date) }}</span>
                </div>
              </div>
              <div v-if="beforePreview.more > 0" class="pop-more">
                + {{ fmtInt(beforePreview.more) }} more not shown
              </div>
            </template>
          </template>
          <div class="pop-fetch-row">
            <button
              type="button"
              class="io-btn"
              :disabled="!beforePreviewed"
              @click="startPopoverProcess('before')"
            >
              Run process ($)
            </button>
          </div>
        </div>

        <div class="pop-footer">
          <div class="pop-note">Scoring sends extracted text to the paid Pangram API.</div>
        </div>
      </div>
    </Teleport>

    <!-- thread-graph lightbox (80% wide), teleported to <body> so no ancestor
         overflow can clip it; backdrop click or Escape closes it -->
    <Teleport to="body">
      <div v-if="graphOpen" class="tg-overlay" @click.self="graphOpen = false">
        <div class="tg-lightbox" role="dialog" aria-modal="true">
          <div class="tg-lb-head">
            <span class="tg-lb-title mono">{{ filters.list }} — threads</span>
            <span class="tg-lb-note">
              receipt order → · months along the top · one circle per email · one row per thread
            </span>
            <span v-if="graphLoading && threadGraph" class="status-mono tg-lb-busy">loading…</span>
            <button type="button" class="pop-close" title="Close" @click="graphOpen = false">
              ✕
            </button>
          </div>
          <div class="tg-lb-body">
            <div v-if="graphLoading && !threadGraph" class="ctx-status">loading…</div>
            <div v-else-if="graphError" class="ctx-status ctx-error">{{ graphError }}</div>
            <div v-else-if="!threadGraph || !threadGraph.total" class="ctx-status">no messages</div>
            <ThreadGraph
              v-else
              :threads="threadGraph.threads"
              :total="threadGraph.total"
              @select="openGraphMessage"
            />
          </div>
          <!-- window slider: two overlaid range inputs (transparent tracks, only
               the thumbs take pointer events) selecting the shown messages by
               receipt rank, not by date; a held handle names the date its rank
               falls on above the rail -->
          <div v-if="hasWindow" class="tg-win">
            <div class="tg-win-track">
              <div class="tg-win-rail"></div>
              <div class="tg-win-fill" :style="winFillStyle"></div>
              <input
                type="range"
                class="tg-win-range"
                :class="{ 'tg-win-range-top': winStart >= winEnd }"
                min="0"
                :max="winMax"
                :value="winStart"
                aria-label="Furthest-back message shown"
                @pointerdown="holdHandle('start')"
                @blur="releaseHandle"
                @input="onHandleInput('start', $event)"
                @change="commitWindow"
              />
              <input
                type="range"
                class="tg-win-range"
                min="0"
                :max="winMax"
                :value="winEnd"
                aria-label="Most recent message shown"
                @pointerdown="holdHandle('end')"
                @blur="releaseHandle"
                @input="onHandleInput('end', $event)"
                @change="commitWindow"
              />
              <!-- the held handle's date, above the rail at the handle -->
              <div v-if="handleTipDate" class="tg-win-tip mono" :style="handleTipStyle">
                {{ handleTipDate }}
              </div>
            </div>
            <div class="tg-win-legend">
              <span class="tg-win-date mono">{{ fmtDate(threadGraph.first_date) }}</span>
              <span class="tg-win-count mono">{{ winCaption }}</span>
              <span class="tg-win-date tg-win-date-r mono">
                {{ fmtDate(threadGraph.last_date) }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- staged run modal, teleported to <body> (inside the component) -->
    <RunProcessModal
      :open="processModalOpen"
      :title="processModalTitle"
      :stages="processStages"
      :running="processRunning"
      @close="closeProcessModal"
    />
  </div>
</template>

<style scoped>
.ctx-body {
  padding: 10px 12px;
}
.mono {
  font-family: var(--mono);
}
.ctx-status {
  font-size: 11.5px;
  color: var(--text-muted);
  padding: 4px 0;
}
.ctx-error {
  color: var(--danger);
}

/* --- card head (list stats) --- */
.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.card-name {
  font-size: 14px;
  font-weight: 700;
}
.close-x {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
}
.synced-line {
  font-size: 10.5px;
  color: var(--text-muted);
}

/* --- stat tiles + mix summary --- */
.stats-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 8px;
}
.stats-mix {
  flex: 1;
  padding-top: 1px;
}
.tile {
  background: var(--tile);
  border-radius: 4px;
  padding: 5px 8px;
  flex: none;
  min-width: 52px;
}
.tile-val {
  font-size: 14px;
  font-weight: 700;
  font-family: var(--mono);
}
.tile-val-sm {
  font-size: 13.5px;
}
.tile-cap {
  font-size: 9.5px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 700;
}
.tile-cap-sm {
  font-size: 9px;
}

/* --- section headings --- */
.section-head {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin: 12px 0 4px;
}
.rug-note {
  font-weight: 400;
  text-transform: none;
  letter-spacing: 0;
  margin-left: 6px;
}

/* --- rug plot (an adaptive TimelineRug strip) --- */
.rug {
  margin-top: 2px;
}
.hover-row:hover {
  background: var(--hover-row);
}

/* --- thread graph (opener only; the chart itself lives in the lightbox) --- */
.threads-row {
  margin-top: 12px;
}

/* --- thread-graph lightbox --- */
.tg-overlay {
  position: fixed;
  inset: 0;
  z-index: 300;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
}
.tg-lightbox {
  width: 80vw;
  max-height: 90vh;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
}
.tg-lb-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.tg-lb-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-name);
}
.tg-lb-note {
  font-size: 10.5px;
  color: var(--text-muted);
}
.tg-lb-busy {
  margin-left: auto;
}
.tg-lb-body {
  height: 64vh;
  min-height: 0;
}

/* --- thread-graph window slider ---
   Two range inputs stacked on one rail. Both are transparent and inert; only
   their thumbs take pointer events, so each handle is grabbable across the
   whole track. With the handles on the same rank the upper input wins the
   pointer, so the left one is raised in that case: the drag then widens the
   window backwards instead of deadlocking against the clamp. */
.tg-win {
  margin-top: 10px;
}
.tg-win-track {
  position: relative;
  height: 18px;
}
.tg-win-rail,
.tg-win-fill {
  position: absolute;
  top: 8px;
  height: 3px;
  border-radius: 2px;
  pointer-events: none;
}
.tg-win-rail {
  left: 0;
  right: 0;
  background: var(--border);
}
.tg-win-fill {
  background: var(--accent);
}
.tg-win-range {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 18px;
  margin: 0;
  background: none;
  pointer-events: none;
  -webkit-appearance: none;
  appearance: none;
}
.tg-win-range-top {
  z-index: 2;
}
.tg-win-range::-webkit-slider-runnable-track {
  height: 18px;
  background: none;
}
.tg-win-range::-moz-range-track {
  height: 18px;
  background: none;
}
.tg-win-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  pointer-events: auto;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--surface);
  border: 2px solid var(--accent);
  cursor: pointer;
}
.tg-win-range::-moz-range-thumb {
  pointer-events: auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--surface);
  border: 2px solid var(--accent);
  cursor: pointer;
}
.tg-win-range:focus-visible::-webkit-slider-thumb {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.tg-win-range:focus-visible::-moz-range-thumb {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
/* The held handle's date, sitting on the rail above the handle. Inert to the
   pointer so it can never interrupt the drag underneath it, and placed by the
   same percentage geometry as the fill (see handleTipStyle). */
.tg-win-tip {
  position: absolute;
  bottom: 100%;
  margin-bottom: 1px;
  z-index: 3;
  padding: 1px 5px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.14);
  font-size: 10px;
  color: var(--text-secondary);
  white-space: nowrap;
  pointer-events: none;
}
.tg-win-legend {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 2px;
}
.tg-win-date {
  font-size: 10px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.tg-win-date-r {
  text-align: right;
}
.tg-win-count {
  flex: 1;
  text-align: center;
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
}

/* --- pull footer (list stats) --- */
.pull-footer {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-mono {
  font-size: 10.5px;
  color: var(--text-muted);
  font-family: var(--mono);
}
.status-mono-dark {
  color: var(--text-secondary);
}

/* --- buttons --- */
.btn-primary {
  font-size: 11px;
  font-weight: 700;
  padding: 3px 9px;
  border: none;
  border-radius: 3px;
  background: var(--accent);
  color: #ffffff;
  cursor: pointer;
}
.btn-secondary {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  cursor: pointer;
}

/* --- index mode --- */
/* Columns: name · msgs · mix · earliest · synced · Add. The name and Earliest
   columns share the space left by the fixed ones in a 2:1 ratio, so the name
   gets two thirds of the width it had before Earliest existed. `minmax(64px, 2fr)`
   lets the name shrink below its own min-content width (it ellipsises instead of
   forcing the row wider) but never collapse to nothing in a narrow pane, while
   Earliest keeps a floor wide enough for a full 'YYYY-MM-DD HH:mm' stamp in the
   mono face. */
.index-caption {
  display: grid;
  grid-template-columns: minmax(64px, 2fr) 44px 150px minmax(104px, 1fr) 88px 34px;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  padding: 2px 0;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
}
.index-caption .sortable {
  cursor: pointer;
}
.index-caption .sortable:hover {
  color: var(--accent);
}
.index-row {
  display: grid;
  grid-template-columns: minmax(64px, 2fr) 44px 150px minmax(104px, 1fr) 88px 34px;
  gap: 6px;
  align-items: center;
  border-bottom: 1px solid var(--border-row);
  cursor: pointer;
  padding: 3px 0;
  font-size: 11.5px;
  position: relative; /* anchors the per-row add-and-check popover */
}
.index-name {
  font-weight: 500;
  color: var(--text-name);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.index-row:hover .index-name {
  color: var(--accent);
}
.index-count {
  text-align: right;
  color: var(--text-secondary);
}
.index-earliest {
  text-align: right;
  color: var(--text-muted);
  font-size: 10.5px;
  white-space: nowrap;
}
.index-synced {
  text-align: right;
  color: var(--text-muted);
  font-size: 10.5px;
  white-space: nowrap;
}
.index-empty {
  padding: 10px 2px;
  font-size: 11.5px;
  color: var(--text-muted);
}

/* The list's history rug above its mix bar (the Aggregate analysis cell). The
   rug appears once the shared timelines fetch has landed. */
.agg-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

/* Index search box, matching the Senders pane's. */
.lists-search {
  font-size: 11px;
  height: 21px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 150px;
  box-sizing: border-box;
  margin-left: auto;
}

/* --- "Show All" toggle switch --- */
.show-all {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  cursor: pointer;
  user-select: none;
}
.show-all-text {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  white-space: nowrap;
}
/* Native checkbox drives state/focus but is visually replaced by the switch. */
.show-all-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  margin: 0;
}
.switch {
  position: relative;
  display: inline-block;
  width: 26px;
  height: 14px;
  border-radius: 7px;
  background: var(--border);
  transition: background 0.12s ease;
  flex: none;
}
.switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 1px 1px rgba(0, 0, 0, 0.25);
  transition: transform 0.12s ease;
}
.show-all-input:checked + .switch {
  background: var(--accent);
}
.show-all-input:checked + .switch .switch-knob {
  transform: translateX(12px);
}
.show-all-input:focus-visible + .switch {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

/* --- lightweight text buttons (matches MessagesPane .io-btn) --- */
.io-btn {
  font-size: 11px;
  font-weight: 600;
  border: none;
  background: none;
  color: #2f6feb;
  cursor: pointer;
  padding: 0;
}
.io-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.row-add-btn {
  justify-self: end;
}

/* --- add-and-check popover --- */
.add-popover {
  /* position / top / bottom / left / width are set inline (see computePopoverStyle) */
  position: fixed;
  z-index: 200;
  width: 400px;
  max-width: 92vw;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
  padding: 10px;
  cursor: default;
  font-size: 11px;
  color: var(--text-secondary);
}
.pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.pop-tabs {
  display: inline-flex;
  gap: 6px;
}
.pop-tab {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 1px 3px;
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
}
.pop-tab-on {
  color: var(--text-name);
  border-bottom-color: var(--accent);
}
.pop-close {
  border: none;
  background: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 0 2px;
}
.pop-list {
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--border-row);
  border-radius: 4px;
  margin-bottom: 6px;
}
.pop-msg {
  display: grid;
  grid-template-columns: 1fr 1.4fr auto;
  gap: 6px;
  align-items: baseline;
  padding: 3px 6px;
  border-bottom: 1px solid var(--border-row);
}
.pop-msg:last-child {
  border-bottom: none;
}
.pop-from {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-name);
}
.pop-subj {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
.pop-date {
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
}
.pop-more {
  font-size: 10px;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.pop-status {
  font-size: 10.5px;
  color: var(--text-muted);
  padding: 3px 0;
}
.pop-error {
  color: var(--danger);
}
.pop-fetch-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0;
}
.pop-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-secondary);
}
.pop-input {
  font-size: 11px;
  height: 20px;
  padding: 0 5px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 60px;
}
.pop-input-num {
  width: 60px;
}
.pop-footer {
  margin-top: 6px;
}
.pop-note {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* --- header actions + pull form --- */
.header-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.status-line {
  margin-bottom: 6px;
}
.pull-form {
  margin-bottom: 8px;
  background: var(--tile);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 7px 9px;
}
.pull-form-row {
  display: flex;
  gap: 5px;
  align-items: center;
}
.pull-name {
  font-size: 11px;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  flex: 1;
  min-width: 0;
}
.pull-count {
  font-size: 11px;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  width: 54px;
}
.btn-go {
  height: 24px;
  padding: 0 9px;
}
.btn-cancel {
  font-size: 11px;
  height: 24px;
  padding: 0 7px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--surface);
  cursor: pointer;
}
.pull-note {
  font-size: 10px;
  color: var(--text-muted);
  margin-top: 4px;
}
</style>
