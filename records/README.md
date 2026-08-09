# records/

Git-tracked, diffable state written by the `exec-tracking` bb plugin and its
future sibling plugins. Unlike the volatile `project_state_root` (`projects/`,
gitignored by design for inbox/queue state), everything here is durable,
reviewable, and committed.

## executed-triples/

Per-project JSONL logs of the resolved `(provider, model, reasoning_level)`
profile that executed each BB thread (GH-617 / GH-630 first scope). One file per
llm-collab project so two projects never collide:

```
executed-triples/<project_id>.jsonl
```

Each line is one record, canonical compact JSON with sorted keys so an unchanged
row stays byte-identical across rewrites (a commit diff shows only the row that
changed). Two shapes:

- resolved: `{model, project_id, provider, reasoning_level, recorded_at, source, status: "resolved", thread_id}`
- unresolved: `{failure_reason, failure_detail?, project_id, provider, recorded_at, status: "unresolved", thread_id}`

A row exists for every `thread.created` the recorder is invoked for, including a
thread whose profile could not be resolved — so an absent row and a failed
resolution are distinguishable. The writer (`bin/record_executed_triple.py`) is the
authority: bounded read, exclusive flock, atomic temp+rename write.
