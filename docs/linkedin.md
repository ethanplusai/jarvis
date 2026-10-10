# Posting to LinkedIn with JARVIS

JARVIS posts to LinkedIn through the `linkedin` connection you declared in
`data/jarvis/connections.json`. He can draft a post, but **nothing goes out
until you approve it on its card**. One post is one card, and that card shows
the exact text.

## How to ask

Ask the way you ask him anything else:

- **By voice** at the orb: *"Post this to LinkedIn: …"*, or *"Draft a LinkedIn
  post about the 18 checks and put it up for me."*
- **Typed** in the conversation panel, in the same words.
- **From your phone**, over Telegram or WhatsApp, in the same words.

If you want to shape the wording first, say so and he reads it back. When the
words are agreed he makes the call once, and the server holds it as a card.

## What the card shows

The card is on the **Business desk** and on every phone line you have set up.
It shows:

- **What and where:** `connector:linkedin · mcp__linkedin__create_post`.
- **The exact text of the post**, line breaks and all. On the desk it appears
  as it will read, with the exact JSON request below it. On Telegram the whole
  post is on the card. On WhatsApp, which allows only 1,024 characters on a
  card, the whole post arrives in a message just before the card.
- **`confirm_post: true`**, which means pressing Approve publishes. A card
  with `confirm_post: false` is a dry run that publishes nothing. JARVIS only
  stages one of those when you ask for a rehearsal.
- **When it lapses.** An unanswered card is held for two minutes while he
  waits, and stays on the desk for 24 hours.
- **A warning when this exact text was already sent.** The card's first lines
  say *"Already sent once (…): …"*, with when it was sent and what LinkedIn
  answered then. Read that before approving it again.

## Approving or rejecting

- **Desk:** tick *I reviewed this request*, then *Allow it through now*. Or
  press *Reject*.
- **Telegram / WhatsApp:** tap **Approve** or **Reject** under the card, or
  reply `approve` / `reject`. If more than one card is waiting, add the card's
  first characters, for example `approve 9a0eacfa`.

Approve releases exactly those bytes, **once**. Approving one wording never
approves an edited one, and a second identical send needs a new card. Reject
sends nothing. Leaving it unanswered also sends nothing: after two minutes
JARVIS tells you he has stopped waiting, and the card stays on the desk. If you
approve it after that, ask him to post it again: the approved card lets exactly
that text through, once.

After it goes, the card records what LinkedIn answered: **Posted**, or a
failure, or an error. Once JARVIS has looked the post up, the card also
carries its own link, `https://www.linkedin.com/feed/update/urn:li:activity:…/`.
Looking up the link is a read, so it needs no second card.

**If a post errors,** it may or may not have gone out. The card says so, and
JARVIS says so too. Check your recent activity before approving it again. On
2026-10-01 a first attempt failed while typing, before Post was pressed. Only
the connector's own trace (`C:\linkedin-mcp\trace-runs`) showed that, so the
retry made the only post. That is why a repeat card now carries the warning.

**"Could not reach LinkedIn; nothing was posted"** means exactly that: a step
before the post itself (the image or video upload) could not connect. Through
the official API, JARVIS first tries each of those steps again, up to three
more times over about twelve seconds, because a request that never connected
never reached LinkedIn. If the card still fails, the cause is usually this
machine's DNS or network, not LinkedIn. On 2026-10-09 the router's DNS failed
for a few minutes. Ask JARVIS for a fresh card once the network is back:
nothing went out, so there is nothing to check first. The request that
publishes is never retried. The backend log names the error under
`jarvis.linkedin`.

If you never received a card on your phone because the line was down,
JARVIS sends it again as soon as the line answers. It is also on the desk.

## If the LinkedIn login has expired

JARVIS never signs in for you. To check whether you are still signed in, ask
him a read, for example *"What does my LinkedIn profile say?"*. If the session
has expired, the connector opens its own LinkedIn sign-in window on your
desktop, and he tells you a login window is open.

1. Sign in to LinkedIn **in that window** yourself, including any code
   LinkedIn sends you.
2. Wait about thirty seconds, then ask him the same thing again.

If no window appears, sign in from a terminal instead. The browser it opens is
yours to use. It saves the session to the profile JARVIS uses:

