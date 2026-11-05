/**
 * hermes-backup — desktop half of the hermes-backup plugin package.
 *
 * Pane: per-leg backup health (Local / Offsite) with snapshot age, an RPO
 * overdue indicator when a leg's newest snapshot is older than 2x the
 * configured interval, the last cycle verdict, and the last DR drill
 * verdict + date. Statusbar chip: health dot + age of the freshest leg.
 *
 * Data comes from the Python backend (dashboard/plugin_api.py) mounted at
 * /api/plugins/hermes-backup/ via ctx.rest('/status'). All timestamps are
 * numeric epoch seconds — the math happens here in the renderer.
 *
 * Catalog rule 8 — SDK surface only: the only importable specifiers are
 * @hermes/plugin-sdk, react and react/jsx-runtime (the file loads
 * uncompiled, so UI is written with jsx()/jsxs() calls, not JSX syntax).
 * No app-internal DOM queries, no prototype patching, no eval, no script
 * tags, no raw window.hermesDesktop bridge. Colors come from the host theme
 * (StatusDot tones + var(--ui-*) with inherit fallbacks) — never hardcoded.
 */

import { Badge, Button, StatusDot, Tip, haptic, host, useQuery } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-backup'
const REST_TIMEOUT_MS = 30000

// ---------------------------------------------------------------- helpers --

function ageStr(epoch, nowMs) {
  if (typeof epoch !== 'number' || !(epoch > 1e9)) return null
  const s = Math.max(0, Math.floor(nowMs / 1000 - epoch))
  if (s < 90) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

function fmtDate(epoch) {
  if (typeof epoch !== 'number' || !(epoch > 1e9)) return null
  try {
    return new Date(epoch * 1000).toLocaleDateString()
  } catch {
    return null
  }
}

/**
 * Health for one leg. ok/warn/overdue are RENDERER math from the numeric
 * epoch against the configured interval (ok <= interval, warn <= 2x,
 * overdue past 2x); skip/fail are server facts (unconfigured / unreachable).
 */
function legHealth(leg, intervalMin, nowMs) {
  if (!leg) return { tone: 'bad', state: 'unknown' }
  if (leg.configured === false) return { tone: 'muted', state: 'skip' }
  if (!leg.reachable) return { tone: 'bad', state: 'fail' }
  const epoch = leg.snapshot && leg.snapshot.epoch
  if (typeof epoch !== 'number' || !(epoch > 1e9)) return { tone: 'bad', state: 'fail' }
  const ageS = Math.max(0, nowMs / 1000 - epoch)
  if (ageS < intervalMin * 60) return { tone: 'good', state: 'ok' }
  if (ageS < intervalMin * 120) return { tone: 'warn', state: 'warn' }
  return { tone: 'bad', state: 'overdue' }
}

const rowStyle = {
  padding: '7px 12px',
  borderRadius: 8,
  background: 'var(--ui-bg-secondary, transparent)',
}
const labelStyle = {
  fontWeight: 600,
  minWidth: 72,
  color: 'var(--ui-text-secondary, inherit)',
}
const valueStyle = {
  color: 'var(--ui-text-tertiary, inherit)',
}
const monoStyle = {
  marginLeft: 'auto',
  color: 'var(--ui-text-tertiary, inherit)',
  fontFamily: 'monospace',
  fontSize: '0.85em',
}

// -------------------------------------------------------------------- pane --

function LegRow({ leg, intervalMin, nowMs }) {
  const { tone, state } = legHealth(leg, intervalMin, nowMs)
  const epoch = leg && leg.snapshot && leg.snapshot.epoch
  const age = ageStr(epoch, nowMs)
  const detail = state === 'skip'
    ? 'not configured — run backup_setup'
    : state === 'fail'
      ? ((leg && leg.error) || 'unreachable')
      : null
  const primary = state === 'skip'
    ? 'not configured'
    : age
      ? `${age} ago`
      : 'no snapshot'
  return jsxs('div', {
    style: rowStyle,
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 10 },
        children: [
          jsx(StatusDot, { tone: tone }),
          jsx('span', { style: labelStyle }, leg && leg.leg === 'r2' ? 'Offsite' : 'Local'),
          jsx('span', { style: valueStyle }, primary),
          jsx('span', { style: monoStyle }, (leg && leg.snapshot && leg.snapshot.id) || ''),
          state === 'overdue' ? jsx(Badge, { variant: 'destructive' }, 'RPO overdue') : null,
        ],
      }),
      detail
        ? jsx('div', {
            style: {
              marginTop: 2, marginLeft: 19, fontSize: '0.85em',
              color: 'var(--ui-text-tertiary, inherit)',
            },
          }, detail)
        : null,
    ],
  })
}

