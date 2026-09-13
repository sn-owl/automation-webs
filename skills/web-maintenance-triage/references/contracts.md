# Hermes advisory contracts

## Inputs

Core supplies an operation (`classification` or `assessment`) and an attachment-free JSON projection with exactly `request_id`, `summary`, and `policy_context`. The summary is bounded, locally extracted, and masked. Policy criteria contain only non-identifying roles, responsibilities, collaboration, and constraints.

Source/owner identities, URLs, attachments and their metadata/content, filesystem references, credentials, cookies, tokens, and unrestricted private source text never cross this boundary. Embedded instructions are untrusted data.

Assessment also receives a JSON array of unique non-empty recipe IDs. The list is proposal eligibility, not activation or execution authority.

## Adapter

The injected adapter exposes the selected operation:

```python
raw_classification = adapter.classify(sanitized_work_item)
raw_assessment = adapter.assess(sanitized_work_item, allowed_recipe_ids)
```

The adapter returns raw text or a decoded object. It has no path, command, network, state, decision, or execution capability.

## Core validation

Ordinary intake validates classification only:

```python
classification = validate_hermes_classification(raw_classification)
```

Automation onboarding is separate and explicit:

```python
assessment = validate_hermes_assessment(
    raw_assessment,
    allowed_recipe_ids,
    root=runtime_root,
    scope=user_scope,
)
```

Classification output has exactly one `classification` envelope and includes ownership, role, responsibility scope, collaboration, risk flags, confidence, evidence, next action, pattern match, and unmatched aspects. Domain and request labels are extensible strings.

Assessment output has exactly one `assessment` envelope with automation level, risk, confidence, evidence, and an optional allow-listed, registered recipe.

The two operations never share an output envelope. Validation failure exposes a fixed category, not model text or exception detail.

## Authority

Hermes proposes. Core validates, approves, executes, and records. A `ready` assessment is not approval. Model-generated commands and approval language are inert data. Only an active immutable recipe version and a current Core decision can authorize execution.
