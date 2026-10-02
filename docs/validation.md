# Validation record

## Version 0.2.0, 2026-10-02

`HERMES_SOURCE=/tmp/hermes-jmap-runtime python -m unittest discover` discovered 120 tests.
113 passed. Seven live tests skipped because their opt-in values were absent.
This includes the actual Hermes loader, registry dispatch, configuration and approval tests.
The Hermes checkout was pinned to `5c08ad68f7ec488057880752f8071cee154a6e60`.

Hermes `doctor_plugin` reported version 0.2.0 with 45 tools and one hook.
Discovery, manifest parsing, import and registration passed.
`python -m compileall -q .` and `git diff --check` also passed.
Python 3.14 ran these tests. The declared minimum remains Python 3.11.

New fixtures cover all-day writes across daylight-saving dates, reminders, default alerts and meeting links.
They also cover private calendar rights, organizer selection, RSVP identity matching and occurrence patch escaping.
Successful writes with failed readback retain the created ID and report the readback failure.

Invitation fixtures cover attachment ownership, byte limits, server parsing, cross-account upload and the write gate.
Import tests cover event selection, duplicate identity detection, recurrence and exclusion of sender reminders.
Malformed parse results fail explicitly. Cancellation snapshots do not mutate events.

Contact fixtures cover address books, bounded search/get, rights, account selection and state guards.
Contact updates retain unrelated card fields. UTF-8 address-book names enforce the protocol byte limit.
Mail fixtures cover threaded reply-all, alias exclusion, Bcc exclusion, original-message forwarding and verified attachments.
Draft edits retain threading headers and body alternatives.
Upload tests cover local regular files, size limits, MIME metadata and uncertain outcomes without retries.
Folder and keyword tests cover rights, state guards and preservation of unrelated memberships and system flags.

Actual Hermes tests load a temporary user installation with writes disabled and enabled.
They dispatch the status and address-book tools through Hermes's registry.
Every consequential tool passes the real approval acceptance, denial and unavailable-gate checks.

Live read tests cover mail, calendars, contacts, identities and diagnostics.
Separate live write tests target explicitly selected test calendars, address books or sending identities.
They create and clean up temporary events, contacts or an unsent draft with an attachment.
The RSVP test has a separate scheduling opt-in and requires an explicit test event and participant identity.
These tests were not run against a live account.

No live compatibility, reminder delivery, mail delivery or remote-machine installation is claimed.
The remaining sections record the earlier version 0.1.0 checks and HTTP diagnosis.

## Version 0.1.0 history

2026-09-30. No live Stalwart credentials were supplied and no live account was
modified. Offline fixtures contain only synthetic identities and test secrets.

## Gates run

- `HERMES_SOURCE=/tmp/hermes-jmap-upstream python -m unittest discover`:
  63 tests discovered, 61 passed, 2 live Stalwart smoke tests skipped by default.
- Hermes `doctor_plugin` from inspected upstream commit
  `5c08ad68f7ec488057880752f8071cee154a6e60`: native discovery, manifest parsing,
  namespaced import and registration pass; 22 tools and one pre_tool_call hook.
- `python -m compileall -q .`: all Python modules compile on Python 3.14.7.
  Python 3.11 is the declared minimum; this environment did not run a 3.11 matrix.

## Requirement evidence

