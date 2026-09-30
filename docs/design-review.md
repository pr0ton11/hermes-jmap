# Hermes JMAP: implementation review

Reviewed 2026-09-30. This is the preimplementation design required by the project brief.
Protocol findings and specification differences are recorded in [protocol-research.md](protocol-research.md).

## Hermes evidence

Inspected upstream Hermes commit `5c08ad68f7ec488057880752f8071cee154a6e60`,
including the three required documentation pages:

- [Build a Plugin](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/website/docs/developer-guide/plugins/index.md): manifest, registration, configuration, dependency preparation, hooks, doctor.
- [User plugin guide](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/website/docs/user-guide/features/plugins.md): user installation and explicit enablement.
- [Adding Tools](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/website/docs/developer-guide/adding-tools.md): JSON schema and handler conventions; this guide explicitly directs personal integrations to plugins.

Inspected two plugins that actually register tools:

- [Spotify registration](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/plugins/spotify/__init__.py) loops over named schemas/handlers, registers a toolset, and gates availability without network calls. Its bundled `backend` activation policy is inappropriate for this user plugin.
- [Google Meet registration](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/plugins/google_meet/__init__.py) similarly separates registration from operations, and registers lifecycle hooks. Both examples use bundled absolute imports; this standalone plugin will use relative imports for Hermes's namespaced directory loader.

Inspected test conventions:

- [Spotify client tests](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/tests/tools/test_spotify_client.py): fake HTTP responses and monkeypatched clients.
- [Meet tests](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/tests/plugins/test_google_meet_plugin.py): isolated `HERMES_HOME`, operation safety, JSON results, no live service by default.
- [Plugin compatibility tests](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/tests/hermes_cli/test_plugin_api_compat.py): real `PluginManager` discovery from a temporary user plugin directory and explicit enabled config.
- [Plugin Doctor tests](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/tests/hermes_cli/test_plugin_dev.py): registration/manifest consistency, no network during registration, state cleanup.