function FactRow({ tone, label, children }) {
  return jsxs('div', {
    style: rowStyle,
    children: [
      jsx(StatusDot, { tone: tone }),
      jsx('span', { style: labelStyle }, label),
      jsx('span', { style: { ...monoStyle, marginLeft: 'auto' } }, children),
    ],
  })
}

/**
 * Buttons hand the request to the agent as a chat turn via
 * host.composer.submit — the documented SDK composer surface. The widget
 * itself never calls a tool: the agent is the operator, so locking checks,
 * approvals and log discipline stay on its path. (The declarative
 * data-hermes-send attribute only works inside ::preview iframe widgets —
 * the pane-side SDK door is the composer verb.)
 */
function sendBackupPrompt(prompt, okMessage) {
  haptic('tap')
  const sent = host.composer.submit(null, prompt)
  host.notify({
    kind: sent ? 'info' : 'warning',
    message: sent
      ? okMessage
      : 'No chat composer is open — open a chat, then try again',
  })
}

function makePane(ctx) {
  function BackupPane() {
    const q = useQuery({
      queryKey: [ID, 'status'],
      queryFn: () => ctx.rest('/status', { timeoutMs: REST_TIMEOUT_MS }),
      refetchInterval: 30000,
      retry: 1,
    })
    if (q.isError) {
      return jsx('div', {
        style: { padding: 16, color: 'var(--ui-danger, var(--ui-text-secondary, inherit))' },
      }, `backup status unavailable: ${q.error && q.error.message ? q.error.message : String(q.error)}`)
    }
    const st = q.data
    if (!st) {
      return jsx('div', { style: { padding: 16, color: 'var(--ui-text-tertiary, inherit)' } }, 'loading…')
    }
    const nowMs = Date.now()
    const intervalMin = typeof st.interval_minutes === 'number' && st.interval_minutes > 0
      ? st.interval_minutes
      : 5
    const legs = Array.isArray(st.legs) ? st.legs : []
    const anyOverdue = legs.some(
      (leg) => legHealth(leg, intervalMin, nowMs).state === 'overdue'
    )
    const schedule = st.schedule || {}
    const schedText = schedule.kind === 'systemd' && schedule.active
      ? 'timer active'
      : schedule.kind === 'cron' && schedule.active
        ? 'cron active'
        : 'no schedule'
    const nextIn = schedule.active && typeof schedule.next_run_epoch === 'number'
      ? ageStr(schedule.next_run_epoch, nowMs)
      : null
    const lastRun = st.last_run || null
    const runTone = !lastRun
      ? 'muted'
      : lastRun.status === 'OK' ? 'good' : lastRun.status === 'SKIP' ? 'warn' : 'bad'
    const drill = st.last_drill || null
    const drillTone = !drill
      ? 'muted'
      : drill.verdict === 'PASS' ? 'good' : 'bad'
    const drillDate = drill ? (drill.date || fmtDate(drill.epoch)) : null
    return jsxs('div', {
      style: {
        padding: '12px 16px', display: 'flex', flexDirection: 'column',
        gap: 8, height: '100%', overflow: 'auto',
      },
      children: [
        jsxs('div', {
          style: { display: 'flex', alignItems: 'baseline', gap: 10 },
          children: [
            jsx('span', { style: { fontWeight: 700, fontSize: '1.05em' } }, 'Hermes Backup'),
            jsx(Badge, { variant: 'outline' }, `RPO ${intervalMin}m`),
            anyOverdue ? jsx(Badge, { variant: 'destructive' }, 'RPO overdue') : null,
            jsx('span', {
              style: {
                marginLeft: 'auto', fontSize: '0.85em',
                color: 'var(--ui-text-tertiary, inherit)',
              },
            }, nextIn ? `${schedText} · next ~${nextIn}` : schedText),
          ],
        }),
        legs.map((leg, i) => jsx(LegRow, { key: i, leg: leg, intervalMin: intervalMin, nowMs: nowMs })),
        jsx(FactRow, {
          tone: runTone,
          label: 'Last cycle',
          children: lastRun
            ? `${lastRun.status} · ${ageStr(lastRun.epoch, nowMs) || '?'} ago`
            : 'no record yet',
        }),
        jsx(FactRow, {
          tone: drillTone,
          label: 'Last drill',
          children: drill && drillDate
            ? `${drill.verdict} · ${drillDate}`
            : 'no drill recorded — run backup_drill',
        }),
        jsxs('div', {
          style: { display: 'flex', gap: 8, marginTop: 'auto', paddingTop: 4 },
          children: [
            jsx(Button, {
              variant: 'outline',
              size: 'sm',
              onClick: () => sendBackupPrompt(
                'Run one snapshot cycle now with the hermes-backup plugin: invoke the backup_now tool and report the per-leg verdicts.',
                'Backup request sent to the agent'
              ),
            }, 'Backup now'),
            jsx(Button, {
              variant: 'outline',
              size: 'sm',
              onClick: () => sendBackupPrompt(
                'Run the backup_drill tool from the hermes-backup plugin and report each step PASS/FAIL verdict.',
                'Drill request sent to the agent'
              ),
            }, 'Run drill'),
          ],
        }),
      ],
    })
  }
  return BackupPane
}