| Requirement | Current evidence |
|---|---|
| Required Hermes reading, two registered-tool plugins, test conventions | design-review.md pins docs/source/examples/test paths; inspected before implementation |
| Current Stalwart mail/calendar capabilities and RFC/draft distinctions | protocol-research.md pins server source and specifications, documents concrete differences |
| Native user plugin, manifest, ctx registration, explicit enablement | plugin.yaml, __init__.py; real PluginManager test proves disabled before enablement and registration after enablement |
| Separate schemas/client/auth/discovery/mail/calendar/models/tests | dedicated modules; Hermes imports remain outside the independent client/operations |
| Required configuration/secrets and endpoint discovery | config.py, session.py; tests for Basic/Bearer, required shape, account capabilities/ambiguity, origin trust and missing config |
| All original read-only mail tools plus attachment retrieval | mail.py, models.py; search mapping, query/get order, missing IDs, batch limits, body clipping, thread pages and private files tested |
| Draft lifecycle and isolated submission | mail_mutations.py; replacement preserves blob/attachment content, create-before-destroy, cleanup failures and submission implicit follow-up response tested |
| Move/read/flag operations | exact emitted JMAP updates tested; keyword patches preserve unrelated keyword state |
| Added single-email permanent delete with approval | explicit destructive schema, existence check, observed state guard, forbidden/stale/missing results tested; real Hermes approval tests cover acceptance/denial/gate failure |
| Calendar discovery/get/query/search | calendar_ops.py; absence of capability refuses without fallback; expanded query uses explicit timezone/local filter bounds |
| Event fields, organizer, floating timezone and recurrence metadata | normalized current draft fields and preserved null timeZone tested; inspected Stalwart fixtures verify current wire property names |
| Availability and conflicts | availability.py; merge/clip/complement/conflicts tested; cancelled/free events omitted, moved recurrence used, incomplete/state-changed reads refused |
| Timezone/DST/all-day behavior | time_utils.py and calendar tests reject fold/gap ambiguity; all-day availability uses server-derived 23-hour DST-day UTC bounds |
| Separate event mutations and scheduling opt-in | calendar_mutations.py; rights, state guards, occurrence scope and scheduling default tested |
| Prompt-injection boundaries and consequential operations | untrusted result envelopes; no content instruction hooks or auto-operations; static approval prompts tested against hostile content |
| Credential/header/log leakage | no plugin logging or raw diagnostic bodies; Config hides credential repr; handler recursive credential-echo redaction tested, including JSON-special-character secrets |
| Attachment and HTTP bounds | declared/streamed response bounds, metadata size, generated private file paths, request bounds, redirect refusal and origin checks tested |
| Malformed JMAP and method/HTTP/network/authorization/rate-limit errors | tag/method/account/get integrity, duplicate/nonfinite JSON, safe method/object errors, HTTP 401/403/429/500, network uncertainty and no automatic retry tested |
| Optional environment-enabled real Stalwart tests | test_integration.py is read-only and skipped unless JMAP_INTEGRATION=1; not run live |

## Verification limits

This is mocked protocol coverage plus actual Hermes plugin-runtime proof. It is
not live proof of mail delivery or compatibility with an installed Stalwart
version. Calendar draft versions share a capability URI and can change wire
fields; tested fixtures follow the inspected current source. Native Hermes
loading and approvals were tested against the pinned checkout, not a user's
existing installed Hermes profile.

Get-all mailbox/calendar lists flag possible server truncation instead of claiming
completeness at the advertised get limit. Availability explicitly describes its
accessible-account/selected-calendar scope; it is conservative event-derived
availability and does not include inaccessible accounts or principal scheduling
preferences. Sending's Email state precheck does not atomically lock submission.
These boundaries are stated in README and tool schemas/results.

This validation record was produced before the initial implementation commit and
push. It does not certify installation into the user's Hermes profile. Local and
Git installation instructions and a real temporary user-plugin installation
test are provided.

## First agent-test HTTP failure

The first live agent test reported generic HTTP errors from list_mailboxes and
list_email with `JMAP_SESSION_URL=https://mail.pr0.tech/.well-known/jmap`.
Unauthenticated read-only endpoint probes confirmed that route returns HTTP 307
with Location `/jmap/session`, and the direct route returns HTTP 200 with JSON
content type. The client deliberately refuses redirects. This explains the
reported failure without a credential probe or any account mutation.

README now uses Stalwart's direct Session path. Errors report numeric HTTP status
and a fixed request stage, and explicitly distinguish refused redirects.
Regression coverage exercises both reported tool handlers for missing HTTP
status/stage, and demonstrates the 307 failure and direct-path success with
synthetic Session/mailbox responses. Authenticated reads on the user's installed
agent still require retesting after the URL change and Hermes restart.
