import { useEffect, useState } from 'react'
import TerminalCard from './TerminalCard'

const POLL_MS = 3000

const BRANCH_LABELS = [
  { key: 'loc1', title: 'Branch — loc1' },
  { key: 'loc2', title: 'Branch — loc2' },
  { key: 'loc3', title: 'Branch — loc3' },
]

const FL_CLIENT_CHANNELS = [
  { key: 'fl-client-loc1', title: 'FL Client — loc1' },
  { key: 'fl-client-loc2', title: 'FL Client — loc2' },
  { key: 'fl-client-loc3', title: 'FL Client — loc3' },
]

// Live status strip -- fl_status is written by fl_server/server.py once per
// completed round and only ever holds the LATEST round (no history), so
// this is a snapshot, not a trend -- the trend lives in the real
// multi_cycle_trend.png below. Pending-label counts are the direct visual
// link back to Tab 2/3's Verdict Agent: every GUILTY verdict above the
// confidence bar queues real labels here before the next FL round drains
// them (see agents/label_generator.py).
function StatusStrip() {
  const [status, setStatus] = useState({ fl_status: null, pending_labels: {} })

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/tab4/status')
        const data = await res.json()
        if (!cancelled) setStatus(data)
      } catch {
        // backend unreachable — keep last known state
      }
    }
    poll()
    const intervalId = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(intervalId)
    }
  }, [])

  const fl = status.fl_status

  return (
    <div className="fl-status-strip">
      <div className="fl-status-round">
        {fl ? (
          <>
            <span className="fl-status-round-label">Latest FL round</span>
            <span className="fl-status-round-value">
              Round {fl.round_num} · AUC {fl.auc?.toFixed(4)}
            </span>
            <span className="structuring-row-meta">{new Date(fl.timestamp).toLocaleString()}</span>
          </>
        ) : (
          <span className="fl-status-round-label">No FL round has been run yet this session</span>
        )}
      </div>
      <div className="fl-status-labels">
        {BRANCH_LABELS.map((b) => (
          <div className="fl-status-chip" key={b.key}>
            <span className="fl-status-chip-count">{status.pending_labels?.[b.key] ?? 0}</span>
            <span className="fl-status-chip-label">{b.title} pending labels</span>
          </div>
        ))}
      </div>
    </div>
  )
}

const IMPACT_ROWS = [
  { key: 'flagged', label: 'Flagged total', decimals: 0 },
  { key: 'false_positives', label: 'False positives', decimals: 0 },
  { key: 'missed_fraud', label: 'Fraud missed', decimals: 0 },
  { key: 'avg_score_fraud', label: 'Avg. score on real fraud', decimals: 4 },
  { key: 'avg_score_legit', label: 'Avg. score on real legit', decimals: 4 },
]

function fmt(value, decimals) {
  return value === null || value === undefined ? '—' : value.toFixed(decimals)
}

