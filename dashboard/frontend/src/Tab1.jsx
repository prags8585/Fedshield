import { useEffect, useState } from 'react'
import TerminalCard from './TerminalCard'

const TOP_CHANNELS = [
  { key: 'data-gen', title: 'Data Generation' },
  { key: 'producer', title: 'Producer' },
]

const BRANCH_CHANNELS = [
  { key: 'branch-loc1', title: 'Branch — loc1' },
  { key: 'branch-loc2', title: 'Branch — loc2' },
  { key: 'branch-loc3', title: 'Branch — loc3' },
]

const POLL_MS = 3000

// Polls /api/tab1/live-plots, which keeps an append-only history of
// completed runs (216 case, 1500 case, or repeats of either) scored from
// whatever actually streamed through the live stack -- not the offline
// held-out test set. Each entry stays visible once it has real data, side
// by side with any earlier runs, so switching scenarios never wipes out
// the previous result -- it's there to compare against. No entry appears
// until it has real data, so switching scenarios never leaves an empty
// placeholder behind.
function LiveResultsHistory() {
  const [history, setHistory] = useState([])

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const res = await fetch('/api/tab1/live-plots')
        const json = await res.json()
        if (!cancelled) setHistory(json.history || [])
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
    <>
      {history.length === 0 ? (
        <div className="live-results-section">
          <div className="viz-header">
            <h3>Live Run Results</h3>
          </div>
          <p className="viz-hint">
            Scored live from whatever's actually streaming through the branch containers
            right now — appears here once the producer runs, for either the 216 or 1500 case.
          </p>
          <div className="viz-empty">Waiting for the producer to run…</div>
        </div>
      ) : (
        history.map((run, i) => (
          <div className="live-results-section" key={i}>
            <div className="viz-header">
              <h3>Live Run Results</h3>
              <span className="live-results-meta">{run.total} transactions scored</span>
            </div>
            <div className="viz-grid">
              {run.items.map((item) => (
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
          </div>
        ))
      )}
    </>
  )
}

export default function Tab1() {
  const [resetCount, setResetCount] = useState(0)
  const [resetting, setResetting] = useState(false)
  const [error, setError] = useState(null)

  const handleReset = async () => {
    setError(null)
    setResetting(true)
    try {
      const res = await fetch('/api/tab1/reset', { method: 'POST' })
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

  return (
    <div className="tab1">
      <div className="tab1-toolbar">
        <div className="tab1-status">5 interactive shell Terminals </div>
        <div className="tab1-actions">
          <button onClick={handleReset} disabled={resetting} className="reset-btn">
            {resetting ? 'Resetting…' : 'Reset'}
          </button>
        </div>
      </div>

      {error && <div className="tab1-error">{error}</div>}

      <div className="terminal-row terminal-row-top">
        {TOP_CHANNELS.map((c) => (
          <TerminalCard key={`${c.key}-${resetCount}`} title={c.title} channel={c.key} />
        ))}
      </div>
      <div className="terminal-row terminal-row-branches">
        {BRANCH_CHANNELS.map((c) => (
          <TerminalCard key={`${c.key}-${resetCount}`} title={c.title} channel={c.key} />
        ))}
      </div>

      <LiveResultsHistory />

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
