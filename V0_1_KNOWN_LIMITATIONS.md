# AgentForge v0.1 Known Limitations

## Answer Contract

Status: LIMITED.

The contract verifies that cited evidence maps to a real source, allowed path and line range, expected content, hash, and request-local evidence record. It does not independently verify semantic entailment between every answer claim and cited text. A valid citation alone is not proof that a claim is correct. User-facing answers should distinguish direct source facts, model inference, and unsupported repository-specific conclusions.

No contract threshold was weakened in the final closure.

## Streaming

Provider-native token streaming is NOT_MEASURED. Historical application lifecycle events do not establish native provider token streaming.

## Host matrix

The final closure relied on R4.2.3 synthetic C2 evidence and did not rerun the full 15-scenario DeepSeek matrix. Earlier scenario results belong to their own historical runs and must not be presented as one current full-matrix PASS.

## Local platform permissions

Three Windows symbolic-link tests were skipped because the process received WinError 1314 while creating links. They are unmeasured here, not passed.

## Cloud use with real repositories

No real user repository source was sent to DeepSeek in this closure. Cloud use with such content requires the product's consent controls and has not been validated by this synthetic acceptance.
