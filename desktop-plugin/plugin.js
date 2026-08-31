/**
 * Watchdog — statusbar chip + watchdog pane for the Hermes desktop app.
 *
 * Backend: the watchdog's own small FastAPI status service on the Hermes
 * host. It is NOT the Hermes gateway (`hermes gateway`), NOT the Hermes web
 * dashboard (`hermes dashboard`, the 9119 remote backend the desktop app
 * dials), and NOT the OpenAI-compatible API server (8642).
 *   http://127.0.0.1:8766   (default; override WATCHDOG_BACKEND_URL / edit below)
 * The service shells out to the SAME check scripts the daily cron watchdog
 * uses (~/.hermes/scripts/lcm_daily_check.py + lcm_health_check.py), so this
 * pane and the cron always agree — one source of truth.
 *
 * Actions (LCM status/doctor/rotate/compact/backup) are NOT sent through the
 * FastAPI backend: they ride the gateway's own prompt.submit RPC so /lcm
 * commands execute in-process inside the engine that owns lcm.db — never a
 * second process writing the DB (WAL corruption vector). One click = the same
 * command you would type. The LCM actions card and its palette commands only
 * appear when the backend reports LCM present (lcm.db + health script).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import {
  cn,
  Codicon,
  haptic,
  host,
  PALETTE_AREA,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'watchdog'
// Point this at the watchdog backend — the plugin's own small FastAPI status
// service that powers this pane. It is NOT the Hermes gateway (`hermes
// gateway`), NOT the Hermes web dashboard (`hermes dashboard`, the 9119
// remote backend the desktop app dials), and NOT the OpenAI-compatible API
// server (8642). It's just the watchdog's status service.
//   - Hermes on this machine:        http://127.0.0.1:8766  (default)
//   - Remote Hermes host (Tailscale): http://<tailscale-ip>:8766
//   - Remote Hermes host (LAN):       http://<lan-ip>:8766
const WATCHDOG_BACKEND_URL = 'http://127.0.0.1:8766'
const STATUS_KEY = ['watchdog-status']
const SOURCES_KEY = ['watchdog-sources']
const SESSION_KEY = ['watchdog-session']

// Matches the app's own prompt.submit ack ceiling (agent.gateway_timeout =
// 1800s): a rotate can take minutes; the 30s default gateway timeout would
// surface a false failure while the turn is still running.
const PROMPT_SUBMIT_TIMEOUT_MS = 1_800_000

const TONE = { ok: 'good', degraded: 'warn', critical: 'bad' }
const LABEL = { ok: 'all quiet', degraded: 'degraded', critical: 'problems' }

/** True when the backend reports LCM is present (scripts + lcm.db exist). */
function lcmAvailable(data) {
  const lcm = (data?.checks || []).find(c => c.id === 'lcm')
  return lcm ? lcm.available !== false : false
}

/** Aggregate state → StatusDot tone + chip label. */
function chipState(data, isError) {
  if (isError) return { tone: 'bad', label: 'watchdog?' }
  if (!data) return { tone: 'muted', label: 'watchdog' }
  const critical = (data.checks || []).filter(c => c.state === 'critical').length
  if (data.state === 'critical') {
    return { tone: 'bad', label: critical > 1 ? `${critical} problems` : '1 problem' }
  }
  return { tone: TONE[data.state] || 'muted', label: LABEL[data.state] || data.state }
}

function relTime(iso) {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

// ---------------- data ----------------

function useStatus() {
  return useQuery({
    queryKey: STATUS_KEY,
    queryFn: async () => {
      const res = await fetch(`${WATCHDOG_BACKEND_URL}/status`, { cache: 'no-store' })
      if (!res.ok) throw new Error(`watchdog api ${res.status}`)
      return res.json()
    },
    refetchInterval: 30_000,
    staleTime: 15_000
  })
}

function useRunCheck() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const res = await fetch(`${WATCHDOG_BACKEND_URL}/run-check`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ check: 'all' })
      })
      if (!res.ok) throw new Error(`watchdog api ${res.status}`)
      return res.json()
    },
    onSuccess: data => qc.setQueryData(STATUS_KEY, data)
  })
}

