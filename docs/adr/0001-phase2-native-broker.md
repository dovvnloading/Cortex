# ADR-0001 Phase 2 native broker adapter

- **Status:** Implemented and verified; provider enablement remains blocked
- **Parent:** [Phase 2 authenticated broker contract](0001-phase2-broker-contract.md)
- **Scope:** Windows named-pipe transport, protected DACL, OS peer identity, and
  authenticated session-key establishment

## Decision

The transport-neutral broker contract is exposed through one native Windows named
pipe instance only. The adapter accepts a strict local name under `\\.\\pipe\\`,
uses `PIPE_REJECT_REMOTE_CLIENTS`, sets `FILE_FLAG_FIRST_PIPE_INSTANCE`, uses byte
mode with bounded buffers, and never creates an inheritable handle. The pipe's
security descriptor is a protected SDDL DACL containing only the configured user
SIDs and AppContainer SIDs. A null/default descriptor is not accepted because
Windows' default named-pipe DACL can grant broader access than this broker requires.

The adapter requires an expected peer process ID. After connection it obtains the
client PID with `GetNamedPipeClientProcessId`, opens that process only with
`PROCESS_QUERY_LIMITED_INFORMATION`, opens its token with `TOKEN_QUERY`, and reads
the user SID, AppContainer SID, AppContainer flag, and mandatory integrity level.
Missing, malformed, inaccessible, or non-AppContainer identity information fails
closed before any broker payload is accepted. The peer is then checked by the
existing `BrokerPeerPolicy` ACL, PID, and integrity rules.

The client verifies the server PID with `GetNamedPipeServerProcessId`. Both sides
perform an ephemeral X25519 exchange. The canonical hello records include the
random nonces, public keys, and process IDs; HKDF-SHA-256 derives 32-byte
direction-specific keys from the transcript; and both sides exchange HMAC key
confirmation records. A confirmation or transcript mismatch closes the pipe. The
handshake is therefore bound to the OS-reported expected processes and cannot be
reflected across the broker's two directions.

After the handshake, all data uses the existing bounded `CXBF` frame decoder and
canonical `broker.message.v1` message validator. The server binds every inbound
message to the trusted peer, expected installation principal, and durable job
owner through `authorize_message`. The client never fabricates a server token
identity; it binds responses only to the expected installation and job owner.
Any framing, identity, handshake, direction, or authorization failure closes the
connection and returns only a stable category.

## Lifecycle and failure contract

The adapter is a blocking transport primitive intended to run in a coordinator
worker. `NativeBrokerServer.close()` and `NativeBrokerConnection.close()` are the
shutdown path; no operation is dispatched after a close. The client has a bounded
`WaitNamedPipeW` connection timeout. Read/write failures, broken pipes, peer
disconnects, and handshake failures do not retry on another transport.

Stable native categories include `native_windows_required`, `native_acl_*`,
`native_pipe_*`, `native_peer_*`, `native_handshake_*`, `native_message_*`, and
`native_connection_closed`. OS error numbers, paths, token values, key material,
and payloads are not exposed.

## Explicit non-goals

This ADR does not launch or supervise a process, create an AppContainer or Job
Object, install a signed bundle, copy files, decode images, publish artifacts,
enable a lifecycle provider, or expose a model tool. Artifact copy-in/publication is
covered by the separate [trusted artifact boundary](0001-phase2-artifact-boundary.md);
decoding and lifecycle enablement remain separate gates.

## Verification

`tests/test_phase2_native_broker.py` contains 9 tests covering protected
deterministic SDDL, X25519/HKDF directional key agreement, malformed-handshake and
PID-mismatch rejection, direction-violation close, local pipe-name and expected-PID
validation, native pipe create/close, and OS token identity extraction. The Windows
API choices follow Microsoft's contracts for
[CreateNamedPipe](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createnamedpipea),
[GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid),
[GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation),
[TOKEN_INFORMATION_CLASS](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-token_information_class),
and
[ConvertStringSecurityDescriptorToSecurityDescriptorW](https://learn.microsoft.com/en-us/windows/win32/api/sddl/nf-sddl-convertstringsecuritydescriptortosecuritydescriptorw).
