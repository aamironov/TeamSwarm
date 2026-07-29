# TeamSwarm offline evaluations

Run the local-model baseline after Ollama is running and the models are pulled:

```bash
npx promptfoo@latest eval -c evals/promptfooconfig.yaml
```

The baseline checks versioned task contracts separately from the online
deterministic evaluator recorded for each TeamSwarm task. Add representative
production-safe cases before changing prompts, routing policy, or model tags.
