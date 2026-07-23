# ADR-0001 Phase 2 authenticated broker contract

- **Status:** Contract implemented and verified; native transport/provider enablement remains blocked
- **Parent:** [Phase 2 signed-manifest gate](0001-phase2-signed-manifest.md)
- **Scope:** Bounded authenticated frames, canonical broker messages, direction keys,
  peer ACL/identity policy, and installation/job ownership authorization

## Decision

The future local executor broker uses a transport-neutral framed protocol before any
named-pipe adapter is allowed to dispatch work. A frame has a fixed binary header,
version, zero flags/reserved bits, positive monotonic sequence, bounded payload length,
payload bytes, and a 32-byte HMAC-SHA-256 tag over header plus payload. The hard payload
ceiling is 64 KiB and the decoder bounds chunk and buffered input. Unknown header values,
truncated frames, trailing bytes, oversized lengths, invalid keys, and sequence replay
fail closed.

The broker session has independent 32-byte-or-longer MAC keys for `to_broker` and
`to_executor` traffic. A valid frame cannot be reflected across directions. The key
establishment and named-pipe transport are deliberately not implemented here; a future
native adapter must provision the keys through a reviewed parent/child handshake.
The implementation keeps frame authentication/sequencing separate from message
canonicalization: a message decoder accepts only an already authenticated frame, and
the authorization step remains a distinct trusted-peer/job-owner check.

## Canonical message contract

The frame payload is canonical ASCII JSON for `broker.message.v1`:

- direction: `to_broker` or `to_executor`;
- operation: `prepare`, `input_chunk`, `input_complete`, `cancel`, or `collect`
  (`start` remains a transport-compatibility value and is rejected by the fixed
  worker rather than being guessed as an input operation);
- bounded request and job identifiers;
- the 256-bit installation principal; and
- a bounded body for a later operation-specific schema.

The body rejects authority-bearing fields recursively: paths, source, commands, shells,
executables, network targets, and tokens cannot cross this generic broker envelope.
Future operation schemas may add only typed artifact IDs and bounded values. Noncanonical
JSON is rejected even when its MAC is valid, so idempotency and audit digests have one
representation.

## Peer ACL and confused-deputy policy

The native transport must construct a named-pipe DACL from `BrokerAclPolicy`, allowing
only the configured user SID and AppContainer SID. After connection, it must obtain the
peer process token and provide `PeerIdentity` to `BrokerPeerPolicy`; the policy checks:

1. expected process ID when a launch binding exists;
2. exact user and AppContainer SIDs;
3. maximum integrity level (low by default); and
4. no unvalidated peer metadata supplied by the wire message.

`authorize_message` then binds the message's installation principal to the trusted
session principal and asks a trusted repository lookup for the job owner. A request is
rejected if the wire principal, peer ACL, expected process, integrity, or durable owner
does not match. This prevents a broker from becoming a confused deputy for another
installation or job.

## Failure and lifecycle contract

Failures expose stable categories only: `frame_*`, `message_*`, `peer_*`,
`broker_principal_*`, `broker_owner_*`. No HMAC key, token, path, payload, or OS error
detail is returned. A native adapter must close the connection on any protocol or
identity error, discard the session sequence, and leave the execution lifecycle
unavailable. The broker contract has no retry, fallback transport, subprocess, or
provider behavior.

## Explicit non-goals

This ADR does not create a named pipe, set a Windows security descriptor, query a token,
derive session keys, spawn or supervise an executor, stage artifacts, or execute any
operation. Native DACL construction, peer-token acquisition, handshake design,
copy-in/output validation, and OS sandbox qualification require separate evidence and
review before the lifecycle health gate can become enabled.

## Verification

`tests/test_phase2_broker.py` covers MAC tampering, replay/sequence, payload/chunk
limits, incremental frames, direction reflection, canonical JSON, nested authority
fields, exact ACL and integrity checks, expected process binding, principal mismatch,
job-owner mismatch, and safe lookup failures.
