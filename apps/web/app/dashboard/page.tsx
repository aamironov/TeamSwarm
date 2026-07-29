"use client";

import { FormEvent, useEffect, useState } from "react";

type Task = {
  id: string;
  objective: string;
  dependencies: string[];
  model_profile: string;
  status: string;
  queue_status?: string | null;
  worker_id?: string | null;
  output?: string | null;
  error?: string | null;
};
type Run = {
  id: string;
  objective: string;
  status: string;
  final_output?: string | null;
  error?: string | null;
  tasks: Task[];
};
type Usage = {
  consumed_tokens: number;
  token_limit: number;
  consumed_cost_usd: number;
  cost_limit_usd: number;
};
type Model = {
  id: string;
  provider: string;
  location: "local" | "remote";
  availability: "available" | "configured" | "not_installed";
  profiles: string[];
};
type ModelCatalog = { active_provider: string; models: Model[] };
type Skill = { name: string; description: string; content_hash: string; allowed_tools: string[] };
type TokenUsageWindow = {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  by_model: Array<{ model: string; total_tokens: number }>;
};
type TraceEvent = { kind: string; message: string; task_id?: string | null; metadata: Record<string, string | number | boolean> };
type Trace = { events: TraceEvent[] };
type Project = { id: string; name: string; directory: string };
type ChatMessage = { id: string; role: "user" | "assistant"; content: string; model?: string | null; context_hash?: string | null };
type Chat = { id: string; project_id: string; title: string; messages: ChatMessage[] };
type PromptAttachment = { filename: string; content: string };

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function Dashboard() {
  const [objective, setObjective] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [plannerBackend, setPlannerBackend] = useState<"deterministic" | "provider-agent" | "autogen">("deterministic");
  const [workflow, setWorkflow] = useState<"standard" | "delivery_cycle">("delivery_cycle");
  const [usage24h, setUsage24h] = useState<TokenUsageWindow | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectDirectory, setProjectDirectory] = useState("");
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [chats, setChats] = useState<Chat[]>([]);
  const [selectedChat, setSelectedChat] = useState<Chat | null>(null);
  const [chatPrompt, setChatPrompt] = useState("");
  const [chatAttachments, setChatAttachments] = useState<PromptAttachment[]>([]);
  const [runAttachments, setRunAttachments] = useState<PromptAttachment[]>([]);
  const [chatSending, setChatSending] = useState(false);
  const [chatError, setChatError] = useState("");
  const [error, setError] = useState("");
  const isActive = run?.status === "pending" || run?.status === "running";

  async function readAttachments(files: FileList | null): Promise<PromptAttachment[]> {
    if (!files) return [];
    return Promise.all(Array.from(files).map(async (file) => ({ filename: file.name, content: await file.text() })));
  }

  useEffect(() => {
    let mounted = true;
    async function refreshDashboard() {
      const [modelsResponse, usageResponse, skillsResponse] = await Promise.all([
        fetch(`${apiBase}/models`),
        fetch(`${apiBase}/usage/last-24-hours`),
        fetch(`${apiBase}/skills`),
      ]);
      if (!mounted) return;
      if (modelsResponse.ok) setCatalog(await modelsResponse.json());
      if (usageResponse.ok) setUsage24h(await usageResponse.json());
      if (skillsResponse.ok) setSkills(await skillsResponse.json());
    }
    void refreshDashboard();
    const timer = window.setInterval(() => void refreshDashboard(), 30_000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    void fetch(`${apiBase}/projects`)
      .then((response) => response.ok ? response.json() : [])
      .then((nextProjects) => setProjects(nextProjects));
  }, []);

  useEffect(() => {
    if (!selectedProjectId) {
      setChats([]);
      setSelectedChat(null);
      return;
    }
    void fetch(`${apiBase}/projects/${selectedProjectId}/chats`)
      .then((response) => response.ok ? response.json() : [])
      .then((nextChats) => {
        setChats(nextChats);
        setSelectedChat(null);
      });
  }, [selectedProjectId]);

  useEffect(() => {
    if (!run || !isActive) return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`${apiBase}/runs/${run.id}`);
      if (!response.ok) return;
      const nextRun: Run = await response.json();
      setRun(nextRun);
      const usageResponse = await fetch(`${apiBase}/runs/${run.id}/usage`);
      if (usageResponse.ok) setUsage(await usageResponse.json());
    }, 900);
    return () => window.clearInterval(timer);
  }, [run?.id, isActive]);

  useEffect(() => {
    if (!run) return;
    void fetch(`${apiBase}/runs/${run.id}/trace`)
      .then((response) => response.ok ? response.json() : null)
      .then((nextTrace) => nextTrace && setTrace(nextTrace));
  }, [run?.id, run?.status]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setUsage(null);
    const response = await fetch(`${apiBase}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        objective,
        attachments: runAttachments,
        workflow,
        max_cycles: 2,
        skills: selectedSkills,
        planner_backend: plannerBackend,
      }),
    });
    if (!response.ok) {
      setError((await response.json()).detail ?? "Unable to create run.");
      return;
    }
    const nextRun: Run = await response.json();
    setRun(nextRun);
    setRunAttachments([]);
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setChatError("");
    const response = await fetch(`${apiBase}/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ directory: projectDirectory }),
    });
    if (!response.ok) {
      setChatError((await response.json()).detail ?? "Unable to add project directory.");
      return;
    }
    const project: Project = await response.json();
    setProjects((current) => [...current.filter((item) => item.id !== project.id), project]);
    setSelectedProjectId(project.id);
    setProjectDirectory("");
  }

  async function createChat() {
    if (!selectedProjectId) return;
    const response = await fetch(`${apiBase}/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: selectedProjectId, title: "Project chat" }),
    });
    if (!response.ok) return;
    const chat: Chat = await response.json();
    setChats((current) => [chat, ...current]);
    setSelectedChat(chat);
  }

  async function selectChat(chatId: string) {
    const response = await fetch(`${apiBase}/chats/${chatId}`);
    if (response.ok) setSelectedChat(await response.json());
  }

  async function sendChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedChat || (!chatPrompt.trim() && !chatAttachments.length)) return;
    setChatSending(true);
    setChatError("");
    const response = await fetch(`${apiBase}/chats/${selectedChat.id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: chatPrompt || "Please review the attached files.", model_profile: "fast", attachments: chatAttachments }),
    });
    setChatSending(false);
    if (!response.ok) {
      setChatError((await response.json()).detail ?? "Unable to send message.");
      return;
    }
    const chat: Chat = await response.json();
    setSelectedChat(chat);
    setChats((current) => current.map((item) => item.id === chat.id ? { ...item, title: chat.title } : item));
    setChatPrompt("");
    setChatAttachments([]);
  }

  return (
    <main>
      <section className="hero">
        <p className="eyebrow">TEAMSWARM / MVP</p>
        <h1>Coordinate a small team of AI agents.</h1>
        <p className="lede">The lead agent plans, routes, meters, consolidates, and validates each run.</p>
      </section>

      <section className="dashboard-grid" aria-label="Model and token dashboard">
        <section className="panel dashboard-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">MODEL REGISTRY</p><h2>Available models</h2></div>
            {catalog && <span className="active-provider">active: {catalog.active_provider}</span>}
          </div>
          {catalog ? (
            <div className="model-list">
              {catalog.models.map((model) => (
                <article className="model-row" key={`${model.provider}-${model.id}`}>
                  <div><strong>{model.id}</strong><span>{model.provider}</span></div>
                  <div className="model-tags">
                    <span className={`badge ${model.location}`}>{model.location}</span>
                    <span className={`badge ${model.availability}`}>{model.availability.replace("_", " ")}</span>
                    {model.profiles.map((profile) => <span className="profile" key={profile}>{profile}</span>)}
                  </div>
                </article>
              ))}
            </div>
          ) : <p className="muted">Loading registered models…</p>}
        </section>

        <section className="panel dashboard-panel usage-window">
          <p className="eyebrow">ROLLING WINDOW</p>
          <h2>Tokens in the last 24 hours</h2>
          {usage24h ? (
            <>
              <strong className="token-total">{usage24h.total_tokens.toLocaleString()}</strong>
              <p className="muted">{usage24h.input_tokens.toLocaleString()} input · {usage24h.output_tokens.toLocaleString()} output</p>
              <div className="usage-by-model">
                {usage24h.by_model.length ? usage24h.by_model.map((item) => (
                  <div key={item.model}><span>{item.model}</span><strong>{item.total_tokens.toLocaleString()}</strong></div>
                )) : <p className="muted">No model calls have been recorded yet.</p>}
              </div>
            </>
          ) : <p className="muted">Loading token usage…</p>}
        </section>
      </section>

      <section className="panel chat-panel" aria-label="Project chat">
        <div className="panel-heading"><div><p className="eyebrow">PROJECT CHAT</p><h2>Persistent project conversations</h2></div></div>
        <form className="project-form" onSubmit={createProject}>
          <label htmlFor="project-directory">Project directory</label>
          <div><input id="project-directory" value={projectDirectory} onChange={(event) => setProjectDirectory(event.target.value)} placeholder="/absolute/path/to/project" required /><button>Add project</button></div>
        </form>
        <div className="chat-layout">
          <aside className="chat-sidebar">
            <label htmlFor="project-select">Project context</label>
            <select id="project-select" value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}>
              <option value="">Select a project…</option>
              {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
            <button className="secondary" type="button" disabled={!selectedProjectId} onClick={() => void createChat()}>New chat</button>
            <div className="chat-list">
              {chats.map((chat) => <button className={`chat-list-item ${selectedChat?.id === chat.id ? "selected" : ""}`} key={chat.id} type="button" onClick={() => void selectChat(chat.id)}>{chat.title}</button>)}
            </div>
          </aside>
          <div className="conversation">
            {selectedChat ? <>
              <div className="messages">
                {selectedChat.messages.length ? selectedChat.messages.map((message) => <article className={`message ${message.role}`} key={message.id}><span>{message.role === "assistant" ? message.model ?? "assistant" : "you"}</span><p>{message.content}</p></article>) : <p className="muted">Start a conversation. The selected project directory is supplied as bounded context.</p>}
              </div>
              <form onSubmit={sendChat}>
                <label htmlFor="chat-prompt">Message</label>
                <textarea id="chat-prompt" value={chatPrompt} onChange={(event) => setChatPrompt(event.target.value)} placeholder="Ask about this project…" minLength={1} />
                <label htmlFor="chat-files">Attach text or source files</label>
                <input id="chat-files" type="file" multiple accept=".txt,.md,.py,.ts,.tsx,.js,.jsx,.json,.yaml,.yml,.toml,.csv,.html,.css,.sql,.xml,.log" onChange={(event) => void readAttachments(event.target.files).then(setChatAttachments)} />
                {chatAttachments.length > 0 && <p className="muted">Attached: {chatAttachments.map((item) => item.filename).join(", ")}</p>}
                <button disabled={chatSending || (!chatPrompt.trim() && !chatAttachments.length)}>{chatSending ? "Thinking…" : "Send message"}</button>
              </form>
            </> : <p className="muted">Add a project directory, select it, then create a chat. Chat history is persisted with that project.</p>}
          </div>
        </div>
        {chatError && <p className="error">{chatError}</p>}
      </section>

      <section className="panel">
        <form onSubmit={submit}>
          <label htmlFor="objective">Objective</label>
          <textarea id="objective" value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Describe the work to perform…" minLength={3} required />
          <label htmlFor="run-files">Attach text or source files</label>
          <input id="run-files" type="file" multiple accept=".txt,.md,.py,.ts,.tsx,.js,.jsx,.json,.yaml,.yml,.toml,.csv,.html,.css,.sql,.xml,.log" onChange={(event) => void readAttachments(event.target.files).then(setRunAttachments)} />
          {runAttachments.length > 0 && <p className="muted">Attached: {runAttachments.map((item) => item.filename).join(", ")}</p>}
          <label htmlFor="workflow">Workflow</label>
          <select id="workflow" value={workflow} onChange={(event) => {
            const next = event.target.value as "standard" | "delivery_cycle";
            setWorkflow(next);
            if (next === "delivery_cycle") setPlannerBackend("deterministic");
          }}>
            <option value="delivery_cycle">Delivery cycle</option>
            <option value="standard">Generated task graph</option>
          </select>
          <label htmlFor="planner-backend">Task planner</label>
          <select id="planner-backend" value={plannerBackend} onChange={(event) => {
            const next = event.target.value as "deterministic" | "provider-agent" | "autogen";
            setPlannerBackend(next);
            if (next !== "deterministic") setWorkflow("standard");
          }}>
            <option value="deterministic">Deterministic</option>
            <option value="provider-agent">TeamSwarm planning agent</option>
            <option value="autogen">Microsoft AutoGen planning agent</option>
          </select>
          {skills.length > 0 && <>
            <span className="form-label">Skills</span>
            <div className="skill-picker">
              {skills.map((skill) => <label key={skill.name}>
                <input
                  type="checkbox"
                  checked={selectedSkills.includes(skill.name)}
                  onChange={(event) => setSelectedSkills((current) => event.target.checked
                    ? [...current, skill.name]
                    : current.filter((name) => name !== skill.name))}
                />
                <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
              </label>)}
            </div>
          </>}
          <button disabled={!objective.trim() || isActive}>{isActive ? "Running…" : "Start run"}</button>
        </form>
        {error && <p className="error">{error}</p>}
      </section>
      {run && (
        <section className="panel result">
          <div className="run-header"><span>Run {run.id.slice(0, 8)}</span><strong data-status={run.status}>{run.status}</strong></div>
          <h2>Task graph</h2>
          <div className="task-graph">
            {run.tasks.map((task, index) => {
              const dependencyLabels = task.dependencies.map((dependencyId) => run.tasks.find((candidate) => candidate.id === dependencyId)?.objective ?? dependencyId.slice(0, 8));
              return <article className="graph-node" key={`graph-${task.id}`}><span>Task {index + 1}</span><strong>{task.objective}</strong><p>{dependencyLabels.length ? `← depends on ${dependencyLabels.join(" · ")}` : "Entry task"}</p></article>;
            })}
          </div>
          <h2>Tasks</h2>
          <div className="tasks">
            {run.tasks.map((task) => <article key={task.id}><div><span>{task.model_profile}</span><strong>{task.status}</strong></div><p>{task.objective}</p>{task.dependencies.length > 0 && <p className="dependency">Depends on: {task.dependencies.map((id) => id.slice(0, 8)).join(", ")}</p>}{task.queue_status && <p className="queue-state">Queue: <strong>{task.queue_status}</strong>{task.worker_id && <> · Worker: {task.worker_id}</>}</p>}{task.output && <pre>{task.output}</pre>}{task.error && <p className="error">{task.error}</p>}</article>)}
          </div>
          {usage && <p className="usage">Tokens: {usage.consumed_tokens.toLocaleString()} / {usage.token_limit.toLocaleString()} · Estimated cost: ${usage.consumed_cost_usd.toFixed(4)} / ${usage.cost_limit_usd.toFixed(2)}</p>}
          {run.final_output && <><h2>Consolidated result</h2><pre className="final">{run.final_output}</pre></>}
          {trace && <><h2>Decision timeline</h2><div className="timeline">{trace.events.filter((event) => ["task_claimed", "model_routed", "task_settled", "capability_denied", "task_recovered"].includes(event.kind)).map((event, index) => <p key={`${event.kind}-${index}`}><strong>{event.kind.replaceAll("_", " ")}</strong> {event.message}</p>)}</div></>}
          {run.error && <p className="error">{run.error}</p>}
        </section>
      )}
    </main>
  );
}
