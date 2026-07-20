# Workflow Pack Boundary V1

## Status

This document freezes the future workflow-pack boundary. It does not load a
pack, extract current workflows, alter project policy, or consume a feature
declaration.

## Normative clauses

1. **Pack ownership.** A workflow pack MAY own project policy, task and message
   templates, routing preferences, optional lifecycle gates, repository-
   specific runbooks, and integration-specific acceptance requirements. Pack
   outputs are policy proposals evaluated by core; they are not core identity,
   registry, evidence, or delivery authority.

2. **Canonical identity bypass is prohibited.** A pack MUST NOT create a second
   message identity, treat a task or issue as canonical intent, collapse
   `Agent`, `Endpoint`, and `SessionRef`, or authorize a runtime target from
   display text. This defers to S1 invariants 1 and 2 and the S2 `AgentV1`,
   `EndpointV1`, `SessionRefV1`, and `MessageV1` contracts. On violation, core
   MUST reject the pack output, record a policy conflict, and preserve the
   original canonical objects unchanged.

3. **Exact project scoping bypass is prohibited.** A pack MUST NOT treat a
   missing, empty, `null`, unknown, or mismatched project id as belonging to the
   active project, and MUST NOT inherit another project's paths or policy. This
   defers to S1 invariant 3 and the explicit workspace/project `scope`
   discriminator used by S2 `AgentV1`, `EndpointV1`, `SessionRefV1`,
   `MessageV1`, `DeliveryV1`, `ReceiptV1`, `CapabilitySetV1`, and
   `StateEvidenceV1`. On violation, core MUST reject the entire proposed action
   before routing or mutation and record the exact scope mismatch.

4. **Exact workspace scoping bypass is prohibited.** A pack MUST NOT read,
   write, route, or reconcile an object across workspace boundaries or infer a
   workspace from cwd, path, process, repository display name, or project name.
   This defers to the S1 `Workspace` security namespace and S2 `WorkspaceV1`
   plus each record's exact `workspace_id`. On violation, core MUST reject the
   entire proposed action, record a cross-workspace policy conflict, and MUST
   NOT copy or re-home the object.

5. **Evidence requirements bypass is prohibited.** A pack MUST NOT promote UI
   visibility, process exit, transport carriage, elapsed time, reviewer prose,
   or best-effort evidence into authoritative acceptance or completion. It MUST
   NOT retry ambiguous delivery as if it were rejected. This defers to S1
   capability/evidence quality and ambiguous-non-success rules and S2
   `StateEvidenceV1`, `DeliveryV1`, and `ReceiptV1`. On violation, core MUST
   reject the state transition, preserve unresolved evidence or quarantine,
   and record the conflicting pack proposal.

6. **Trusted registry bypass is prohibited.** A pack MUST NOT select or widen
   executable code, adapter, handler, capability profile, filesystem root,
   repository relationship, endpoint, session, retry policy, or compatibility
   policy from task text, message content, templates, environment input, or
   another untrusted payload. This defers to the S1 trusted-registry rule and S2
   `WorkspaceV1`, `EndpointV1`, and `CapabilitySetV1`. On violation, core MUST
   reject the entire proposal before lookup or execution, record the attempted
   selector, and MUST NOT sanitize it into a registry entry or choose a
   fallback.

7. **Deterministic ambiguity and conflict rule.** When pack policy and core
   policy disagree, or when two applicable pack rules produce more than one
   valid result, core policy wins. The conflict MUST be recorded with pack id
   and revision, core rule, proposed action, exact scope, and evidence
   references. The result MUST NOT be silently merged, chosen by order,
   selected by recency, or retried through another pack. This defers to S1's
   strict object, registry, and ambiguity rules and to S2's closed semantic
   objects. If the conflict prevents one exact authorized result, core MUST
   reject the action and leave canonical state unchanged.