function useSources() {
  return useQuery({
    queryKey: SOURCES_KEY,
    queryFn: async () => {
      const res = await fetch(`${WATCHDOG_BACKEND_URL}/sources`, { cache: 'no-store' })
      if (!res.ok) throw new Error(`watchdog api ${res.status}`)
      return res.json()
    },
    refetchInterval: 300_000,
    staleTime: 120_000
  })
}

/** Live session list straight from the gateway (read-only RPC) — used to show
 *  the active session's real message count without touching the backend. */
function useSessionInfo() {
  return useQuery({
    queryKey: SESSION_KEY,
    queryFn: async () => host.request('session.active_list', {
      current_session_id: host.state.activeSessionId.get() || ''
    }),
    refetchInterval: 60_000,
    staleTime: 30_000
  })
}

/** Shared LCM action: inject a /lcm command into the ACTIVE session via the
 *  gateway's prompt.submit RPC (same path as typing it — in-process, safe).
 *  display_kind 'hidden' keeps the injected user row out of the transcript;
 *  the /lcm command's output still streams back as a normal assistant turn. */
async function runLcmCommand(command) {
  const sid = host.state.activeSessionId.get()
  if (!sid) throw new Error('no active session')
  const gw = host.getGateway()
  if (!gw) throw new Error('gateway unavailable')
  const res = await gw.request('prompt.submit', {
    session_id: sid,
    text: command,
    display_kind: 'hidden'
  }, PROMPT_SUBMIT_TIMEOUT_MS)
  queryClient.invalidateQueries({ queryKey: SESSION_KEY })
  return res
}

function useLcmAction(command) {
  return useMutation({
    mutationFn: () => runLcmCommand(command),
    onSuccess: () => {
      haptic('tap')
      queryClient.invalidateQueries({ queryKey: SESSION_KEY })
    }
  })
}

// ---------------- statusbar chip ----------------

function WatchdogChip() {
  const { data, isError, isFetching } = useStatus()
  const { tone, label } = chipState(data, isError)

  return jsx(Tip, {
    label: 'Watchdog — click to open',
    children: jsx('button', {
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] tabular-nums transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      type: 'button',
      onClick: () => {
        haptic('tap')
        host.navigate('/watchdog')
      },
      children: jsxs('span', {
        className: 'inline-flex items-center gap-1',
        children: [
          jsx(StatusDot, { tone, className: isFetching ? 'animate-pulse' : undefined }),
          jsx('span', { children: label })
        ]
      })
    })
  })
}

// ---------------- pane page ----------------

function CheckRow({ check }) {
  const tone = TONE[check.state] || 'muted'
  const open = check.state !== 'ok'

  return jsxs('div', {
    className: 'border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-2',
        children: [
          jsx(StatusDot, { tone, className: 'mt-0.5 shrink-0' }),
          jsx('span', { className: 'font-medium', children: check.name }),
          jsx('span', { className: 'truncate text-(--ui-text-tertiary)', children: check.summary })
        ]
      }),
      open && jsx('div', {
        className: 'mt-1 flex flex-col gap-0.5 pl-[1.1rem] font-mono text-[0.6875rem]',
        children: (check.details || []).map(d =>
          jsxs('div', {
            className: 'flex items-baseline gap-1.5',
            children: [
              jsx(StatusDot, { tone: d.status === 'ok' ? 'good' : 'warn', className: 'mt-[0.3rem] shrink-0' }),
              jsx('span', {
                className: d.status === 'ok' ? 'text-(--ui-text-quaternary)' : 'text-(--ui-text-secondary)',
                children: d.detail ? `${d.label} — ${d.detail}` : d.label
              })
            ]
          }, `${check.id}-${d.label}`)
        )
      })
    ]
  })
}

function StatChip({ tone, children }) {
  return jsxs('span', {
    className: 'inline-flex items-center gap-1 rounded-full border border-(--ui-stroke-secondary) px-2 py-0.5 text-[0.6875rem]',
    children: [
      jsx(StatusDot, { tone }),
      jsx('span', { className: 'text-(--ui-text-secondary)', children })
    ]
  })
}

