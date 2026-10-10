# Business desk

Open **Business desk** in the voice page menu or the Run Control header. You can
also ask JARVIS through the typed composer or voice to prepare a proposal, inspect
business records, or report on a connected account.

The desk is a window, not a modal: the page behind it stays usable. Drag it
by its title bar, minimise it to that bar (or press Enter with the bar
focused), move it with the arrow keys, and it remembers where you left it
and whether it was open. Every approval card says when it was staged, last
changed and — while pending — when it expires, to the second; the briefing
says when it was assembled; every local record says when it was last saved.

## Approval and delivery

Every provider mutation, including a pause, budget edit, asset upload, or phone
call, starts as an immutable proposal. Review its destination and exact request,
tick the authorization checkbox, then click **Approve & send**. The digest binds
approval to the stored provider, operation, account, and payload. Changing a
configured account or owner number invalidates the proposal. Proposals expire
after 24 hours. Rejecting one has no external effect.

JARVIS's MCP tools can propose, read the queue and reports, and manage local
records (see [What JARVIS can read](#what-jarvis-can-read)). There is
no approval tool; the MCP bearer credential is explicitly rejected by the
approval endpoint. The approval UI uses the existing same-origin and framing
protections. This is a local operator boundary, not protection against software
running as your Windows user: such software can read your files and forge HTTP
headers. Keep the service bound to loopback.

**The second door is your phone.** With a Telegram bot or a WhatsApp
number configured ([JARVIS on Telegram](telegram.md), [JARVIS on
WhatsApp](whatsapp.md)), every card is also sent to you as it is staged,
with Approve and Reject buttons. A tap carries the card's id and its
full digest and goes through the same function as the desk's button
(`business_api.decide`), so it decides only the card it was made for, once;
the audit trail records it as `via:telegram` or `via:whatsapp`. Only you
are read, and `TELEGRAM_APPROVALS=0` / `WHATSAPP_APPROVALS=0` keep a line
notify-only.

Approval claims the action in SQLite before sending. Concurrent approval clicks
cannot send it twice. The network request has an 18-second overall timeout and
does not follow redirects or retry mutations. A provider acceptance produces a
**submitted** receipt, not a claim that an ad is serving or a call was answered.
Use the provider report/console to check actual delivery and review status.

A connection failure, cancellation, process crash, unreadable receipt, or server
error may leave an **unknown** outcome. Check the provider console before
creating another proposal; the original may have succeeded. Restart recovers
in-flight actions as unknown rather than replaying them. Restoring a backup also
invalidates its pending/in-flight proposals and its approved-but-unspent connector
calls, since they may have executed after the backup was taken; the next such call
is put to you afresh. Receipts and transitions remain auditable.

Shutdown closes business execution admission, cancels and joins active provider
requests, and persists unknown outcomes before releasing database ownership.

**Deleting finished approvals.** A card that can no longer do anything —
submitted, failed, rejected, unknown, or a proposal or allowance that lapsed
unspent — offers **Delete**; **Clear completed** takes all of them at once.
Both need two clicks (the first arms the button). Delete takes the card off
the desk and nothing else: the row keeps a `deleted` stamp, its audit trail
gains a `deleted` event, and the export still carries it — the ledger is
never edited. A live approval cannot be deleted; reject it, which is
recorded as an answer. The MCP bearer token cannot delete anything, for the
same reason it cannot approve: what the brain asked for is not the brain's
to remove.

## What JARVIS can read

Every tool result the brain receives is capped at 1,500 characters. Two tools
read the desk, and both are **assembled to fit** that cap rather than cut by
it, so nothing disappears off the end of a string (a third, `business_find`,
points at records without reading them — see
[Changing a record by voice](#changing-a-record-by-voice)):

- **`business_status`** is the overview. **Live approval cards come first** —
  pending, or approved and not yet spent, inside their 24-hour window — as
  summaries without payloads: id, state, provider, operation, when it got
  there, when it expires, and how long its payload is. The count of live
  cards is always there, so the cap can never hide that a card exists. Then
  the briefing (money owed and owing per currency, largest first), which
  providers are configured, approval history, and the newest tasks,
  contacts, invoices and expenses, each list taking a turn for the room
  left. Anything that did not fit is counted — `not_listed: N` on a list,
  `…_currencies_not_listed` beside a money map (those totals are on the
  desk; the invoices and expenses behind them page like any list). Ask with
  `kind` (`approval`,
  `task`, `contact`, `invoice` or `expense`) for one list, newest first, and
  pass the page's `next_before` back as `before` for older ones: every card
  and every record's id, version and fields are reachable that way. A
  record's notes are previewed — 60 characters in the overview, 200 in a
  list — with `notes_chars` giving the full length; the Business desk shows
  them whole.
- **`business_action`** shows **one card in full**: id, provider, operation,
  state, created, updated, expires, and the payload that was approved or is
  waiting, as sorted, compact JSON with characters shown as themselves. It
  parses to exactly the stored value, which is what the approval's digest is
  over, so re-issuing the call with those arguments is the approved call.
  Give it the card's id, or its first eight characters; a prefix that
  matches more than one card lists them rather than guessing. It also says
  what the card's state means for the call it holds — an approved connector
  card goes through once when you ask for the same call with exactly that
  payload; a lapsed one, asked for again, becomes a new card; a lapsed
  provider proposal needs a new proposal. A payload too long for one reply
  comes in numbered parts, each saying `part k of n` and how to ask for the
  next; joined in order, they are the exact payload. Nothing is truncated
  silently.

Only the card leaves: no digest, no receipt, no audit trail, no other row,
and a card taken off the desk is not read back. A secret from this machine —
the value of an environment variable whose name says it holds a credential
(`TOKEN`, `SECRET`, `PASSWORD`, `PASSCODE`, `KEY`, `AUTH`, `CREDENTIAL`, or a
`PIN`), or JARVIS's tool token — is shown as `[redacted]` wherever it sits in
the card: in text, in a key, or as a number (a PIN stored as `1234567` is
still the PIN `01234567`). Values shorter than 8 characters are only treated
as secrets under a password, passcode or PIN name, down to 4 — shorter than
that, or a flag such as `true`, would match ordinary text. `business_status`
redacts the same way in what it shows of cards and records. When a card is
redacted the reply says the view is **not** the exact stored text, and stops
promising that re-sending it is the approved call; the same happens when the
payload contains the untrusted-block delimiter, which is shown altered.
Characters the brain cannot see — a no-break or zero-width space, a joiner,
a variation selector, a combining accent — are shown as their `\uXXXX`
escapes, so a re-sent call keeps them.

A payload is text the brain composed out of whatever it had read, so it
arrives inside an untrusted block. Reading either tool marks the rest of
that **turn**: JARVIS will not act on anything in it — not even the
`business_propose` a lapsed card says it needs — until you ask again. And it
marks the brain's **whole context**: until that context rotates, JARVIS will
not write business records, project-document approvals (`approve_document`)
or memories, and says so when it refuses. Sending a connector call you have
already approved is not affected; the gate checks the approval's digest, not
those marks. Neither tool changes anything, so both answer on any turn.

This exists because of a measured failure: on 2026-09-26 an approved
Paperclip comment could not be sent, because the only view of the card was
the whole ledger in one string, cut off by the cap half-way through that
very card.

**Cards across a brain rotation.** JARVIS's brain is replaced by a fresh
generation when its context fills (see "Context rotation" in the README), and
on 2026-09-25 a generation that had staged that same comment was replaced two
minutes later; its successor had only a model's note to go on. Every new
generation is now told which cards are on the desk — approved and not yet
sent first, then any being sent, then those waiting for you, then the last
day's finished ones — straight from the ledger, never with the request text.
A connector card the gate has let through is `submitted` in the ledger; the
brain is told it was released, not sent, since nothing records whether your
service then did it. It reads any card's exact request with `business_action`.

## Changing a record by voice

Say what you want changed — "mark the Acme invoice paid", "the dentist task
is done", "note on the Globex contact that they called back" — and JARVIS
does it in two steps:

1. **`business_find`** takes your words for the record and matches them
   here, in the server, against every record's title and contact: each
   word has to begin a word in one or the other, ignoring case and accents,
   so "acme retainer" finds *Retainer — ACME Corp* and "art" does not find
   *Stuart*. It can be narrowed to one kind and one status. What comes back
   for each match is a **handle** — id, version, kind, status, due date,
   amount, currency, and when it was last saved — the most recently created
   first, as many as fit, with the rest counted. Never the title, the notes
   or the contact.
2. **`business_record`** updates from that handle, naming only what
   changes: `status: paid`, a new `due`, or `append_notes`. Everything it
   leaves out — the title and status included — keeps its stored value,
   and the reply is the record's new handle, so a second change can follow
   without looking it up again.

**Why a separate tool.** `business_record` puts something on record for
good, so it is gated on the brain's whole context (`DURABLE_WRITERS` in
`server.py`): once that context has read anything JARVIS did not write, no
record is written until it rotates. `business_status` is one of those
reads — it shows what records *say*, and a record's notes can hold text
pasted from anywhere, a client's email included — and until this change it
was also the only place an update's id and version could come from. So
every update followed a read that blocked it, and after the rotation the
refusal asked for, the same read came first again: JARVIS could create a
record and never change one. Found by the adversarial review of the
`business_action` change, 2026-09-26.

`business_find` does not mark the context, because there is nothing in its
reply anybody typed: every value is re-checked against the closed set or
exact shape a record admits — a status must be one its kind can have, a date
a real date, a currency three capital letters, an amount a whole number in
range — and left out if it fails. A record too damaged to read safely —
text that is not UTF-8, a body that is not a JSON object, as a hand-edited
database or an old backup can hold — or one whose id, version or kind
fails, is counted as `unreadable` and never listed, matched or quoted.
`business_record` replies with the same handle and nothing more; it used to
send back the whole saved record, which would have put a record's stored
notes in front of JARVIS, unmarked, on every update. Neither tool ever fails
in the words of an error: a rejected update names the field and what it
must be, not the value, and anything unexpected — a locked database, a
damaged row — is reported as the ledger being unavailable. The database's
own errors can quote the row they choked on; that is how the review of this
change got a planted instruction through, and why a reading tool that
*fails* now marks the context just as one that answers does.

**What still stops a write.** The gate is unchanged. If JARVIS has read
`business_status`, an approval card, a web page, a file or anything else it
did not write in this context, it will not write a record, and says so;
**say "start fresh"** and ask again, and the fresh context finds the record
with `business_find` and writes it. An instruction planted in a record's
notes or title reaches JARVIS only through `business_status`, and reading
that blocks every record write that follows — the test suite plants one and
checks that the ledger does not change.

An update carries the version `business_find` gave. If the record changed
in between — you edited it on the desk — the update is refused and JARVIS
finds it again, so an older view never overwrites a newer change. Notes that
begin with a preview JARVIS was shown by `business_status` (sent back as they
were, or with more added) are refused rather than written over the full
notes, and so is a title or contact sent back in the clipped form a page
showed; `append_notes` adds to the stored notes without JARVIS needing to see
them. The desk always saves the whole record.

## Connect accounts

Set credentials in the server's local environment or its existing `.env` file,
then restart. Never paste credentials into proposals, conversation, source code,
or the request-details editor. The connection panel shows missing variable names
without exposing values. “Configured” does not claim that authentication works;
**Check connection & report** performs a real read-only request.

| Provider | Required configuration |
|---|---|
| Google Ads | `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN` |
| Meta Ads | `META_AD_ACCOUNT_ID`, `META_ACCESS_TOKEN`, `META_API_VERSION` (an explicitly selected supported `vNN.0`) |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `JARVIS_OWNER_PHONE` |
| ChatGPT Ads | `OPENAI_ADS_API_KEY` issued in Ads Manager, separate from an OpenAI model API key |

Google uses v25 by default; change `GOOGLE_ADS_API_VERSION` only after testing an
upgrade. `GOOGLE_ADS_LOGIN_CUSTOMER_ID` is optional for manager accounts. Customer
IDs accept hyphens, which are removed before calling the API. Google access tokens
are refreshed using the configured OAuth client and refresh token. Meta requires
a token with the account's appropriate marketing permissions. Account access,
developer approval, billing setup, ad-policy review, and provider quotas remain
provider requirements.

These provider credentials and the owner phone number are removed from the
environment of Claude brain/run children. Provider error bodies are not logged
or returned; status codes and a console-check instruction are returned instead.
Response paging links that may contain tokens are removed before display/storage.

## Advertising

The request editor accepts native provider fields so creative, bidding, targeting,
currency units, and dates are explicit rather than guessed. The included examples
create **paused** campaigns. A campaign alone is not a complete ad: add its ad
groups/ad sets, targeting, creative, and ads, inspect review results, then propose
activation. JARVIS can prepare those requests through the same approval queue.

| Provider | Implemented writes | Read report |
|---|---|---|
| Google | `mutate`, with 1–50 `mutateOperations` for budgets, campaigns, campaign criteria, ad groups, ad-group criteria, and ads; create/update/remove; atomic partial-failure-disabled request | Up to 500 campaigns, last 30 days' impressions/clicks/cost, currency and daily budget |
| Meta | `campaigns`, `adsets`, `adcreatives`, `ads`; append `/OBJECT_ID` to update | First 100 campaigns with last-30-day insight summaries and configured budgets |
| ChatGPT | `campaigns`, `ad_groups`, `ads`, `upload`; append `/OBJECT_ID` to update campaign/group/ad | First 100 campaigns |

Google update operations need the provider's `updateMask`. Meta updates use POST;
ChatGPT updates also use POST, as specified by its Advertiser API. Multi-step Meta
and ChatGPT campaign creation requires separate approved requests; each receipt
contains IDs needed for the next step. If one step fails, existing objects remain
visible in the provider console. Nothing is silently rolled back or activated.

For pause/resume, use Google campaign updates with status `PAUSED`/`ENABLED`, Meta
status `PAUSED`/`ACTIVE`, or ChatGPT status `paused`/`active`. Activation and budget
changes require the same explicit approval as creation. Review existing budgets
before activating an existing campaign. Provider budget rules can permit daily
overspend: JARVIS does **not** claim that a daily budget is a guaranteed total
spend ceiling. Configure appropriate lifetime/account limits with the provider.
There is no unattended spending authority or background campaign optimizer.

Recent reports are bounded snapshots, not a complete advertising warehouse. Use
the provider console for full pagination, attribution, billing, and delivery
investigation. The native payload path supports provider-specific creative and
targeting fields; provider-side validation is authoritative.

## Calls to you

The `twilio` / `call` action accepts `message` (1–2,000 characters) and optional
`time_limit` (10–300 seconds; default 120). It speaks escaped text with TwiML and
hangs up. Ring timeout is 30 seconds. Destination is always `JARVIS_OWNER_PHONE`;
the model/payload cannot substitute another recipient. Both phone numbers must
be in E.164 format. The caller number must be usable by your Twilio account;
trial accounts may require destination verification.

These are spoken notifications, not a bidirectional phone assistant. They do not
record audio or require an exposed webhook. A `cancel` action accepts `call_sid`.
The call report lists recent calls to the configured owner, including provider
status. Calls require approval and incur normal Twilio charges. No actual calls
are made by the test suite.

Cancellation first verifies the call's destination against the configured owner.
Meta object updates likewise verify the object's ad-account ownership before
submitting an edit.

## Local business records

Tasks track open/done status and due dates. Contacts track lead, qualified, won,
and lost stages, notes and follow-up dates. Invoices track draft/sent/paid/void
status; expenses track open/paid/void status. Monetary amounts are **integer minor
units** and carry an explicit currency; no exchange-rate assumptions are made.
The briefing counts overdue work/leads and totals outstanding receivables and
expenses separately per currency.

Updates carry a record version: a stale browser cannot overwrite a newer change.
Lists use stable cursors for older history. Invoice records are an operational
ledger, not emailed invoices, card charging, tax calculation, or bank syncing.
Changing a status records your statement; it does not verify a payment. No
third-party communications are sent by local-record actions.

## Data and verification

Records, exact proposed payloads, phone destinations/messages and provider receipts
are private business data in `jarvis.db`. They are included in verified backups
and restore. **Export business records & audit** streams JSONL without loading
the full history into memory. Business history is not auto-deleted, and run
retention does not remove it. Exports/backups are unencrypted; protect them as you
would credentials and customer information. Environment credentials are not
stored in the ledger. Disk use grows with retained records and receipts.

Offline tests use HTTP transports and private databases, including concurrent
approvals, ambiguous failures, interrupted execution, restore replay prevention,
record conflicts, secret redaction, owner-destination enforcement, XML escaping,
and browser approval/record-edit flows. Live provider authentication and delivery
require configured accounts and an explicitly approved test action.

## Provider references

Reviewed 2026-09-21:

- [Google REST examples](https://developers.google.com/google-ads/api/rest/examples)
- [Google campaign creation](https://developers.google.com/google-ads/api/docs/campaigns/create-campaigns)
- [Meta official SDK endpoint definitions](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py)
- [Twilio Call resource](https://www.twilio.com/docs/voice/api/call-resource)
- [ChatGPT Ads quickstart](https://developers.openai.com/ads/api-quickstart)
- [ChatGPT campaign updates](https://developers.openai.com/ads/api-reference/campaigns)