```bash
"C:/linkedin-mcp/venv/Scripts/linkedin-mcp-posting.exe" --login --user-data-dir C:/linkedin-mcp/profile
```

`--status` in place of `--login` checks the session and exits.

## One browser profile, shared safely

JARVIS's connection uses the browser profile in `C:\linkedin-mcp\profile`. If
two processes need that profile, for example JARVIS and a terminal `--login`,
the connector passes the browser between them through a lease on
`C:\linkedin-mcp\profile.lock`. The lock files are permanent by design, so
seeing them is not a sign that something is stuck. The `linkedin` server in
Claude Code's own `~/.claude.json` is a different program
(`uvx mcp-server-linkedin@latest`). It uses a different profile
(`~/.linkedin-mcp`), so it never competes with JARVIS for this one.

## Limits and the automatic stop

LinkedIn's User Agreement forbids "bots or other unauthorized automated
methods" to post, comment, like or share. JARVIS's original LinkedIn
connection drives your own signed-in browser, so until the official API
route (below) is running, JARVIS keeps to a low rate. The limits are enforced
in code, at the gate and at the desk. They aren't just a convention:

- **at most 1 post a day** per account, and **at least 6 hours** between two
  posts on one account, even across midnight;
- **at most 5 comments a day**, replies included;
- a post or comment that errored still counts, because it may have gone out.

A request over a limit is refused before any card is staged, and the refusal
says when it can go. If you approve a card and the limit fills before it is
sent, it isn't sent; your approval stays good until the limit allows. The
limits are `LINKEDIN_POSTS_PER_DAY`, `LINKEDIN_COMMENTS_PER_DAY` and
`LINKEDIN_MIN_POST_GAP_HOURS` in `.env`. Raise them yourself once the API
route has run cleanly for about a week. JARVIS cannot change them.

**The automatic stop.** If LinkedIn shows a security check, a captcha, an
"unusual activity" notice, a restriction or a failed sign-in, **every
LinkedIn action stops**, reads included. JARVIS tells you out loud and on your
phone. He never tries to get past a check. When you have looked at LinkedIn
yourself, press **Resume LinkedIn** on the Business desk. Only that button
lifts the stop. JARVIS's own tools are refused by it.

## Moving to LinkedIn's official API

The official API posts through LinkedIn's own servers instead of a browser.
Once it is connected, JARVIS uses it for posts and comments on that account.
It works inside the same gate: one card per post, approval spent once, the
post's own link written on the card, and the same limits and stop. You do
every sign-in yourself on LinkedIn's own page. JARVIS never sees your
password, and keeps the token in its private data folder.

**Your profile (self-serve, works the same day):**

1. Open <https://www.linkedin.com/developers/apps> and **Create app**. Name
   it without "LinkedIn" or "In" in the name (for example "Stark Posting"),
   choose your company page, add a logo, accept the terms.
   A page admin then verifies the app from its **Settings** tab.
2. **Products** tab: request **Share on LinkedIn** and **Sign In with
   LinkedIn using OpenID Connect**. Both are granted at once.
3. **Auth** tab: under *Authorized redirect URLs* add exactly
   `https://localhost:8340/api/linkedin/callback`. Copy the **Client ID**
   and **Primary Client Secret**.
4. In JARVIS's `.env`, set `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, and
   `LINKEDIN_MEDIA_ROOTS` to the folder the post images and videos come from
   (for example `C:\dev\linkedin-media`). Restart JARVIS.
5. Business desk → **LinkedIn** → **Connect** beside *Your profile*. Sign in
   on LinkedIn's page and press **Allow**. On the way back your browser warns
   about JARVIS's own certificate on `localhost`; continue to it. The desk
   then shows *connected*, and until when.

**A company page (vetted by LinkedIn; can take days):**

6. Create a **second, separate app** for the same page. LinkedIn requires the
   Community Management API to be the only product on its app. Have a
   **super admin** of the page verify it.
7. **Products** tab: request **Community Management API** (Development
   tier). LinkedIn asks for a business email (not a personal one), the
   company's legal name, registered address, website and privacy policy.
8. Once LinkedIn approves it: add the same redirect URL. In `.env`, set
   `LINKEDIN_ORG_CLIENT_ID`, `LINKEDIN_ORG_CLIENT_SECRET`, and
   `LINKEDIN_ORGANIZATION_ID`, which is the number in your page's admin
   address, `linkedin.com/company/<number>/admin`. Restart JARVIS, then
   **Connect** beside *Company page*.