function SourceRow({ src }) {
  const tone = src.status === 'ok' ? 'good' : 'warn'
  const val = src.status !== 'ok'
    ? 'failed'
    : (src.new_count > 0 ? `${src.new_count} new` : 'up to date')
  return jsxs('div', {
    className: 'border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-2',
        children: [
          jsx(StatusDot, { tone, className: 'mt-0.5 shrink-0' }),
          jsx('span', { className: 'font-medium', children: src.name }),
          jsx('span', {
            className: 'rounded-full border border-(--ui-stroke-secondary) px-1.5 py-px font-mono text-[0.625rem] uppercase text-(--ui-text-tertiary)',
            children: src.kind
          }),
          jsx('span', { className: 'ml-auto shrink-0 text-(--ui-text-secondary)', children: val }),
          jsx('span', { className: 'shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)', children: relTime(src.checked_at) })
        ]
      }),
      src.status !== 'ok' && jsx('div', {
        className: 'mt-1 pl-[1.1rem] font-mono text-[0.6875rem] text-(--ui-text-tertiary)',
        children: src.detail
      })
    ]
  })
}

function AlertRow({ a }) {
  const resolved = !!a.resolved_at
  return jsxs('div', {
    className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) py-1.5 last:border-b-0',
    children: [
      jsx('span', {
        className: 'shrink-0 font-mono text-[0.6875rem] text-(--ui-text-quaternary)',
        children: relTime(a.opened_at)
      }),
      jsx('span', {
        className: cn(
          'shrink-0 rounded-full px-1.5 py-px text-[0.625rem] font-bold uppercase tracking-wide',
          resolved
            ? 'bg-(--ui-stroke-secondary) text-(--ui-text-tertiary)'
            : 'bg-amber-500/20 text-amber-500'
        ),
        children: resolved ? 'resolved' : 'active'
      }),
      jsx('span', {
        className: 'truncate text-(--ui-text-secondary)',
        children: `${a.name}: ${a.message}`
      })
    ]
  })
}

function SectionCard({ title, count, caption, children }) {
  return jsxs('div', {
    className: 'rounded-lg border border-(--ui-stroke-secondary) p-2',
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-baseline justify-between gap-2 px-1',
        children: [
          jsx('span', { className: 'text-[0.6875rem] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)', children: title }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: count })
        ]
      }),
      caption
        ? jsx('div', { className: 'mb-1 px-1 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)', children: caption })
        : null,
      children
    ]
  })
}

/** LCM action row: one-click compact / backup into the active session. */
function LcmActionRow({ label, command, hint, kind, disabled }) {
  const action = useLcmAction(command)
  const busy = action.isPending
  const off = disabled || busy

  return jsxs('div', {
    className: 'flex items-center gap-2 py-1',
    children: [
      jsx('button', {
        className: cn(
          'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs',
          kind === 'primary'
            ? 'border-(--ui-accent)/40 bg-(--ui-accent)/10 text-(--ui-accent) hover:bg-(--ui-accent)/20'
            : 'border-(--ui-stroke-secondary) hover:bg-(--chrome-action-hover)',
          'disabled:opacity-60'
        ),
        type: 'button',
        disabled: off,
        title: off ? (disabled ? 'Needs an active session' : hint) : hint,
        onClick: () => action.mutate(),
        children: jsxs('span', {
          className: 'inline-flex items-center gap-1',
          children: [
            jsx(Codicon, { name: busy ? 'sync' : (kind === 'primary' ? 'zap' : 'save'), size: '0.75rem', className: busy ? 'animate-spin' : undefined }),
            jsx('span', { children: busy ? 'Working…' : label })
          ]
        })
      }),
      jsx('span', {
        className: 'min-w-0 flex-1 truncate text-[0.6875rem] text-(--ui-text-tertiary)',
        children: action.isError
          ? `failed: ${action.error.message || 'unknown'}`
          : (action.isSuccess ? 'sent — output lands in chat' : hint)
      })
    ]
  })
}

