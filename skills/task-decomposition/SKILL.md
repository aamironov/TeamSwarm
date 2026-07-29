---
name: task-decomposition
description: Turn a complex goal into a bounded, dependency-aware task graph for specialized agents. Use when work benefits from parallel discovery, implementation, testing, and evaluation.
license: Apache-2.0
metadata:
  author: TeamSwarm
  version: "1.0"
---

# Task decomposition

Analyze the requested outcome before assigning work.

1. Define the observable completion condition.
2. Separate discovery, implementation, verification, and evaluation work.
3. Make tasks independent when they do not need another task's output.
4. Add dependencies only where information or artifacts must flow downstream.
5. Give every task a concrete output contract and measurable acceptance checks.
6. Keep the graph bounded; prefer the fewest tasks that cover the goal.
7. End with a task that evaluates the combined evidence against the completion condition.

Do not execute the work while planning. Do not invent tools or permissions that
the worker agents have not been granted.
