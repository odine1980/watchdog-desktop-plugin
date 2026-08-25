/**
 * Watchdog — statusbar chip + watchdog pane for the Hermes desktop app.
 *
 * Backend: a standalone FastAPI service on the Hermes host
 *   http://127.0.0.1:8766   (default; override WATCHDOG_API_BASE / edit below)
 * The service shells out to the SAME check scripts the daily cron watchdog
 * uses (~/.hermes/scripts/lcm_daily_check.py + lcm_health_check.py), so this
 * pane and the cron always agree — one source of truth.
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
// Point this at your watchdog API.
//   - Hermes on this machine:        http://127.0.0.1:8766  (default)
//   - Remote Hermes host (Tailscale): http://<tailscale-ip>:8766
//   - Remote Hermes host (LAN):       http://<lan-ip>:8766
const API_BASE = 'http://127.0.0.1:8766'
const STATUS_KEY = ['watchdog-status']
const SOURCES_KEY = ['watchdog-sources']

const TONE = { ok: 'good', degraded: 'warn', critical: 'bad' }
const LABEL = { ok: 'all quiet', degraded: 'degraded', critical: 'problems' }

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
      const res = await fetch(`${API_BASE}/status`, { cache: 'no-store' })
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
      const res = await fetch(`${API_BASE}/run-check`, {
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
      const res = await fetch(`${API_BASE}/sources`, { cache: 'no-store' })
      if (!res.ok) throw new Error(`watchdog api ${res.status}`)
      return res.json()
    },
    refetchInterval: 300_000,
    staleTime: 120_000
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

function SectionCard({ title, count, children }) {
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
      children
    ]
  })
}

function WatchdogPage() {
  const { data, isError, isFetching, refetch } = useStatus()
  const runCheck = useRunCheck()
  const sourcesQ = useSources()

  if (isError) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-start justify-center gap-3 p-6 text-sm',
      children: [
        jsx('div', { className: 'font-medium', children: 'Watchdog backend unreachable' }),
        jsx('div', {
          className: 'font-mono text-[0.75rem] text-(--ui-text-tertiary)',
          children: `${API_BASE} — is the status service running? (uvicorn watchdog_api:app --host 0.0.0.0 --port 8766)`
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
              jsx('span', { className: 'text-(--ui-text-tertiary)', children: label })
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
          jsx(StatChip, { tone: 'muted', children: `backlog ${stats.lcm_backlog ?? '?'}` }),
          jsx(StatChip, { tone: stats.lcm_integrity === 'ok' ? 'good' : 'bad', children: `lcm ${stats.lcm_integrity ?? '?'}` })
        ]
      })
    ]
  })
}

// ---------------- plugin ----------------

const plugin = {
  id: ID,
  name: 'Watchdog',
  description: 'System + LCM watchdog — statusbar chip, live checks, watched sources, alert history.',
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
      }
    ])
  }
}

export default plugin
