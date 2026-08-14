import { useEffect, useState } from 'react'
import TerminalCard from './TerminalCard'

// Prefixed so this tab's master/producer/branch terminals never share a PTY
// (or a WebSocket) with Tab 3's — each tab gets its own five shells, wired to
// their own /ws/agents/tab2-* route in the backend (see agent_flow.py).
const AGENTS_WS_BASE = '/ws/agents'

const BRANCH_CHANNELS = [
  { key: 'tab2-branch-loc1', title: 'Branch — loc1' },
  { key: 'tab2-branch-loc2', title: 'Branch — loc2' },
  { key: 'tab2-branch-loc3', title: 'Branch — loc3' },
]

const POLL_MS = 3000

function timeAgo(iso) {
  if (!iso) return ''
  const label = new Date(iso).toLocaleTimeString()
  return label
}

// Section 1 — Structuring Agent. Agent 1 no longer renders a per-token
// verdict; it's pure list-and-describe now (see CLAUDE.md's "Post-Session 6
// Extension - Reframed 3-Agent Pipeline"). Polls the running Redis list
// directly (structuring_log), newest first, in its own scrollable panel so
// this section's growth never pushes the rest of the page down.
function StructuringSection() {
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/tab2/structuring-log')
        const data = await res.json()
        if (!cancelled) {
          setItems(data.items || [])
          setTotal(data.total || 0)
        }
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

  return (
    <div className="pipeline-section">
      <div className="viz-header">
        <h3>1 · Structuring Agent</h3>
        {total > 0 && <span className="live-results-meta">{total} flagged txn(s) logged</span>}
      </div>
      <p className="viz-hint">Every flagged transaction, fraud or false positive, as it arrives.</p>
      <div className="pipeline-section-body">
        {items.length === 0 ? (
          <div className="viz-empty">Waiting for the ML model to flag a transaction…</div>
        ) : (
          items.map((item, i) => (
            <div className="structuring-row" key={`${item.token_id}-${item.txn_id}-${i}`}>
              <div className="structuring-row-header">
                <span className="investigation-token">{item.token_id}</span>
                <span className="structuring-row-meta">
                  {item.branch_id} · score {item.score?.toFixed(3)} · {timeAgo(item.flagged_at)}
                </span>
              </div>
              <p className="agent-box-text">{item.summary}</p>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

// Section 2 — Money-Trail Agent. Every member of a confirmed ring gets the
// identical evidence (see money_trail_agent.py's group-write step), so the
// backend already groups by ring_id -- one card per real ring here, not one
// per token. Verdict is folded into the same card since it's now
// display-only context on the ring, not a separate gate (Reporting no
// longer waits on it either — see CLAUDE.md).
function MoneyTrailSection() {
  const [rings, setRings] = useState([])
  const [deadEnds, setDeadEnds] = useState([])

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/tab2/rings')
        const data = await res.json()
        if (!cancelled) {
          setRings(data.rings || [])
          setDeadEnds(data.dead_ends || [])
        }
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

  return (
    <div className="pipeline-section">
      <div className="viz-header">
        <h3>2 · Money-Trail Agent</h3>
        {rings.length > 0 && <span className="live-results-meta">{rings.length} ring(s) found</span>}
      </div>
      <p className="viz-hint">Confirmed convergence rings, plus tokens that never converged.</p>
      <div className="pipeline-section-body">
        {rings.length === 0 && deadEnds.length === 0 ? (
          <div className="viz-empty">Waiting for a convergence to resolve…</div>
        ) : (
          <>
            {rings.map((ring) => {
              const verdictBadge = ring.verdict
                ? `${ring.verdict.verdict} (${Math.round((ring.verdict.confidence || 0) * 100)}%)`
                : null
              const verdictBadgeClass = ring.verdict?.verdict === 'GUILTY' ? 'badge-guilty' : 'badge-not-guilty'
              return (
                <div className="ring-card" key={ring.ring_id}>
                  <div className="structuring-row-header">
                    <span className="investigation-token">{ring.ring_id}</span>
                    <span className="structuring-row-meta">
                      {(ring.all_tokens || []).length} account(s) · converged on {ring.convergence_node}
                    </span>
                  </div>
                  <p className="agent-box-text">{ring.summary}</p>
                  {ring.verdict && (
                    <>
                      <span className={`agent-box-badge ${verdictBadgeClass}`}>{verdictBadge}</span>
                      <p className="agent-box-text ring-rationale">{ring.verdict.rationale}</p>
                    </>
                  )}
                </div>
              )
            })}
            {deadEnds.length > 0 && (
              <div className="dead-end-list">
                <div className="dead-end-list-header">
                  {deadEnds.length} token(s) never converged (insufficient evidence / cycle)
                </div>
                {deadEnds.map((d) => (
                  <div className="dead-end-row" key={d.token_id}>
                    <span className="investigation-token">{d.token_id}</span>
                    <span className="structuring-row-meta">{d.stop_reason}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// Section 3 — Reporting Agent. Fires independent of the Verdict Agent's own
// opinion the moment ANY ring first converges (see CLAUDE.md) — so this
// section just needs to know whether reports/fraud_rings_report.xlsx exists
// yet, and hand back a real download.
function ReportSection() {
  const [status, setStatus] = useState({ ready: false })

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/tab2/report')
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

  return (
    <div className="pipeline-section pipeline-section-report">
      <div className="viz-header">
        <h3>3 · Reporting Agent</h3>
        {status.ready && <span className="live-results-meta">{status.ring_count} ring(s) reported</span>}
      </div>
      <div className="pipeline-section-body pipeline-section-body-report">
        {status.ready ? (
          <>
            <p className="blink-text">Your report is ready to download</p>
            <a
              className="report-download-btn"
              href="/api/tab2/report/download"
              download="fraud_rings_report.xlsx"
            >
              Download Report
            </a>
          </>
        ) : (
          <div className="viz-empty">Waiting for the first ring to converge…</div>
        )}
      </div>
    </div>
  )
}

export default function Tab2() {
  const [resetCount, setResetCount] = useState(0)
  const [resetting, setResetting] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState(null)

  const handleReset = async () => {
    setError(null)
    setResetting(true)
    try {
      const res = await fetch('/api/tab2/reset', { method: 'POST' })
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
    try {
      const res = await fetch('/api/tab2/start', { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        setError(body.detail || 'failed to start')
      }
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
      {starting ? 'Starting…' : 'Start'}
    </button>
  )

  return (
    <div className="tab1">
      <div className="tab1-toolbar">
        <div className="tab1-status">
          Click Start to run the agentic flow.
        </div>
        <div className="tab1-actions">
          <button onClick={handleReset} disabled={resetting} className="reset-btn">
            {resetting ? 'Resetting…' : 'Reset'}
          </button>
        </div>
      </div>

      {error && <div className="tab1-error">{error}</div>}

      <div className="terminal-row terminal-row-master">
        <TerminalCard
          key={`master-${resetCount}`}
          title="Master — Orchestrator"
          channel="tab2-master"
          wsBase={AGENTS_WS_BASE}
          headerActions={startButton}
        />
      </div>

      <div className="terminal-row terminal-row-top">
        <TerminalCard key={`data-gen-${resetCount}`} title="Data Generation" channel="data-gen" />
        <TerminalCard
          key={`producer-${resetCount}`}
          title="Producer"
          channel="tab2-producer"
          wsBase={AGENTS_WS_BASE}
        />
      </div>
      <div className="terminal-row terminal-row-branches">
        {BRANCH_CHANNELS.map((c) => (
          <TerminalCard key={`${c.key}-${resetCount}`} title={c.title} channel={c.key} wsBase={AGENTS_WS_BASE} />
        ))}
      </div>

      <div className="pipeline-grid">
        <StructuringSection />
        <MoneyTrailSection />
        <ReportSection />
      </div>

      <div className="neo4j-section">
        <h3>Neo4j Cypher Playground</h3>
        <div className="neo4j-placeholder">
          <p>
            Neo4j Browser sends <code>X-Frame-Options: DENY</code> and can't be embedded
            in-page. Open it in its own tab instead.
          </p>
          <a href="http://localhost:7475" target="_blank" rel="noopener noreferrer">
            <button type="button">Open Neo4j Browser ↗</button>
          </a>
        </div>
      </div>
    </div>
  )
}