function WatchdogPage() {
  const { data, isError, isFetching, refetch } = useStatus()
  const runCheck = useRunCheck()
  const sourcesQ = useSources()
  const sessionQ = useSessionInfo()

  if (isError) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-start justify-center gap-3 p-6 text-sm',
      children: [
        jsx('div', { className: 'font-medium', children: 'Watchdog backend unreachable' }),
        jsx('div', {
          className: 'font-mono text-[0.75rem] text-(--ui-text-tertiary)',
          children: `${WATCHDOG_BACKEND_URL} — is the status service running? (uvicorn watchdog_api:app --host 0.0.0.0 --port 8766)`
        }),
        jsx('button', {
          className: cn(
            'rounded-md border border-(--ui-stroke-secondary) px-2.5 py-1 text-xs',
            'hover:bg-(--chrome-action-hover)'
          ),
          type: 'button',
          onClick: () => refetch(),
          children: 'Retry'
        })
      ]
    })
  }

  if (!data) {
    return jsx('div', { className: 'p-6 text-sm text-(--ui-text-tertiary)', children: 'Loading…' })
  }

  const { tone, label } = chipState(data, false)
  const stats = data.stats || {}
  const activeSessions = (sessionQ.data?.sessions || []).filter(s => s.current)
  const active = activeSessions[0] || null

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm',
    children: [
      // Header: state + run button
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(StatusDot, { tone }),
              jsx('span', { className: 'font-medium', children: 'Watchdog' }),
              jsx('span', { className: 'text-(--ui-text-tertiary)', children: label }),
              jsx('span', { className: 'font-mono text-[0.6875rem] text-(--ui-text-quaternary)', children: `v${plugin.version}` })
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: `updated ${relTime(data.generated_at)}` }),
              jsx('button', {
                className: cn(
                  'inline-flex items-center gap-1 rounded-md border border-(--ui-stroke-secondary) px-2 py-0.5 text-xs',
                  'hover:bg-(--chrome-action-hover) disabled:opacity-60'
                ),
                type: 'button',
                disabled: runCheck.isPending || isFetching,
                onClick: () => runCheck.mutate(),
                children: jsxs('span', {
                  className: 'inline-flex items-center gap-1',
                  children: [
                    jsx(Codicon, { name: runCheck.isPending ? 'sync' : 'play', size: '0.75rem' }),
                    jsx('span', { children: runCheck.isPending ? 'Running…' : 'Run checks now' })
                  ]
                })
              })
            ]
          })
        ]
      }),
      // Checks
      jsx('div', {
        className: 'flex flex-col',
        children: (data.checks || []).map(c => jsx(CheckRow, { check: c }, c.id))
      }),
      // LCM actions — one-click compact/backup, agent-mediated via the gateway.
      // Only rendered when the backend reports LCM present (lcm.db + scripts).
      lcmAvailable(data) && jsx(SectionCard, {
        title: 'LCM actions',
        count: active
          ? `${active.message_count ?? '?'} msgs · ${String(active.id || '').slice(-8)}`
          : (sessionQ.isError ? 'gateway unreachable' : 'no active session'),
        caption: 'Compact + backup manage the context directly — they work with embeddings off (only semantic search is disabled). Buttons need an active session.',
        children: jsxs('div', {
          className: 'flex flex-col',
          children: [
            jsx(LcmActionRow, {
              label: 'Status',
              command: '/lcm status',
              hint: 'Session snapshot: message counts, DAG depth, provider, tail.',
              kind: 'secondary',
              disabled: !active
            }),
            jsx(LcmActionRow, {
              label: 'Diagnostics',
              command: '/lcm doctor',
              hint: 'Read-only doctor report: schema, integrity, FTS, DAG health.',
              kind: 'secondary',
              disabled: !active
            }),
            jsx(LcmActionRow, {
              label: 'Preview compact',
              command: '/lcm rotate',
              hint: 'Read-only preview — what compaction would do, no changes.',
              kind: 'secondary',
              disabled: !active
            }),
            jsx(LcmActionRow, {
              label: 'Compact now',
              command: '/lcm rotate apply',
              hint: 'Compacts this session in place (backup-first, tail-preserving). May take minutes — output lands in chat.',
              kind: 'primary',
              disabled: !active
            }),
            jsx(LcmActionRow, {
              label: 'Backup first',
              command: '/lcm backup',
              hint: 'Timestamped SQLite snapshot before any cleanup.',
              kind: 'secondary',
              disabled: !active
            })
          ]
        })
      }),
      // Watched sources
      jsx(SectionCard, {
        title: 'Watched sources',
        count: sourcesQ.isError
          ? 'unavailable'
          : (() => {
              const bad = (sourcesQ.data?.sources || []).filter(s => s.status !== 'ok').length
              const total = (sourcesQ.data?.sources || []).length
              return bad ? `${bad} of ${total} need attention` : `${total}/${total} up to date`
            })(),
        children: sourcesQ.isError
          ? jsx('div', { className: 'px-1 py-1 text-[0.75rem] text-(--ui-text-tertiary)', children: 'Source check unavailable (backend down?).' })
          : (!sourcesQ.data
              ? jsx('div', { className: 'px-1 py-1 text-[0.75rem] text-(--ui-text-tertiary)', children: 'Loading…' })
              : jsx('div', { className: 'flex flex-col', children: sourcesQ.data.sources.map(s => jsx(SourceRow, { src: s }, s.id)) }))
      }),
      // Alert history
      jsx(SectionCard, {
        title: 'Alert history',
        count: `${(data.alerts || []).length} shown`,
        children: (data.alerts || []).length === 0
          ? jsx('div', { className: 'px-1 py-1 text-[0.75rem] text-(--ui-text-tertiary)', children: 'No alerts since the watchdog started tracking.' })
          : jsx('div', { className: 'flex flex-col', children: (data.alerts || []).map(a => jsx(AlertRow, { a }, a.id)) })
      }),
      // Stat strip
      jsxs('div', {
        className: 'mt-auto flex flex-wrap gap-1.5 pt-2',
        children: [
          jsx(StatChip, { tone: (stats.disk_pct ?? 0) >= 80 ? 'warn' : 'good', children: `disk ${stats.disk_pct ?? '?'}%` }),
          jsx(StatChip, { tone: 'muted', children: stats.mem_summary ? stats.mem_summary.split('|')[0].trim().slice(0, 60) : 'mem ?' }),
          lcmAvailable(data) && jsx(StatChip, { tone: 'muted', children: `backlog ${stats.lcm_backlog ?? '?'}` }),
          lcmAvailable(data) && jsx(StatChip, { tone: stats.lcm_integrity === 'ok' ? 'good' : 'bad', children: `lcm ${stats.lcm_integrity ?? '?'}` })
        ]
      })
    ]
  })
}