**Every 60 days:** LinkedIn's tokens last 60 days, and LinkedIn doesn't issue
refresh tokens to apps like this one. The desk shows the date. Press
**Reconnect** before it lapses.

**Media:** a post can carry one image (PNG, JPG or GIF) or one MP4 video
from the media folder. The card holds the file's path and sha256, so the
file you approved is the file that's uploaded. If the file changes after you
approve, the post isn't sent.

## The company page until LinkedIn grants access: hand-post cards

Until the Community Management API is granted, JARVIS can't post to the company
page, and he won't use browser automation for it. A company-page
post is a **hand-post card** instead (desk provider `linkedin_hand`):

1. JARVIS stages the card with the exact page text and, if there is one, its
   image or video from the media folder, bound by sha256. It's one card for
   one post.
2. You approve it, on Telegram or the desk. **Nothing is posted.** JARVIS
   sends you on Telegram, in order:
   - a note saying where to post;
   - the post text as its own message, ready to copy whole;
   - the media file, sent as a document so Telegram doesn't recompress it.
3. You post it on the page yourself.
4. **Reply to JARVIS's first message** with the post's link. That reply is
   your say-so: the link is written on the card ("Posted by hand"), JARVIS
   confirms, and no brain turn is involved. A message that isn't a reply to
   that delivery records nothing.

## Applying for the company page's API access

LinkedIn vets this one. According to its documentation (Community Management
API, 2026-09), it reviews:
- the use case;
- a verified **business** email address;
- a verified organisation and its website domain;
- that the app is verified by the company's LinkedIn Page.

The program is for **registered legal organisations with a commercial use
case**. Nothing in LinkedIn's restricted-use list excludes a company running
its own Page through its own app; "to manage LinkedIn Pages or Profiles via
your application" is the permitted use. The decision and the timing are
LinkedIn's.

**Checklist (you do every step yourself):**

- [ ] A **new** app at <https://www.linkedin.com/developers/apps>, with no
      other products on it. Name it without "LinkedIn" or "In" (for example
      "Stark Page Publisher"). Choose your company page, add the
      logo, and accept the terms.
- [ ] A **super admin** of the page verifies the app (app **Settings** →
      **Verify**, then the admin approves the link LinkedIn sends).
- [ ] **Products** → **Community Management API** → request access
      (Development tier). Have ready:
  - [ ] a **business** email at the company domain. LinkedIn emails a
        verification link; check spam and promotions folders.
  - [ ] the company's **legal name** and **registered address**.
  - [ ] the company's website and privacy policy URLs.
  - [ ] use case: **Page Management**. Paste the description below.
- [ ] Once approved: add the redirect URL
      `https://localhost:8340/api/linkedin/callback`. In `.env`, set
      `LINKEDIN_ORG_CLIENT_ID`, `LINKEDIN_ORG_CLIENT_SECRET`, and
      `LINKEDIN_ORGANIZATION_ID`, which is the number in
      `linkedin.com/company/<number>/admin`. Restart JARVIS, then on the desk
      press **Connect** beside *Company page*.

**Use-case description.** LinkedIn's access form may have no free-text field
for this, so keep it for LinkedIn's follow-up emails:

> [Company legal name] operates the LinkedIn Page "[Page name]". We
> use an internal application to publish the company's own organic posts to
> that Page. Our team drafts each post. The Page's administrator reviews and
> approves every post individually in the application before it is published.
> Only after that approval does the application call the Posts API, with an
> image or a short video uploaded through the Images or Videos API, and it
> records the resulting post URN. Volume is one to two posts a day. The
> application manages only our own Page. It serves no third-party customers,
> stores no member data beyond the URNs of the posts we create, and does not
> use LinkedIn data for advertising, sales, recruiting or any social-feed
> display.

**If LinkedIn says no, or takes too long:** a LinkedIn Marketing Partner
publishing tool (for example Buffer, Hootsuite or Sprout Social) posts to
Pages through the official API under its own approval. You'd connect the page
there, and JARVIS's hand-post card would then deliver into that tool's
queue. That route needs its own build and is your decision.
