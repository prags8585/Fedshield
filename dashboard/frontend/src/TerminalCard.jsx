import { useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

// Wider than any card could realistically show, so real lines never wrap --
// the card scrolls horizontally instead, like scrolling a real wide terminal.
const FIXED_COLS = 220

export default function TerminalCard({ title, channel, headerActions, wsBase = '/ws/tab1' }) {
  const containerRef = useRef(null)

  useEffect(() => {
    const term = new Terminal({
      fontSize: 13,
      scrollback: 5000,
      cursorBlink: true,
      theme: {
        background: '#0b0e16',
        foreground: '#dce1ec',
        cursor: '#c9a961',
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(containerRef.current)

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${window.location.host}${wsBase}/${channel}`)
    ws.binaryType = 'arraybuffer'

    let lastSize = null
    const sendResize = () => {
      if (ws.readyState !== WebSocket.OPEN) return
      const { rows, cols } = term
      if (lastSize && lastSize.rows === rows && lastSize.cols === cols) return
      lastSize = { rows, cols }
      ws.send(JSON.stringify({ type: 'resize', rows, cols }))
    }

    const applyFit = () => {
      fit.fit()
      if (term.cols !== FIXED_COLS) {
        term.resize(FIXED_COLS, term.rows)
      }
      sendResize()
    }
    applyFit()

    ws.onopen = () => sendResize()
    ws.onmessage = (event) => term.write(new Uint8Array(event.data))
    ws.onerror = () => term.write('\r\n\x1b[31m[dashboard] websocket error\x1b[0m\r\n')
    ws.onclose = () => term.write('\r\n\x1b[90m[dashboard] disconnected\x1b[0m\r\n')

    const dataDisposable = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(data))
      }
    })

    window.addEventListener('resize', applyFit)
    const resizeObserver = new ResizeObserver(applyFit)
    resizeObserver.observe(containerRef.current)

    return () => {
      window.removeEventListener('resize', applyFit)
      resizeObserver.disconnect()
      dataDisposable.dispose()
      ws.close()
      term.dispose()
    }
  }, [channel, wsBase])

  return (
    <div className="terminal-card">
      <div className="terminal-card-title">
        <span>{title}</span>
        {headerActions}
      </div>
      <div className="terminal-card-body" ref={containerRef} />
    </div>
  )
}