The [actual PluginContext](https://github.com/NousResearch/hermes-agent/blob/5c08ad68f7ec488057880752f8071cee154a6e60/hermes_cli/plugins.py)
accepts `register_tool(name, toolset, schema, handler, ...)`, `register_hook`,
and `get_config`. It does not have a per-tool destructive/read-only registration
flag. Use distinct toolsets and descriptions, plus the documented `pre_tool_call`
approval directive for consequential actions. Do not invent registry parameters.
Handlers accept `(args, **kwargs)` and return JSON strings, including errors.
Registration must perform no session discovery or network requests.

## Concrete layout

```text
plugin.yaml          standalone manifest, all provided tools, required env, settings
__init__.py          register(ctx), handler bindings, approval hook; relative imports
schemas.py           model-facing JSON schemas, read/mutation classification
handlers.py          argument validation, JSON envelopes, sanitized errors
config.py            typed configuration and private authentication construction
session.py           Session validation, capabilities, deterministic account selection
client.py            bounded HTTP/JSON transport, JMAP invocation and blob download
models.py            mail/calendar normalization; untrusted-content envelopes
mail.py              Mailbox, Email, Thread, Identity, EmailSubmission operations
calendar_ops.py      Calendar/CalendarEvent operations and server-expanded occurrences
availability.py      interval clipping, merging, free intervals; no recurrence engine
tests/               mocked transport, protocols, operations, schemas, Hermes loading
docs/                protocol evidence, design, validation status
README.md            installation, secret setup, enabled tools, limitations, testing
pyproject.toml       Python requirement, dependency declaration and test configuration
```

Hermes imports stop at the adapter. The client and operation layers accept typed
configuration and an injectable HTTP transport, so normal tests need no Hermes.
Python 3.11+ supplies zoneinfo and the HTTP/JSON libraries.

## Configuration

Required manifest entries: `JMAP_SESSION_URL`, `JMAP_USERNAME`, `JMAP_SECRET`.
Only the secret entry is masked. These are supplied through Hermes's documented
`.env`/environment mechanism; no credential appears in schema arguments or config
YAML. Optional scoped settings from `ctx.get_config`:

- `auth_type`: basic (default) or bearer; the latter treats JMAP_SECRET as a token.
- `mail_account_id`, `calendar_account_id`: optional explicit selection.
- `timezone`: Europe/Zurich by default.
- `timeout_seconds`: 20, bounded 1–120.
- `max_attachment_bytes`: 10 MiB, bounded by a hard maximum.
- `trusted_origins`: explicit HTTPS origins allowed for advertised cross-origin endpoints.
- `enable_mutations`: false by default; separately enables all mutation tools.

Choose the advertised primary account for each capability, or the sole capable
account. Ambiguous accounts require explicit configuration; never guess from
arbitrary dict order. Verify that an explicitly selected account has the required
account capability. Authentication identity and account ID are different concepts.
Read-only accounts cannot be mutated.

Discover apiUrl/uploadUrl/downloadUrl/eventSourceUrl and accounts from Session.
Preserve endpoint templates, substitute only validated URL-encoded identifiers.
Require HTTPS (allow HTTP solely on loopback for test/development). Reject URL user
info, fragments, redirects, and untrusted origins before attaching Authorization.
Allow explicit trusted origins for servers advertising a separate blob/API host.
Do not use remote content or model-supplied URLs as authenticated endpoints.

## Tool schema proposal

All schemas have an object parameter type, explicitly listed properties,
`additionalProperties: false`, and runtime validation. Optional arguments below
are shown with `?`; all other listed arguments are required. No credentials,
URLs, arbitrary method names or shell commands are accepted from the model.

Common pagination: `limit?` integer 1–100, default 20; `position?` integer >=0,
default 0. Return position, next_position, query_state, optional total and
has_more. A query followed by get preserves query order and reports missing IDs.
Never call a bounded result complete if there are more pages.

| Tool | Parameters | JMAP operation |
|---|---|---|
| jmap_list_mailboxes | none | Mailbox/get |
| jmap_list_email | mailbox_id?, limit?, position? | Email/query + Email/get |
| jmap_search_email | text?, sender?, recipient?, subject?, before?, after?, mailbox_id?, unread?, flagged?, limit?, position? | Email/query filter + Email/get |
| jmap_get_email | email_id, include_body? (false), max_body_chars? | Email/get |
| jmap_get_thread | thread_id, limit?, position? | Thread/get + bounded Email/get |
| jmap_get_attachment | email_id, blob_id | verify membership in Email/get attachments; bounded blob download |
| jmap_create_draft | to, subject, body, cc?, bcc?, identity_id? | Identity/get, drafts Mailbox/get, Email/set create |
| jmap_update_draft | email_id, to?, cc?, bcc?, subject?, body?, if_in_state? | verify $draft, create replacement, then destroy old draft |
| jmap_move_email | email_id, mailbox_id, if_in_state? | Email/set mailboxIds update |
| jmap_mark_read | email_id, read, if_in_state? | Email/set keywords/$seen patch |
| jmap_set_flagged | email_id, flagged, if_in_state? | Email/set keywords/$flagged patch |
| jmap_send_draft | email_id, identity_id, if_in_state? | verify draft, Identity/get, EmailSubmission/set |
| jmap_list_calendars | none | Calendar/get |
| jmap_get_calendar | calendar_id | Calendar/get |
| jmap_list_events | start, end, calendar_ids?, timezone?, limit?, position? | CalendarEvent/query + CalendarEvent/get |
| jmap_search_events | start, end, text?, calendar_ids?, timezone?, limit?, position? | CalendarEvent/query + CalendarEvent/get |
| jmap_get_event | event_id, timezone? | CalendarEvent/get |
| jmap_calendar_availability | start, end, calendar_ids?, timezone?, include_free? | bounded, paged expanded CalendarEvent query/get and interval union |
| jmap_create_event | calendar_id, title, start, duration, timezone?, description?, locations?, participants?, recurrence_rule?, send_scheduling_messages? (false) | CalendarEvent/set create |
| jmap_update_event | event_id, changes, if_in_state?, send_scheduling_messages? (false) | allowlisted CalendarEvent/set patch |
| jmap_delete_event | event_id, if_in_state?, send_scheduling_messages? (false) | CalendarEvent/set destroy |

`to`/`cc`/`bcc` are arrays of structured `{email, name?}` addresses. Event changes
are a typed object with explicit editable fields, never arbitrary patches. Duration
is an RFC duration and start is a JSCalendar local datetime; timezone is an IANA
name or explicit null for floating events. Query boundaries are RFC3339 instants.
Event occurrence IDs retain the server's opaque representation. Updates/deletes
must state whether they affect an occurrence or a series in the result.

Mail tools use the mail capability; submission additionally requires the submission
capability. Calendar tools reject absent calendar capability with a sanitized,
actionable error. Registration advertises stable tool names without networking;
capabilities are checked on invocation. No protocol fallback is implicit.

## Normalized outputs and safety

Return an operation envelope with `data`, pagination when relevant, and
`content_trust: untrusted` on remote-content results. Normalize IDs, mailbox roles,
counts, addresses, subject, dates, keywords, preview and attachment metadata.
Bodies are fetched only on explicit request, clipped, and marked truncated.
Do not load binary attachments in email results or include base64 in ordinary
tool responses. Explicit retrieval writes a random-named, mode-0600 file in a
plugin-owned directory and returns metadata/path; remote filenames never become
paths. Enforce declared and streamed byte limits.

Events retain local start, duration, timeZone (including null), derived end,
calendars, locations, participants, organizer, recurrence rule/overrides,
recurrence ID and status. Distinguish floating/all-day data from zoned instants.
Use server expansion with explicit timezone for occurrence lists. Preserve
recurrence data in get_event. Availability returns busy/free intervals only when
the full bounded range was scanned; report incompleteness/errors instead of
inventing free time. Omit cancelled/free events; retain tentative events as busy.
Respect excluded calendars and occurrences. Clip and merge overlapping busy
intervals. DST fold/gap behavior needs fixture tests and explicit handling.

Remote text is data only: no hooks inject it as instructions, no operation is
triggered from email/calendar content, and no returned invitation is executed.
Schema descriptions make this constraint visible to the model. Wrapping content
is mitigation, not proof that prompt injection is impossible.

Separate toolsets `jmap_mail_read`, `jmap_mail_write`, `jmap_calendar_read`,
`jmap_calendar_write`. Mutation tools are opt-in; send and calendar create/update/
delete return `action: approve` from pre_tool_call using a concise action summary.
The approval hook never copies arbitrary remote descriptions or credentials into
its prompt. Do not treat a model argument such as `confirmed: true` as human
consent. Email deletion is excluded. Drafting never submits mail. Moving, marking
and flagging use patches preserving unrelated state. Mutation set errors and
partial success must be inspected, not treated as successful HTTP responses.

Errors expose safe codes and actionable static messages, never arbitrary HTTP
response bodies, exception strings, endpoint strings, authentication headers or
server-provided descriptions. Bound response sizes and method list counts;
validate response method name/tag/account/list shapes. Do not retry mutations
automatically after ambiguous transport failures; report unknown outcome and
instruct the caller to inspect state before another submission. Return a bounded
Retry-After indication for rate limits instead of sleeping a Hermes worker.

## Dependencies and implementation gates

Prefer stdlib urllib/http/json/zoneinfo and a small direct JMAP client. Calendar
recurrence expansion belongs to the server; do not add a recurrence framework or
silently implement only daily/weekly rules. Dependency comparison and maintenance
evidence are in the protocol note. Tests may use pytest as a development-only
dependency; no lazy dependency installation is needed at runtime.

1. Foundation: manifest, schemas, config, authentication, validated Session,
   bounded HTTP, account/capability selection; mocked failure/success tests.
2. Read-only mail: normalized mailboxes/query/get/thread/attachment metadata;
   pagination and search tests. Prove installation/registration and invocation
   through Hermes's real PluginManager before implementing mutation code.
3. Read-only calendar: normalized calendars/events, explicit Zurich timezone,
   server recurrence expansion, availability/conflict intervals. Test all-day,
   floating, zoned, DST, recurrence overrides, incomplete pages and permissions.
4. Mail mutations: draft create/update then flags/moves and isolated submission;
   test draft checks, submission capability, identity/account matching, set errors,
   state conflicts and approval hook behavior.
5. Calendar mutations: explicit create/update/delete with allowlisted fields,
   rights/state checks and scheduling message opt-in; approval integration tests.
6. Hardening: leakage, hostile remote strings/filenames/URLs, bounded bodies/blobs,
   malformed JSON/JMAP, 401/403/429, timeout, mutation outcome uncertainty;
   run full tests and Hermes Plugin Doctor against the pinned inspected checkout.

Optional live integration tests require explicit test environment variables and
are skipped by default. The default live suite performs reads only. A separate
explicit mutation-test opt-in must restrict operations to objects created by the
test and clean up only those objects. No personal credentials or live-account
data go into fixtures. Report live proof separately from mocked protocol tests.

## Review-time completion state

At the time this review was written, implementation,
Hermes runtime verification and real Stalwart verification are still pending.
The full project remains incomplete until every requested tool, safety property,
test category and user installation path has been checked against current code.
Current implementation and evidence are tracked in [validation.md](validation.md).

Scope update: the user explicitly requested an email delete tool with approval.
Add `jmap_delete_email(email_id, if_in_state?)` to the mail write toolset and
consequential approval hook. It verifies existence and uses the observed Email
state to guard `Email/set destroy` for exactly one ID. The schema and approval
prompt explicitly distinguish permanent destruction from moving to Trash.
Earlier statements excluding email deletion describe the original proposal,
superseded by this explicit request. No live deletion is authorized or performed
as part of developing the tool.

Protocol review correction: Email MIME content and addresses are immutable after
creation. Updating a draft must return a new email ID. Create a replacement first;
only after success destroy the old draft with a state guard. If cleanup fails,
report both IDs and the partial outcome. Never destroy first, silently lose
attachments, or represent a replacement as an in-place edit. Preserve original
mailboxes/keywords and supported body-part references during replacement.
Current calendar draft uses singular `recurrenceRule`; the wire adapter must
follow that draft and verified server behavior rather than older JSCalendar
examples. Availability may use Principal/getAvailability for an unfiltered
principal when advertised and resolvable; its result cannot be narrowed to chosen
calendars. Selected-calendar availability must use expanded event data.
