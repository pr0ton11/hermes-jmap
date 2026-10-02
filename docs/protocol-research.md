# JMAP protocol review

Research date: 2026-09-30. This is preimplementation evidence and design guidance, not a claim of live-server compatibility. No personal server or credentials were used.

## Evidence baseline

Stalwart source inspected at commit `648df2d6f1fa7e1a5ccf005949273179386fe037` (main). Its changelog identifies **0.16.24, 2026-09-27** as the latest released version and has an empty 0.16.25 development heading. Pin integration results to the actual installed version; main is not a release guarantee. [Changelog](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/CHANGELOG.md)

Relevant specifications:

- [RFC 8620](https://www.rfc-editor.org/rfc/rfc8620.html): Session discovery, accounts, capabilities, invocation IDs, errors, state guards, pagination, blob download/upload.
- [RFC 8621](https://www.rfc-editor.org/rfc/rfc8621.html): mailboxes, email, threads, identities, submission, keywords and body parts.
- [RFC 8984](https://www.rfc-editor.org/rfc/rfc8984.html): original JSCalendar, including local time, duration and recurrence semantics.
- [JMAP Calendars draft 29](https://datatracker.ietf.org/doc/html/draft-ietf-jmap-calendars-29): calendar protocol. This remains a draft; do not label it a published Calendar RFC. It references [JSCalendar bis 20](https://datatracker.ietf.org/doc/html/draft-ietf-calext-jscalendarbis-20). The draft revision matters because older calendar examples and libraries use different properties.
- [RFC 9670](https://www.rfc-editor.org/rfc/rfc9670.html): principals and sharing. Calendar availability is an additional capability, not implied by mail support.

## Discovery and mail

Read the configured Session URL with authentication, then retain `apiUrl`, `uploadUrl`, `downloadUrl`, `eventSourceUrl`, `accounts`, `primaryAccounts` and capabilities. These are discovery fields; there are no separate hardcoded account endpoints. Select the primary account per required capability, allow an explicit account override and reject ambiguity. Check both Session and selected account capabilities. Add only required, advertised capability URIs to `using`. [RFC 8620 §2–3](https://www.rfc-editor.org/rfc/rfc8620.html#section-2)

Mail uses `urn:ietf:params:jmap:mail`; sending additionally requires `urn:ietf:params:jmap:submission`. Fetch bounded email properties/body values, normalize body truncation explicitly, expose attachment metadata without retrieving blobs. Search maps to `Email/query` (`text`, `from`, `to`, `subject`, `before`, `after`, `inMailbox`, `notKeyword: "$seen"`, `hasKeyword: "$flagged"`). Follow query IDs with `Email/get`; `Thread/get` supplies IDs for a second bounded get. Query position/limit and get batching must respect Session limits. Preserve missing IDs and per-object errors. [RFC 8621](https://www.rfc-editor.org/rfc/rfc8621.html)

Drafts are created with `Email/set`, a drafts mailbox and `$draft`. Editing body/address properties requires care: many Email fields are immutable after creation, so draft replacement may be necessary; verify the allowed update properties in RFC 8621 §4.6 instead of assuming mutable MIME content. Submission uses `EmailSubmission/set`, a discovered `Identity` and an existing email ID. A successful HTTP response does not prove a successful set. Inspect `notCreated`, `notUpdated`, `notDestroyed` and method errors. Do not retry an uncertain submission automatically. [RFC 8621 §4.6 and §7](https://www.rfc-editor.org/rfc/rfc8621.html#section-4.6)

## Stalwart calendar support and differences

Current source defines `urn:ietf:params:jmap:calendars`, `urn:ietf:params:jmap:calendars:parse`, and `urn:ietf:params:jmap:principals:availability`, with per-account limits including `maxExpandedQueryDuration`. Its dispatcher implements Calendar/get, CalendarEvent/query/get/set and Principal/getAvailability. Capability visibility depends on permissions. [Capability types](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap-proto/src/request/capability.rs), [Session](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/api/session.rs), [dispatcher](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/api/request.rs).

Concrete compatibility points:

1. Stalwart additionally implements `Calendar/query`; the reviewed calendar draft enumerates Calendar/get/changes/set only. Use `Calendar/get` for portable calendar listing. Stalwart's administrative `Calendar` singleton under `urn:stalwart:jmap` is server configuration, not the user's calendar collection. [Query source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/query.rs), [admin Calendar](https://stalw.art/docs/ref/object/calendar/).
2. Expanded event queries require both `after` and `before`. Source defaults the query/get timezone to UTC. Pass the selected timezone explicitly, normally Europe/Zurich. Returned synthetic IDs are opaque and represent occurrences. Preserve `baseEventId`, `recurrenceId`, original `start`, `timeZone`, `duration`, and recurrence metadata alongside derived UTC boundaries. [Query](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/query.rs), [get](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/get.rs).
3. Synthetic occurrence update/delete was added in **0.16.20**. Later releases fixed synthetic identity and null recurrence properties on expanded instances. Earlier reports that synthetic mutations are unsupported must not be generalized to current Stalwart. Test both entire-series and single-occurrence behavior. [Changelog](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/CHANGELOG.md).
4. Expansion overflow is reported as `invalidArguments` by CalendarEvent/query; availability overflow uses `requestTooLarge`. Do not assume all limits use only the calendar draft's named expansion errors. Treat either as incomplete availability, never “free.” [Query source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/query.rs), [availability source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/principal/availability.rs).
5. CalendarEvent/set has `sendSchedulingMessages`, default false. Current code checks permission before sending. Make this an explicit tool argument, default false, and describe invitations/updates/cancellations as possible external effects when true. This behavior is part of the calendar protocol, not a reason to send via SMTP. [Set source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/set.rs).

6. CalendarEvent filter boundaries are local datetimes interpreted using the query `timeZone`; Stalwart's LocalTime parser discards any supplied offset. Convert caller RFC3339 instants to local wall times before sending filters, and reject ambiguous boundaries rather than passing UTC text with a Zurich timeZone. Participant scheduling uses `calendarAddress` and event organizer uses `organizerCalendarAddress` in the current JSCalendar-bis format; older `sendTo`/`replyTo` examples are obsolete. [Parser](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap-proto/src/object/calendar_event.rs), [JSCalendar bis](https://datatracker.ietf.org/doc/html/draft-ietf-calext-jscalendarbis-20).

7. Mailbox/get and Calendar/get with null IDs are capped by current Stalwart's get-object limit. Report potential truncation when reaching that bound. The calendar draft has no portable Calendar/query pagination method. [Mailbox/get](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/mailbox/get.rs), [Calendar/get](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar/get.rs).

## Time and availability design

Prefer server recurrence expansion over a partial client recurrence engine. Keep floating times distinguishable from UTC/zoned times. Use the caller's timezone to interpret floating times; preserve that interpretation in output. An all-day duration must retain calendar-day semantics across DST. A repeated or nonexistent wall time must not silently acquire an arbitrary offset. Explicit UTC start/end from the server can support interval calculations while original local fields remain visible. Test Zurich spring and autumn changes, all-day events, excluded/overridden recurrences and events crossing range boundaries. [JSCalendar RFC](https://www.rfc-editor.org/rfc/rfc8984.html), [CalendarEvent/get source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/get.rs).

For whole-principal availability, prefer advertised `Principal/getAvailability` after resolving the authenticated principal. It applies calendar subscription/availability preferences, free-busy rights, cancellation and participation rules. It does not accept a calendar-ID filter. For requested calendar IDs, derive intervals from fully paginated expanded event results, including availability/status rules explicitly; report the scope. Merge clipped half-open busy intervals and derive their complement only when all pages and times are complete. A failed page, state change, missing event, unsupported timezone or expansion failure must suppress any claim of complete free time. [Availability source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/principal/availability.rs), [calendar draft](https://datatracker.ietf.org/doc/html/draft-ietf-jmap-calendars-29#section-2.2).

## Authentication, safety and dependencies

Stalwart supports Basic and Bearer authentication paths. Plugin configuration should select one explicitly; load its secret from the environment, not tool arguments. Require TLS for remote endpoints, validate discovered endpoint origins before forwarding credentials and reject redirects to untrusted origins. Never echo HTTP bodies, request headers or exception URLs that may contain secrets. These are client safety decisions; server discovery alone is not permission to disclose credentials to another host. [Authentication source](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/common/src/auth/authentication.rs).

Treat all remote strings as untrusted data and label them accordingly. No message may trigger another tool or command. Enforce byte limits before JSON parsing/download completion; attachment retrieval should use a discovered blob URL and safe generated output filename, never an attachment-provided path. Bound requests, body text, pagination and expansion. Mutations should use state guards and expose conflicts instead of silently overwriting newer data. Disable automatic mutation retries on timeout.

The Python candidate **jmapc 0.3.0** was released May 23, 2026 (previous release January 2025), so there is recent maintenance evidence. Its published method list covers mail and submissions, but calendar requires `CustomMethod` plus our own models. It is GPL-3.0, Python >=3.10. This is not a claim of abandonment or calendar parity. [PyPI](https://pypi.org/project/jmapc/), [project](https://github.com/smkent/jmapc).

Recommendation: a small direct JSON/HTTP adapter with injected transport, Python standard-library dataclasses/zoneinfo and no JMAP framework. This keeps calendar draft handling and bounded/secret-safe HTTP behavior under test. Prefer the HTTP client already supplied by Hermes if its plugin dependency rules permit, otherwise use standard-library urllib with explicit redirect rejection. Avoid adding a recurrence dependency when server expansion meets the requirement. This recommendation follows the inspected capability gap, rather than dependency count alone.

## Version 0.2.0 extension evidence

Reviewed on 2026-10-02 against the same pinned Stalwart commit.
The extension uses JMAP capabilities instead of an alternative mail or calendar protocol.
Contacts use [RFC 9610](https://www.rfc-editor.org/rfc/rfc9610.html) and
[JSContact RFC 9553](https://www.rfc-editor.org/rfc/rfc9553.html).
AddressBook/get returns metadata and rights. ContactCard/query/get provides search and bounded retrieval.
ContactCard/set applies field patches and state guards. Contact edits retain fields outside the requested changes.

[Stalwart ContactCard/set](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/contact/set.rs)
provides the server evidence for contact writes.
Account capabilities control address-book creation. Address-book rights control contact writes.
There is no address-book deletion or sharing tool in this version.

[CalendarEvent/set](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/set.rs)
distinguishes private fields, RSVP rights and event ownership.
Reminders use Alert objects with OffsetTrigger values. Default reminders use `useDefaultAlerts`.
Meeting links use `virtualLocations`. ParticipantIdentity/get resolves the caller's scheduling address.
RSVP patches only the matching participant and requests scheduling messages explicitly.

[CalendarEvent/parse](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/crates/jmap/src/calendar_event/parse.rs)
parses uploaded iCalendar blobs into events.
The pinned [Cargo.lock](https://github.com/stalwartlabs/stalwart/blob/648df2d6f1fa7e1a5ccf005949273179386fe037/Cargo.lock)
selects calcard 0.3.14.
Its [conversion source](https://docs.rs/crate/calcard/0.3.14/source/src/jscalendar/import/convert.rs)
copies VCALENDAR METHOD into each converted entry.
This supports explicit REQUEST/PUBLISH selection instead of guessing from email text.
The plugin verifies attachment ownership before parsing.
A blob from another account is copied through bounded download/upload before parsing in the calendar account.
Import selects one REQUEST/PUBLISH snapshot and excludes sender reminders and transport metadata.
An existing UID/recurrence identity returns its existing event ID without overwrite.
Import never sends a scheduling response automatically.

Mail uploads use the discovered `uploadUrl` from RFC 8620.
Reply drafts use Reply-To, message IDs and references from RFC 8621.
Forward drafts attach the verified original MIME blob as `message/rfc822`.
Mailbox metadata and incremental mailbox/keyword patches use RFC 8621.
Custom keywords exclude system flags. Server-side automation remains outside the implementation.

These are source and specification checks. No live Stalwart write result is claimed.
Calendar draft compatibility still follows the pinned server wire format above.
