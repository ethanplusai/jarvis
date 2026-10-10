# Runtime boundaries

`server.py` remains the composition root and turn orchestrator. Existing public
entry points remain in place so the original lifecycle, voice, REST, and tool
tests continue to verify behavior. This is an incremental extraction rather
than a rewrite of working orchestration.

| Boundary | Responsibility | Contract |
|---|---|---|
| `claude_env.py` | Child configuration | Subscription auth and business-credential scrubbing; no environment mutation |
| `platform_capabilities.py` | Platform/tool support | Shared capability names for UI, MCP discovery, brain grant, execution gate |
| `service_lifecycle.py` | Ordered shutdown | Injected owners/callbacks; reap before unlock; propagate failures |
| `process_tree.py` | Process ownership | Job Object/process group attached at spawn; cancellation cleanup |
| `speech_transport.py`, `tts.py` | TTS HTTP transport | Non-streaming/streaming synthesis independent of turn scheduling |
| `speech.py` | Speech scheduling | Interruption, playback acknowledgement, connected-client behavior |
| `diagnostics_state.py`, `diagnostics_api.py` | Observations and REST diagnostics | Configuration is not verification; age-bounded login/TTS observations |
| `run_store.py`, `conversation_store.py` | Durable run/conversation state | Atomic terminal claim and message receipt; stable history cursors |
| `maintenance.py`, `data_api.py` | Data lifecycle | Verified backups, offline rollback-preserving restore, opt-in retention |
| `business_store.py` | Business state | Immutable action proposals, atomic execution claim, versioned records |
| `business_providers.py` | Provider transport | Pinned hosts, bounded requests, no automatic write retries, no credentials in records |
| `business_api.py` | Business workflows and REST | Owner approval boundary, provider result sanitization, proposal-only model tools |

External side effects are separated from persistence: a business action claims
execution before network I/O, and only a provider receipt changes it to submitted.
A crash cannot imply success or authorize a retry. Restoring historical state
invalidates pending approvals. This differs deliberately from a normal local
record update, which is a single version-checked transaction.

Tests cover these boundaries through injected transports, private SQLite stores,
real process trees, and the assembled browser UI. Do not move functions merely
to reduce the composition-root line count; extract a responsibility with its
dependency interface and retain behavior-focused regression coverage.