// ------------------------------------------------------------------- chip --

function makeChip(ctx) {
  function BackupChip() {
    const q = useQuery({
      queryKey: [ID, 'status'],
      queryFn: () => ctx.rest('/status', { timeoutMs: REST_TIMEOUT_MS }),
      refetchInterval: 60000,
      retry: false,
    })
    const st = q.data
    const nowMs = Date.now()
    const intervalMin = st && typeof st.interval_minutes === 'number' && st.interval_minutes > 0
      ? st.interval_minutes
      : 5
    const legs = st && Array.isArray(st.legs) ? st.legs : []
    const epochs = legs
      .map((leg) => leg && leg.snapshot && leg.snapshot.epoch)
      .filter((e) => typeof e === 'number' && e > 1e9)
    const failing = legs.some((leg) => leg && leg.configured !== false && !leg.reachable)
    let tone = 'muted'
    let label = 'backup —'
    if (failing) {
      tone = 'bad'
    }
    if (epochs.length) {
      const age = ageStr(Math.max(...epochs), nowMs)
      if (age) {
        label = `backup ${age}`
        if (!failing) {
          const ageS = Math.max(0, nowMs / 1000 - Math.max(...epochs))
          tone = ageS < intervalMin * 60 ? 'good' : ageS < intervalMin * 120 ? 'warn' : 'bad'
        }
      }
    }
    return jsx(Tip, {
      content: 'Hermes Backup — snapshot age across the backup legs',
      children: jsx('button', {
        type: 'button',
        onClick: () => {
          haptic('tap')
        },
        style: {
          display: 'inline-flex', alignItems: 'center', gap: 6,
          background: 'transparent', border: 'none', cursor: 'pointer',
          color: 'var(--ui-text-secondary, inherit)', font: 'inherit',
        },
        children: [
          jsx(StatusDot, { tone: tone }),
          jsx('span', {}, label),
        ],
      }),
    })
  }
  return BackupChip
}

// -------------------------------------------------------------- register --

export default {
  id: ID,
  name: 'Hermes Backup',
  defaultEnabled: true,
  register(ctx) {
    try {
      const Pane = makePane(ctx)
      const Chip = makeChip(ctx)
      ctx.register({
        id: 'pane',
        area: 'panes',
        title: 'hermes backup',
        data: {
          placement: 'bottom',
          dock: { pane: 'workspace', pos: 'bottom' },
          height: '200px',
        },
        render: () => jsx(Pane, {}),
      })
      ctx.register({
        id: 'chip',
        area: 'statusBar.right',
        order: 141,
        render: () => jsx(Chip, {}),
      })
      host.notify({ kind: 'info', message: 'Hermes Backup plugin loaded' })
    } catch (err) {
      try {
        host.notify({
          kind: 'error',
          message: `Hermes Backup plugin failed to load: ${err && err.message ? err.message : err}`,
        })
      } catch {
        /* notify is best-effort */
      }
      throw err
    }
  },
}
