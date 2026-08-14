import { useState } from 'react'
import Tab1 from './Tab1'
import Tab2 from './Tab2'
import Tab3 from './Tab3'
import Tab4 from './Tab4'
import './App.css'

const TABS = [
  { key: 'manual', label: 'Manual (216)' },
  { key: 'agentic216', label: 'Agentic (216)' },
  { key: 'agentic500', label: 'Agentic (1500)' },
  { key: 'fl', label: 'FL' },
]

function App() {
  const [active, setActive] = useState('manual')

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <svg className="brand-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path
              d="M16 2 L28 7 V15 C28 22.5 23 27.5 16 30 C9 27.5 4 22.5 4 15 V7 Z"
              stroke="currentColor"
              strokeWidth="1.6"
            />
            <path d="M11 16 L14.5 19.5 L21 12" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div className="brand-text">
            <h1>FedShield</h1>
            <p>Federated Fraud Intelligence Platform</p>
          </div>
        </div>
        <nav className="tab-bar">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`tab-btn ${active === t.key ? 'active' : ''}`}
              onClick={() => setActive(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {active === 'manual' && <Tab1 />}
        {active === 'agentic216' && <Tab2 />}
        {active === 'agentic500' && <Tab3 />}
        {active === 'fl' && <Tab4 />}
      </main>
    </div>
  )
}

export default App
