---
name: workspace-coding
description: Inspect, modify, and verify source code in an explicitly approved local workspace. Use for implementation, repair, review, and test tasks that require concrete file changes.
license: Apache-2.0
allowed-tools: workspace.list_files workspace.read_file workspace.write_file workspace.replace_text workspace.run_command git.status
metadata:
  author: TeamSwarm
  version: "1.0"
---

# Workspace coding

Work only inside the approved workspace and use the smallest relevant tool call.

1. Inspect relevant files before editing them.
2. Prefer exact replacement for focused edits; use full-file writes only for new
   files or when a complete replacement is genuinely clearer.
3. Give every mutating call a stable, task-specific idempotency key.
4. Do not request access to `.git`, secrets, environment files, or paths outside
   the workspace.
5. Run focused tests or lint checks after changes.
6. Inspect Git status and report changed files, commands, results, and remaining
   risks in the final response.

Never claim an edit or test occurred unless the corresponding tool result
confirms it.
