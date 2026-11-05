#!/usr/bin/env node
/**
 * Server-render proof for desktop/plugin.js — the blank-render regression
 * class (P1 #1: text passed as the third jsx() argument rendered an empty
 * element while 9/9 syntax-level tests stayed green).
 *
 * Renders the pane + chip with REAL react-dom/server (`renderToString`,
 * React symlinked from the host app's node_modules — dependency-light,
 * runs offline) and:
 *   1. asserts the rendered HTML carries NON-EMPTY visible text;
 *   2. FAILS when React emits a "unique key" warning during render (the
 *      pre-existing array-children finding in BackupChip's dot/label spans
 *      and the row/header children — every array child now carries a
 *      stable key, and this test keeps it that way).
 *
 * The pytest wrapper (tests/test_render.py) prepares the scratch project:
 *   node_modules/react + react-dom  (symlinked)
 *   node_modules/@hermes/plugin-sdk (tiny stub: pass-through Badge/Button/
 *                                   Tip, useQuery serving ./status.json)
 *   ./plugin.js  (copy of desktop/plugin.js)
 *   ./status.json(the status under test)
 * and then runs this file with cwd = the scratch dir.
 *
 * Output (stdout): JSON { ok, paneHtml, chipHtml, paneText, chipText,
 * warnings, problems }. Exit 1 when any problem is found.
 */
import fs from 'node:fs'
import { renderToString } from 'react-dom/server'
import plugin from './plugin.js'

const registered = []
const ctx = { register: (cfg) => registered.push(cfg) }
const status = JSON.parse(
  fs.readFileSync(new URL('./status.json', import.meta.url), 'utf8')
)
globalThis.__BACKUP_STATUS_JSON__ = JSON.stringify(status)
plugin.register(ctx)

const paneCfg = registered.find((r) => r.id === 'pane')
const chipCfg = registered.find((r) => r.id === 'chip')
if (!paneCfg || !chipCfg) {
  process.stderr.write('test_render: pane/chip not registered\n')
  process.exit(1)
}

// Capture React's dev warnings (console.error) during the render — this is
// how "Each child in a list should have a unique key prop" is caught.
const warnings = []
const realError = console.error
console.error = (...args) => {
  warnings.push(args.map((a) => (typeof a === 'string' ? a : '')).join(' ').trim())
}

let paneHtml = ''
let chipHtml = ''
try {
  paneHtml = renderToString(paneCfg.render())
  chipHtml = renderToString(chipCfg.render())
} finally {
  console.error = realError
}

const stripTags = (h) => h.replace(/<[^>]*>/g, '')
const paneText = stripTags(paneHtml)
const chipText = stripTags(chipHtml)

const problems = []
if (!paneText.trim()) {
  problems.push('pane rendered with NO visible text (the blank-render class)')
}
for (const need of [
  'Hermes Backup', 'Local', 'Offsite', 'Backup now', 'Run drill',
  'Last cycle', 'Last drill', 'ago',
]) {
  if (!paneText.includes(need)) problems.push(`pane missing text: ${need}`)
}
if (!chipText.trim()) {
  problems.push('chip rendered with NO visible text')
}
if (!/backup (\d+[smhd]|—)/.test(chipText)) {
  problems.push(`chip missing "backup <age>" label; got: ${JSON.stringify(chipText)}`)
}

const isKeyWarning = (w) => /unique\s+"key"/i.test(w) || /unique key/i.test(w)
const keyWarnings = warnings.filter(isKeyWarning)
const otherWarnings = warnings.filter((w) => !isKeyWarning(w))
if (keyWarnings.length) {
  problems.push(`React key warnings (${keyWarnings.length}):\n  ${keyWarnings.join('\n  ')}`)
}

process.stdout.write(JSON.stringify({
  ok: problems.length === 0,
  paneHtml,
  chipHtml,
  paneText,
  chipText,
  keyWarningCount: keyWarnings.length,
  otherWarnings,
  problems,
}, null, 2))
if (problems.length) process.exit(1)
