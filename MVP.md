# TeamSwarm MVP

Status: Proposed MVP scope

## 1. MVP objective

Deliver a provider-neutral, single-deployment service that can accept a user
request, split it into bounded agent tasks when necessary, execute those tasks
with one initial LLM provider, consolidate the results, validate the final
answer, and show an auditable record of usage and decisions.

The MVP proves that a lead agent can safely coordinate a small team of agents.
It is not a general autonomous-agent platform or a multi-provider production
control plane yet.

## 2. Demonstrable user journey

1. A user submits a request and an optional project context.
2. The Lead Agent either runs it directly or creates up to three bounded tasks.
3. The system assigns each task to either a fast or a strong model profile using
   deterministic routing rules.
4. Tasks run sequentially or in parallel, within an explicit token and cost
   budget.
5. A task can share a completed result only with downstream tasks explicitly
   authorized by the Lead Agent.
6. The Task Consolidator produces one result, preserving the task sources used
   to form it.
7. Deterministic checks and a bounded self-evaluation decide whether to accept,
   repair once, or return a clear failure.
8. The caller can inspect the run, task graph, model choices, token usage, cost
   estimate, cache grants, and validation outcome.

## 3. Scope

### Included

- Request API with run creation, status, cancellation, and final result.
- Provider adapter interface with mock, OpenAI, Bytez, OpenRouter, Ollama, and SGLang
  implementations; one provider mode is active for each run environment.
- Two configured model profiles: `fast` and `strong`.
- Canonical run, task, model-selection, result, cache-grant, and usage-event
  contracts.
- Lead Agent task planner with direct execution or a directed acyclic graph of
  at most three tasks.
- Rule-based difficulty evaluation and model routing.
- Context budgeting using source priority, token estimates, and deterministic
  truncation; no semantic retrieval dependency.
- Context manifests that record selected, summarized, and omitted sources with
  token allocations and provenance.
- Base Prompt Optimizer plus one provider-specific prompt module.
- Agent Orchestrator with sequential and parallel task execution, timeout,
  cancellation, one retry, and a single fallback model.
- Task Consolidator for structured merge, required-task checking, duplicate
  removal, and explicit conflict reporting.
- Agent Result Cache for run-scoped completed artifacts, exact-key reuse, and
  Lead Agent-managed task or consolidation read grants.
- One bounded prompt-quantification workflow: a parent prompt may fan out to at
  most three deterministically partitioned child prompts with a shared immutable
  context prefix and a required consolidation task.
- Immutable workflow revisions plus a bounded review/repair template; a roadmap
  remains for conditional branching, map/reduce, bounded refinement loops, and
  interactive human approval gates.
- Capability-scoped local Tool Gateway with workspace containment, command
  allowlisting, audit records, mutation idempotency, and explicit per-run write
  approval.
- Usage Metering and Subscription Accounting with reservation, settlement, and
  dimensions for tenant, subscription, project, run, task, agent, provider,
  model, token class, request count, cost estimate, and billing period.
- Provider-reported, internally metered, and estimated usage labels.
- Online validation: output-schema checks, required-evidence checks, budget
  checks, and one bounded self-evaluation pass.
- Offline regression evaluation using Promptfoo with a small versioned dataset.
- Run history, structured logs, trace IDs, and immutable result artifacts.
- Database-backed task queue with leased worker claims and dependency-aware
  dispatch.
- Task contracts, capability-scoped context access, immutable artifacts,
  deterministic evaluations, and replayable routing decisions.

### Explicitly deferred

- Additional provider adapters and automated cross-provider fallback.
- Semantic or cross-project cache matching.
- Long-term memory and retrieval-augmented generation.
- Dynamic routing learned from production data.
- Provider invoice or subscription-feed reconciliation, unless the selected
  provider exposes a simple testable usage feed.
- Live traffic experiments, causal effect estimation, and automated prompt
  promotion.
- Autonomous self-improvement; the MVP may create a diagnostic report only.
- Interactive human-review pauses and non-workspace external side effects.
- General workflow templates beyond the existing sequential, parallel,
  retry/fallback, bounded prompt-quantification, delivery-cycle, and
  review/repair paths.
- Multi-region deployment and service decomposition. The MVP supports a
  database-backed worker pool; high-availability scheduling remains deferred.

## 4. MVP component boundaries

```text
Request API
  -> Lead planner
  -> Context budgeter + rule-based router
  -> Durable task queue -> workers
  -> Orchestrator
       -> Prompt module -> Provider adapter
       -> Agent result cache (grant-controlled)
       -> Usage meter (reserve and settle)
  -> Consolidator
  -> Validator + self-evaluation
  -> Final result and trace
```

The Lead Agent owns the task graph and access-grant intent. The runtime enforces
all grants, budgets, retry limits, and policy checks. An LLM may propose a task
or repair, but it cannot directly read cached artifacts, change an access grant,
or bypass budget enforcement.

## 5. MVP data and permission rules

- Every task has a unique task ID and an explicit output schema.
- Every result is stored as an immutable artifact with provenance and validation
  state.