// ---------------- plugin ----------------

const plugin = {
  id: ID,
  name: 'Watchdog',
  version: '1.0.2',
  description: 'System + LCM watchdog — statusbar chip, live checks, watched sources, alert history, one-click LCM actions (status, diagnostics, compact, backup).',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/watchdog' },
        render: () => jsx(WatchdogPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 50,
        data: { codicon: 'pulse', label: 'Watchdog', path: '/watchdog' }
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 80,
        render: () => jsx(WatchdogChip, {})
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'watchdog.open',
          label: 'Watchdog: Open pane',
          keywords: ['watchdog', 'status', 'health', 'lcm', 'checks'],
          run: () => host.navigate('/watchdog')
        }
      },
      {
        id: 'run-now',
        area: PALETTE_AREA,
        data: {
          id: 'watchdog.runNow',
          label: 'Watchdog: Run checks now',
          keywords: ['watchdog', 'run', 'check', 'refresh', 'status'],
          run: () => {
            haptic('tap')
            queryClient.invalidateQueries({ queryKey: STATUS_KEY })
            queryClient.invalidateQueries({ queryKey: SOURCES_KEY })
          }
        }
      },
      {
        id: 'compact-lcm',
        area: PALETTE_AREA,
        data: {
          id: 'watchdog.compactLcm',
          label: 'Watchdog: Compact LCM now',
          keywords: ['watchdog', 'lcm', 'compact', 'rotate', 'cleanup', 'context'],
          run: () => {
            haptic('tap')
            if (!lcmAvailable(queryClient.getQueryData(STATUS_KEY))) {
              console.warn('[watchdog] LCM not available — compact skipped')
              return
            }
            runLcmCommand('/lcm rotate apply').catch(err => {
              console.error('[watchdog] compact lcm failed', err)
            })
          }
        }
      },
      {
        id: 'backup-lcm',
        area: PALETTE_AREA,
        data: {
          id: 'watchdog.backupLcm',
          label: 'Watchdog: Backup LCM',
          keywords: ['watchdog', 'lcm', 'backup', 'snapshot'],
          run: () => {
            haptic('tap')
            if (!lcmAvailable(queryClient.getQueryData(STATUS_KEY))) {
              console.warn('[watchdog] LCM not available — backup skipped')
              return
            }
            runLcmCommand('/lcm backup').catch(err => {
              console.error('[watchdog] backup lcm failed', err)
            })
          }
        }
      }
    ])
  }
}

export default plugin
