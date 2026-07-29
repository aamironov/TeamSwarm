import Link from "next/link";

const capabilities = [
  ["01", "Plan with intent", "Turn a broad goal into bounded tasks, dependencies, contracts, and an execution budget."],
  ["02", "Grant context deliberately", "Give each agent only the project context and artifacts it needs—never the entire workspace by default."],
  ["03", "Verify the work", "Evaluate outputs, consolidate results, and retain a replayable record of every material decision."],
];

export default function Home() {
  return (
    <main className="landing">
      <nav className="landing-nav" aria-label="Main navigation">
        <Link className="brand" href="#top" aria-label="TeamSwarm home">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span>TeamSwarm</span>
        </Link>
        <div className="nav-links">
          <a href="#how-it-works">How it works</a>
          <a href="#principles">Principles</a>
          <Link className="nav-cta" href="/dashboard">Open dashboard <span aria-hidden="true">↗</span></Link>
        </div>
      </nav>

      <section className="landing-hero" id="top">
        <div className="hero-copy">
          <p className="landing-kicker"><span /> MULTI-AGENT ORCHESTRATION</p>
          <h1>Build an AI team<br />that stays <em>in sync.</em></h1>
          <p className="hero-description">TeamSwarm turns a group of capable models into a controlled, observable system—where every task has a purpose, every agent has boundaries, and every result can be trusted.</p>
          <div className="hero-actions">
            <Link className="button-primary" href="/dashboard">Launch control plane <span aria-hidden="true">→</span></Link>
            <a className="button-quiet" href="#how-it-works">See the workflow <span aria-hidden="true">↓</span></a>
          </div>
          <div className="hero-proof" aria-label="TeamSwarm benefits">
            <span><b>Provider-neutral</b> OpenAI, Ollama, and mock providers</span>
            <span><b>Context-aware</b> Permissioned artifact sharing</span>
          </div>
        </div>

        <div className="swarm-console" aria-label="Illustration of TeamSwarm coordinating agent tasks">
          <div className="console-topline"><span>LIVE ORCHESTRATION</span><span className="live-status"><i /> SYSTEM READY</span></div>
          <div className="console-content">
            <div className="console-lead">
              <span className="node-icon lead-icon">✦</span>
              <div><small>LEAD AGENT</small><strong>Coordinate product launch</strong></div>
              <span className="node-state">active</span>
            </div>
            <div className="signal-line"><i /><i /><i /></div>
            <div className="agent-grid">
              <article className="agent-node research"><div><span className="node-icon">⌕</span><small>RESEARCH</small></div><strong>Market brief</strong><p><i /> complete</p></article>
              <article className="agent-node build"><div><span className="node-icon">⌘</span><small>BUILD</small></div><strong>Launch plan</strong><p><i /> in progress</p></article>
              <article className="agent-node evaluate"><div><span className="node-icon">✓</span><small>EVALUATE</small></div><strong>Quality checks</strong><p><i /> waiting</p></article>
            </div>
            <div className="console-log"><span>RUN / 84F2</span><p><b>Artifact grant issued</b><br />research → build <i>read only</i></p></div>
          </div>
        </div>
      </section>

      <section className="trust-strip" aria-label="Product guarantees">
        <p><span>◌</span> Dependency-aware queue</p><p><span>◇</span> Traceable decisions</p><p><span>⊞</span> Token &amp; cost metering</p><p><span>⌁</span> Persistent project context</p>
      </section>

      <section className="workflow-section" id="how-it-works">
        <div className="section-heading"><p className="landing-kicker"><span /> THE OPERATING MODEL</p><h2>One lead. Many specialists.<br /><em>Clear handoffs.</em></h2></div>
        <div className="workflow-list">
          {capabilities.map(([number, title, description]) => <article className="workflow-item" key={number}><span className="workflow-number">{number}</span><div><h3>{title}</h3><p>{description}</p></div><span className="workflow-arrow" aria-hidden="true">↗</span></article>)}
        </div>
      </section>

      <section className="principles-section" id="principles">
        <div className="principles-card">
          <p className="landing-kicker"><span /> DESIGNED FOR CONTROL</p>
          <h2>Autonomy without<br />the <em>blind spots.</em></h2>
          <p>Most agent systems optimize for output. TeamSwarm also optimizes for the path taken: who accessed what, why a model was selected, how much it cost, and whether the result met its contract.</p>
          <Link href="/dashboard" className="text-link">Explore the control plane <span aria-hidden="true">→</span></Link>
        </div>
        <div className="principle-grid">
          <article><span className="principle-icon">⌁</span><h3>Context is a capability</h3><p>Artifact sharing is deny-by-default and issued by the lead agent per task.</p></article>
          <article><span className="principle-icon">◫</span><h3>Models are interchangeable</h3><p>Route fast, strong, local, or remote models according to task difficulty and cost.</p></article>
          <article><span className="principle-icon">◷</span><h3>Every decision leaves a trail</h3><p>Replay tasks, inspect routing, evaluate results, and see usage across the system.</p></article>
        </div>
      </section>

      <section className="landing-cta">
        <p className="landing-kicker"><span /> READY TO ORCHESTRATE</p>
        <h2>Give your agents<br />a system to work in.</h2>
        <Link className="button-primary" href="/dashboard">Open TeamSwarm <span aria-hidden="true">→</span></Link>
      </section>

      <footer className="landing-footer"><Link className="brand" href="#top"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>TeamSwarm</span></Link><p>Provider-neutral multi-agent orchestration.</p><Link href="/dashboard">Control plane ↗</Link></footer>
    </main>
  );
}