- Cache access is deny-by-default. A result is supplied to another agent only
  after the Lead Agent issues a grant for that recipient task.
- The Task Consolidator receives only the handles required by its consolidation
  plan.
- The Context Optimizer mediates cache reads; raw models never receive cache
  search or enumeration access.
- A shared context prefix is immutable and policy-filtered. A child receives its
  own prompt delta plus only the upstream or sibling artifacts named in its
  grant; agents cannot mutate or discover peer context.
- Prompt quantification records the parent prompt, child prompt variants,
  partition rationale, shared-context hash, per-child budgets, and
  consolidation policy in the trace.
- The default cache scope is one run. Cross-run reuse is out of scope.
- External tools are read-only in the MVP. Writes require a later approval
  workflow.
- The Usage Meter reserves the maximum configured request budget before a model
  call and releases unused capacity after it settles.

## 6. Minimal API surface

```text
POST   /runs                 create a run
GET    /runs/{runId}         get run state and summary
GET    /runs/{runId}/events  stream normalized events
POST   /runs/{runId}/cancel  cancel outstanding work
POST   /runs/{runId}/approval approve or reject a paused protected revision
GET    /runs/{runId}/usage   retrieve dimensional usage and allowance summary
GET    /runs/{runId}/trace   retrieve tasks, routing, grants, and validation
GET    /runs/{runId}/queue   retrieve task queue claims and worker state
GET    /runs/{runId}/replay  retrieve immutable artifacts and evaluations
```

## 7. Acceptance criteria

The MVP is complete when all of the following hold:

- A direct task and a three-task workflow both complete through the API.
- Two independent tasks run concurrently and dependent tasks wait correctly.
- Routing selects the configured `fast` or `strong` profile and records why.
- The system rejects a cyclic task graph, missing output schema, over-budget
  request, or disallowed tool request.
- A downstream task without a cache grant cannot access another task's result.
- A granted task and the consolidator can access only their named result handles.
- A malformed task result triggers one repair attempt or a clear terminal
  failure.
- Consolidation reports a material conflict rather than silently selecting a
  claim.
- Each model invocation creates a usage reservation and a settled or released
  usage event with its relevant dimensions.
- The usage endpoint distinguishes estimated/internal/provider-reported data.
- Promptfoo regression tests pass for the approved baseline dataset.
- The run trace is sufficient to reproduce the selected prompt version, model
  profile, context sources, cache grants, task results, and validation outcome.

## 8. Implementation sequence

1. Create canonical contracts, persistence, run lifecycle, and trace events.
2. Add one provider adapter and direct single-agent execution.
3. Add context budgeting, rule-based routing, budget reservation, and usage
   settlement, including a persisted context manifest.
4. Add task graph validation and the orchestrator's sequential/parallel paths.
5. Add result artifacts, grant-controlled cache reads, and task consolidation.
6. Add bounded prompt quantification: deterministic fan-out, shared immutable
   context prefixes, recipient-specific sibling-result grants, and controlled
   fan-in.
7. Add online validation, self-evaluation, retry/fallback, and cancellation.
8. Add the Promptfoo dataset, CI regression gate, and run-inspection endpoints.
9. Add typed workflow templates in this order: review/repair loop, conditional
   branch, map/reduce, bounded refinement loop, then human approval gate. Each
   template must create immutable graph revisions and enforce iteration, time,
   cost, token, and capability caps.

## 9. Initial evaluation scenarios

- Simple request stays single-agent and uses the `fast` profile.
- Complex request becomes two independent tasks plus a consolidation task.
- Dependent task receives an explicitly granted upstream result.
- Same task without a grant is denied cache access.
- A quantified parent prompt fans out into distinct evidence partitions; each
  child sees the same approved prefix but cannot discover an ungranted sibling
  result.
- The consolidator receives only granted child outputs and reports duplicate or
  conflicting claims with child-level provenance.
- A quantified workflow is compared with its equivalent single-prompt baseline
  for quality, coverage, latency, tokens, and cost.
- Strong-profile fallback is used after a fast-profile malformed result.
- Budget exhaustion denies a new call before provider invocation.
- Provider timeout releases the reservation and records the retry.
- Consolidation detects conflicting upstream claims.
- Self-evaluation finds a missing required field and requests one repair.
- A refinement workflow stops at its declared success condition or its hard
  iteration/budget/deadline limit, with every iteration traceable.
- A conditional workflow records its condition, selected branch, skipped branch,
  and resulting artifacts.

## 10. Decisions required before implementation

- Select the first LLM provider and the two initial model profiles.
- Select implementation language and web framework.
- Select a relational database and artifact store.
- Define the initial tenant, project, and subscription identity model.
- Set token, cost, timeout, and concurrency budgets for the first environment.
- Select the initial structured-output schema format.

## 11. MVP exit decision

After the acceptance criteria pass on representative tasks, choose the next
investment using evidence from the recorded traces and evaluations:

- add a second provider if availability, quality, or cost data justifies it;
- add retrieval if context-budget failures are the main limitation;
- add controlled experiments if prompt or router variants need causal evidence;
- add external write tools only after approval workflows and audit requirements
  are implemented.