// Round-impact panel -- AUC on the held-out validation set barely moves
// because it's already ~0.99 (a ranking metric near its own ceiling), so it
// can't show a round mattered. This reuses evaluation/fl_demo_impact.py's
// own scoring logic against a real demo scenario's actual transactions,
// with a genuine before/after snapshot of the round that was JUST run --
// flagged/false-positive/missed counts, and the more sensitive signal:
// average confidence on real fraud vs. real legit, which can shift even
// when the flag/no-flag count doesn't. Lives in this component's own state,
// set directly from the /start response -- not polled, not persisted, a
// fresh one-off comparison per click.
function ImpactPanel({ impact }) {
  if (!impact) return null
  const { before, after } = impact

  return (
    <div className="live-results-section">
      <div className="viz-header">
        <h3>Impact Metric</h3>
        <span className="live-results-meta">{impact.case_label}</span>
      </div>
      {/* <p className="viz-hint">
        AUC barely moves once it's already ~0.99 — this scores the actual demo transactions
        before and after this specific round instead. A higher score on real fraud or a lower
        score on real legit means the model got more confident, even if the flag/no-flag count
        didn't change.
      </p> */}
      <div className="fl-impact-table">
        <div className="fl-impact-row fl-impact-header">
          <span>Metric</span>
          <span>Before</span>
          <span>After</span>
          <span>Change</span>
        </div>
        {IMPACT_ROWS.map((row) => {
          const b = before[row.key]
          const a = after[row.key]
          const diff = b !== null && b !== undefined && a !== null && a !== undefined ? a - b : null
          return (
            <div className="fl-impact-row" key={row.key}>
              <span>{row.label}</span>
              <span>{fmt(b, row.decimals)}</span>
              <span>{fmt(a, row.decimals)}</span>
              <span>{diff === null ? '—' : `${diff >= 0 ? '+' : ''}${fmt(diff, row.decimals)}`}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// Static evidence panel -- real PNGs already on disk from actual FL runs in
// an earlier session (evaluation/fl_before_after.py, fl_vs_isolated.py,
// fl_multi_cycle_trend.py — see CLAUDE.md's "Session 5 Update"). Fetched
// once, not polled -- nothing here is live-generated.
function EvidencePanel() {
  const [items, setItems] = useState([])
  const [snapshots, setSnapshots] = useState({})

  useEffect(() => {
    fetch('/api/tab4/evidence')
      .then((res) => res.json())
      .then((data) => {
        setItems(data.items || [])
        setSnapshots(data.snapshots || {})
      })
      .catch(() => {
        // backend unreachable — leave panel empty
      })
  }, [])

  const before = snapshots.before
  const after = snapshots.after

  return (
    <div className="live-results-section">
      <div className="viz-header">
        <h3>Evidence from Real FL Runs</h3>
      </div>
      <p className="viz-hint">
        Not simulated — these are the actual plots and model snapshots produced by real FL rounds
        in an earlier session.
      </p>
      {before && after && (
        <div className="fl-before-after">
          <span>
            AUC: {before.auc.toFixed(4)} → {after.auc.toFixed(4)}
          </span>
          <span>Precision: {(before.precision * 100).toFixed(1)}%</span>
          <span>Recall: {(before.recall * 100).toFixed(1)}%</span>
          <span>FPR: {(before.fpr * 100).toFixed(1)}%</span>
        </div>
      )}
      {items.length === 0 ? (
        <div className="viz-empty">No FL evidence found in evaluation/fl_before_after/ yet.</div>
      ) : (
        <div className="viz-grid">
          {items.map((item) => (
            <a
              key={item.filename}
              className="viz-card"
              href={`${item.url}?t=${item.mtime}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <img src={`${item.url}?t=${item.mtime}`} alt={item.label} />
              <div className="viz-card-label">{item.label}</div>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Tab4() {
  const [resetCount, setResetCount] = useState(0)
  const [resetting, setResetting] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState(null)
  const [impact, setImpact] = useState(null)

  const handleReset = async () => {
    setError(null)
    setResetting(true)
    try {
      const res = await fetch('/api/tab4/reset', { method: 'POST' })
      if (!res.ok) {
        setError('reset failed to start')
        return
      }
      setResetCount((c) => c + 1)
    } catch {
      setError('could not reach backend')
    } finally {
      setResetting(false)
    }
  }

  const handleStart = async () => {
    setError(null)
    setStarting(true)
    setImpact(null)
    try {
      const res = await fetch('/api/tab4/start', { method: 'POST' })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) {
        setError(body.detail || 'failed to start')
        return
      }
      // The backend always restarts all 4 FL terminals fresh (a completed
      // round's process already exited on its own, so there's no "already
      // running" shell to reuse) -- PtySession.start() clears its
      // subscriber set as part of that, so a terminal card whose WebSocket
      // connected before this click is now silently orphaned. Remount all 4
      // (same resetCount trick Reset already uses) so they reconnect fresh
      // and pick up the new session's scrollback/live stream.
      setResetCount((c) => c + 1)
      // The backend waits out the whole round (up to ~80s total) before
      // responding, so this arrives already computed -- see tab4.py's
      // _score_impact_case, run once before the round and once after.
      if (body.impact) setImpact(body.impact)
    } catch {
      setError('could not reach backend')
    } finally {
      setStarting(false)
    }
  }

  const startButton = (
    <button
      type="button"
      className="terminal-card-action"
      onClick={handleStart}
      disabled={starting}
    >
      {starting ? 'Starting…' : 'Start FL Round'}
    </button>
  )

  return (
    <div className="tab1">
      <div className="tab1-toolbar">
        <div className="tab1-status">Federated Learning — real Flower FedAvg across 3 branches</div>
        <div className="tab1-actions">
          <button onClick={handleReset} disabled={resetting} className="reset-btn">
            {resetting ? 'Resetting…' : 'Reset'}
          </button>
        </div>
      </div>

      {error && <div className="tab1-error">{error}</div>}

      <div className="terminal-row terminal-row-master">
        <TerminalCard
          key={`fl-server-${resetCount}`}
          title="FL Server — Flower FedAvg"
          channel="fl-server"
          headerActions={startButton}
        />
      </div>

      <div className="terminal-row terminal-row-branches">
        {FL_CLIENT_CHANNELS.map((c) => (
          <TerminalCard key={`${c.key}-${resetCount}`} title={c.title} channel={c.key} />
        ))}
      </div>

      <p className="viz-hint">
        
      </p>

      <ImpactPanel impact={impact} />
      <StatusStrip />
      <EvidencePanel />
    </div>
  )
}
