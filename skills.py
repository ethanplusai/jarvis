"""
JARVIS Skills System — an installable catalog of agent capabilities.

A "skill" is a curated capability definition (inspired by Claude Agent Skills /
SKILL.md): a name, the situations it applies to, and concrete instructions the
agent loads into context when the skill is enabled. This lets JARVIS perform a
wide range of small-business tasks without bespoke code for each one.

Hybrid model:
- Most skills are *prompt-based*: enabling them injects their instructions into
  the system prompt so JARVIS knows how to do the task well.
- A few flagship skills are *executable*: they have a real Python handler that
  produces an artifact (an invoice, an agenda, an SOP) saved under data/artifacts/.

Everything is stored in SQLite so enabled skills persist across restarts.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("jarvis.skills")

DB_PATH = Path(__file__).parent / "data" / "jarvis.db"
ARTIFACTS_DIR = Path(__file__).parent / "data" / "artifacts"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Skill catalog — ~120 capabilities across 16 categories.
# Each entry: (slug, name, category, when_to_use, instructions)
# A handful are marked executable via EXECUTABLE_HANDLERS below.
# ---------------------------------------------------------------------------

# fmt: off
_CATALOG: list[tuple[str, str, str, str, str]] = [
    # --- Sales & CRM ---
    ("lead-qualification", "Lead Qualification", "Sales & CRM", "qualifying an inbound lead or prospect", "Score the lead against BANT (Budget, Authority, Need, Timeline). Ask only the missing pieces, then give a hot/warm/cold verdict and the recommended next step."),
    ("cold-email-outreach", "Cold Email Outreach", "Sales & CRM", "writing a first-touch cold email to a prospect", "Open with a specific, researched hook about them — never 'I hope this finds you well'. One value point, one soft call to action, under 120 words."),
    ("sales-followup", "Sales Follow-up", "Sales & CRM", "following up after no reply or a meeting", "Reference the prior touch, add one new piece of value, and propose a concrete next step with a date. Keep it short and easy to say yes to."),
    ("proposal-writing", "Proposal Writing", "Sales & CRM", "drafting a sales proposal or statement of work", "Lead with the client's problem in their words, then scope, deliverables, timeline, and price. Tie every line item to an outcome they care about."),
    ("quote-generation", "Quote Generation", "Sales & CRM", "producing a price quote", "List line items with quantity, unit price, and total. Note assumptions, validity window, and payment terms. Offer a good/better/best option when sensible."),
    ("crm-update", "CRM Note Logging", "Sales & CRM", "logging contact notes or deal updates", "Capture who, what was discussed, sentiment, next action, and owner. Write it so a colleague could pick up the deal cold."),
    ("discovery-call-prep", "Discovery Call Prep", "Sales & CRM", "preparing for a discovery or sales call", "Research the company, draft 5-7 open discovery questions, anticipate two objections, and define the single outcome that makes the call a success."),
    ("objection-handling", "Objection Handling", "Sales & CRM", "responding to a sales objection", "Acknowledge the concern, reframe with evidence, and redirect to value. Never argue — isolate the real objection first."),
    ("pipeline-review", "Pipeline Review", "Sales & CRM", "reviewing or forecasting the sales pipeline", "Summarise deals by stage and value, flag stalled deals, weight the forecast by stage probability, and recommend where to focus this week."),

    # --- Marketing ---
    ("marketing-strategy", "Marketing Strategy", "Marketing", "building a marketing plan or strategy", "Define audience, positioning, channels, budget split, and 3 measurable goals. Recommend the two highest-leverage channels for a small budget."),
    ("seo-optimization", "SEO Optimization", "Marketing", "optimising content for search", "Cover title tag, meta description, H1, target keyword density, internal links, and intent match. Flag thin or keyword-stuffed sections."),
    ("elevator-pitch", "Elevator Pitch", "Marketing", "writing an elevator pitch or one-liner", "Compress to: who it's for, the pain, what it does, and why it's different — under 30 seconds spoken. Offer a 10-second and a 30-second version."),
    ("keyword-research", "Keyword Research", "Marketing", "researching keywords for content or ads", "Group keywords by intent (informational/commercial/transactional), note rough difficulty, and recommend a small set of winnable, high-intent terms."),
    ("ad-copywriting", "Ad Copywriting", "Marketing", "writing paid ad copy for Google or Meta", "Match copy to platform limits, lead with the benefit, include one clear CTA, and provide 3 variants to test. State the angle of each."),
    ("landing-page-copy", "Landing Page Copy", "Marketing", "writing landing page copy", "Hero with a sharp value proposition, 3 benefit blocks, social proof, objection handling, and a single repeated CTA. Write for scanners."),
    ("email-newsletter", "Email Newsletter", "Marketing", "drafting an email newsletter or campaign", "Subject line plus preview text, one core idea, skimmable sections, and one primary CTA. Provide two subject-line options."),
    ("brand-voice", "Brand Voice Guide", "Marketing", "defining brand voice and tone", "Capture personality traits, do/don't word lists, tone-by-context examples, and a one-paragraph voice summary anyone can apply."),
    ("competitor-analysis", "Competitor Analysis", "Marketing", "analysing competitors", "Compare positioning, pricing, channels, and messaging in a table. End with gaps and the differentiation opportunity for the user."),
    ("campaign-planning", "Campaign Planning", "Marketing", "planning a marketing campaign", "Define goal, audience, offer, channels, timeline, assets needed, and success metric. Lay it out as a week-by-week plan."),
    ("press-release", "Press Release", "Marketing", "writing a press release", "Use the inverted-pyramid: headline, dateline, strong lede answering who/what/why, supporting quote, boilerplate, and contact. Keep it factual."),

    # --- Content & Social ---
    ("blog-writing", "Blog Writing", "Content & Social", "writing a blog post or article", "Hook, clear structure with subheads, scannable paragraphs, one takeaway per section, and a CTA. Match the requested tone and length."),
    ("social-media-calendar", "Social Content Calendar", "Content & Social", "planning a social media calendar", "Plan posts across the week by theme and format, balance promotional vs value content (roughly 1:4), and note the best posting cadence per platform."),
    ("social-post-writing", "Social Post Writing", "Content & Social", "writing individual social posts", "Hook in the first line, one idea, platform-appropriate length and tone, and a clear CTA or question. Offer a couple of variations."),
    ("linkedin-content", "LinkedIn Content", "Content & Social", "writing LinkedIn thought-leadership posts", "Open with a contrarian or personal hook, short punchy lines, one insight, and an engagement prompt. No corporate fluff or hashtag spam."),
    ("video-script", "Video Script", "Content & Social", "scripting a short-form or YouTube video", "Hook in the first 3 seconds, beat-by-beat structure with on-screen notes, and a payoff plus CTA. Mark pacing for short-form."),
    ("content-repurposing", "Content Repurposing", "Content & Social", "repurposing one piece of content across channels", "Take the source asset and adapt it into a thread, a LinkedIn post, a newsletter blurb, and a short-video script — keeping the core message, changing the format."),
    ("hashtag-strategy", "Hashtag Strategy", "Content & Social", "choosing hashtags", "Mix broad, niche, and branded tags, keep counts platform-appropriate, and avoid banned or overly generic tags. Explain the reasoning briefly."),
    ("caption-writing", "Caption Writing", "Content & Social", "writing captions for images or posts", "Match the visual, hook fast, keep it on-brand, and add a light CTA. Provide a short and a longer option."),
    ("newsletter-curation", "Newsletter Curation", "Content & Social", "curating links and content for a newsletter", "Select a tight set of items, write a one-line 'why it matters' for each, and order them by reader value. Add a short intro."),

    # --- Finance & Accounting ---
    ("invoice-creation", "Invoice Creation", "Finance & Accounting", "creating an invoice for a client", "Generate a clean invoice with seller/client details, numbered line items, subtotal, tax, total, due date, and payment terms. Use the executable generator to save a file."),
    ("expense-tracking", "Expense Tracking", "Finance & Accounting", "categorising or tracking expenses", "Categorise each expense (standard accounting buckets), flag anything unusual or non-deductible, and total by category."),
    ("budget-planning", "Budget Planning", "Finance & Accounting", "building a budget", "Lay out income, fixed costs, variable costs, and a buffer. Show monthly and annual views and flag the biggest controllable line items."),
    ("cashflow-forecast", "Cash Flow Forecast", "Finance & Accounting", "forecasting cash flow", "Project inflows and outflows by week or month, compute the running balance, and flag any periods that go negative with options to fix them."),
    ("financial-reporting", "Financial Reporting", "Finance & Accounting", "summarising monthly financials", "Summarise revenue, costs, margin, and the top movements vs last period. Lead with the numbers, then one paragraph of plain-English context."),
    ("pricing-strategy", "Pricing Strategy", "Finance & Accounting", "setting or reviewing pricing", "Analyse cost-plus, value, and competitor anchors. Recommend a price with the reasoning and a tiering structure if it fits."),
    ("bookkeeping-review", "Bookkeeping Review", "Finance & Accounting", "reviewing bookkeeping entries", "Check for miscategorised, duplicate, or missing entries, and reconcile totals. Produce a short list of items needing attention."),
    ("tax-prep-checklist", "Tax Prep Checklist", "Finance & Accounting", "organising for tax season", "Produce a document checklist by category, note common small-business deductions to confirm, and flag deadlines. Recommend confirming with an accountant."),
    ("payroll-summary", "Payroll Summary", "Finance & Accounting", "preparing a payroll summary", "Summarise gross, deductions, and net per person and in total, and note pay date and any anomalies. Keep figures clearly labelled."),

    # --- HR & Recruiting ---
    ("job-description", "Job Description", "HR & Recruiting", "writing a job description", "Cover role summary, responsibilities, must-have vs nice-to-have requirements, and what makes the role attractive. Keep it inclusive and jargon-light."),
    ("resume-screening", "Resume Screening", "HR & Recruiting", "screening resumes against a role", "Score each candidate against the must-haves, note strengths and gaps, and give an advance/hold/reject recommendation with a one-line reason."),
    ("interview-questions", "Interview Questions", "HR & Recruiting", "preparing interview questions", "Generate role-specific behavioural and technical questions mapped to the competencies being assessed, with what a strong answer looks like."),
    ("candidate-evaluation", "Candidate Evaluation", "HR & Recruiting", "evaluating a candidate after an interview", "Score against a consistent rubric, separate evidence from impression, and give a clear hire/no-hire with the deciding factors."),
    ("offer-letter", "Offer Letter", "HR & Recruiting", "drafting an offer letter", "Include role, start date, compensation, key terms, and a warm tone. Mark anything that should be reviewed by legal or the user."),
    ("onboarding-checklist", "Onboarding Checklist", "HR & Recruiting", "onboarding a new hire", "Produce a day-1 / week-1 / month-1 checklist covering accounts, equipment, intros, training, and first goals. Assign an owner to each item."),
    ("employee-handbook", "Employee Handbook", "HR & Recruiting", "drafting handbook policies", "Draft clear policy sections in plain language. Note where local law or legal review is required rather than inventing specifics."),
    ("performance-review", "Performance Review", "HR & Recruiting", "writing a performance review", "Structure around goals, strengths, growth areas, and specific examples. Keep feedback actionable and balanced; suggest next-period goals."),
    ("pto-policy", "Leave & PTO Policy", "HR & Recruiting", "drafting a leave or PTO policy", "Cover accrual, requests, approval, carryover, and edge cases in plain language. Flag where jurisdiction-specific rules apply."),

    # --- Customer Support ---
    ("support-response", "Support Response", "Customer Support", "drafting a customer support reply", "Acknowledge, answer the actual question, give clear steps, and set expectations. Match the customer's tone; be warm and concise."),
    ("faq-generation", "FAQ Generation", "Customer Support", "building an FAQ", "Cluster real questions, write plain-language answers, and order by frequency. Keep each answer self-contained."),
    ("complaint-resolution", "Complaint Resolution", "Customer Support", "handling a complaint or escalation", "Lead with empathy, own the problem, offer a concrete remedy, and confirm the fix. De-escalate before solving."),
    ("canned-responses", "Canned Responses", "Customer Support", "building reusable macros or templates", "Write reusable templates with clear placeholders for the common scenarios, each editable and on-brand."),
    ("refund-handling", "Refund Handling", "Customer Support", "processing a refund or return", "Confirm eligibility against policy, explain the outcome kindly, and lay out the steps and timing. Offer an alternative when a refund isn't possible."),
    ("csat-survey", "CSAT Survey Design", "Customer Support", "designing a satisfaction survey", "Keep it short, lead with the core satisfaction question, add one open follow-up, and avoid leading wording."),
    ("knowledge-base-article", "Knowledge Base Article", "Customer Support", "writing a help/KB article", "Task-focused title, prerequisites, numbered steps, screenshots-to-add markers, and a troubleshooting section. Write for a stressed reader."),
    ("ticket-triage", "Ticket Triage", "Customer Support", "triaging and prioritising tickets", "Classify by urgency and impact, assign a priority, and route or template a first response. Flag anything needing escalation."),

    # --- Operations ---
    ("sop-writing", "SOP Writing", "Operations", "writing a standard operating procedure", "Produce a titled SOP with purpose, scope, roles, numbered steps, and a quality check. Use the executable generator to save a file."),
    ("process-mapping", "Process Mapping", "Operations", "mapping or optimising a process", "Lay out the current steps, mark hand-offs and bottlenecks, and propose a streamlined version with the time/effort saved."),
    ("inventory-tracking", "Inventory Tracking", "Operations", "tracking or managing inventory", "Track quantity, reorder point, and lead time per item; flag low stock and suggest reorder quantities."),
    ("vendor-management", "Vendor Management", "Operations", "managing vendors or suppliers", "Track terms, reliability, and cost per vendor; compare options and recommend renegotiation or switching where it pays off."),
    ("meeting-agenda", "Meeting Agenda", "Operations", "preparing a meeting agenda", "Define the objective, timed topics with owners, pre-reads, and desired decisions. Use the executable generator to save a file."),
    ("meeting-minutes", "Meeting Minutes", "Operations", "capturing meeting minutes", "Record decisions, action items with owners and dates, and open questions — not a transcript. Lead with the action items."),
    ("project-kickoff", "Project Kickoff", "Operations", "kicking off a project", "Define goal, scope, roles, milestones, risks, and the first three actions. Capture what success looks like in one sentence."),
    ("risk-assessment", "Risk Assessment", "Operations", "assessing operational risk", "List risks with likelihood and impact, score them, and pair each high risk with a concrete mitigation and owner."),
    ("checklist-builder", "Checklist Builder", "Operations", "turning a task or routine into a reusable checklist", "Break the job into verifiable steps in execution order, keep each item one action, and save it with the executable generator so it can be reused."),

    # --- Admin & Scheduling ---
    ("calendar-scheduling", "Calendar Scheduling", "Admin & Scheduling", "scheduling or optimising the calendar", "Propose time blocks that protect focus time, batch similar work, and respect existing commitments. Surface conflicts before booking."),
    ("email-triage", "Email Triage", "Admin & Scheduling", "triaging and prioritising the inbox", "Sort into act-now / delegate / defer / archive, draft quick replies for the easy ones, and surface anything time-sensitive first."),
    ("travel-planning", "Travel Planning", "Admin & Scheduling", "planning business travel", "Build an itinerary with flights, lodging, transit, and buffers around meetings. Note costs and anything needing booking confirmation."),
    ("document-formatting", "Document Formatting", "Admin & Scheduling", "formatting or cleaning up a document", "Apply consistent headings, spacing, and styling; fix structure and tighten wording without changing meaning."),
    ("data-entry", "Structured Data Entry", "Admin & Scheduling", "entering or structuring data", "Convert the source into clean, consistently formatted rows/fields, validate types, and flag anything ambiguous rather than guessing."),
    ("appointment-reminders", "Appointment Reminders", "Admin & Scheduling", "setting up appointment reminders", "Draft reminder messages with the key details and timing, and recommend a reminder cadence that reduces no-shows."),
    ("todo-prioritization", "To-do Prioritization", "Admin & Scheduling", "prioritising a to-do list", "Rank by impact and urgency, identify the one thing that matters most today, and suggest what to drop or defer."),
    ("unit-converter", "Unit Converter", "Admin & Scheduling", "converting units of length, weight, temperature, or data", "Convert between units precisely using the executable converter and state the result plainly with sensible rounding."),
    ("timezone-converter", "Timezone Converter", "Admin & Scheduling", "converting a time between timezones", "Use the executable converter for exact, DST-aware results; state both local times and the day shift if any."),
    ("date-calculator", "Date Calculator", "Admin & Scheduling", "counting days between dates or adding days to a date", "Use the executable calculator for exact day math; report the count, the weekdays, and the weekday-only estimate when it matters."),

    # --- Legal & Compliance ---
    ("contract-review", "Contract Review", "Legal & Compliance", "reviewing a contract", "Summarise key terms, flag risky or unusual clauses, and list questions to raise. Always recommend a qualified lawyer for anything material."),
    ("nda-drafting", "NDA Drafting", "Legal & Compliance", "drafting a non-disclosure agreement", "Produce a clear mutual or one-way NDA covering definition, obligations, term, and exclusions. Mark it as a starting point for legal review."),
    ("terms-of-service", "Terms & Privacy Basics", "Legal & Compliance", "drafting basic terms or a privacy notice", "Draft plain-language sections for the common cases and clearly mark where jurisdiction-specific legal review is required."),
    ("gdpr-checklist", "Data Privacy Checklist", "Legal & Compliance", "checking data-privacy compliance", "Produce a practical GDPR/CCPA-style checklist covering lawful basis, consent, data inventory, retention, and subject requests."),
    ("policy-drafting", "Business Policy Drafting", "Legal & Compliance", "drafting an internal business policy", "Write a clear policy with purpose, scope, rules, and enforcement in plain language. Note where legal or HR review applies."),
    ("compliance-audit", "Compliance Self-Audit", "Legal & Compliance", "running a compliance self-audit", "Walk the relevant requirements as a checklist, mark pass/gap/unknown, and prioritise the gaps by risk."),

    # --- Data & Analytics ---
    ("data-cleaning", "Data Cleaning", "Data & Analytics", "cleaning or normalising a dataset", "Identify duplicates, missing values, and inconsistent formats; propose fixes and produce the cleaned structure. Never silently drop data."),
    ("spreadsheet-formulas", "Spreadsheet Formulas", "Data & Analytics", "building spreadsheet formulas", "Write the exact formula for Excel/Google Sheets, explain each part, and note edge cases. Prefer robust functions over fragile ones."),
    ("data-visualization", "Data Visualization", "Data & Analytics", "choosing or describing charts", "Recommend the chart type that fits the question, specify axes and series, and note what insight it should reveal."),
    ("kpi-dashboard", "KPI Dashboard", "Data & Analytics", "defining KPIs or a dashboard", "Pick a small set of KPIs tied to goals, define each metric precisely with its formula, and lay out a sensible dashboard order."),
    ("report-generation", "Report Generation", "Data & Analytics", "generating an analytical report", "Lead with the headline finding, support with the key numbers, then detail and recommendations. Make it skimmable."),
    ("ab-test-analysis", "A/B Test Analysis", "Data & Analytics", "analysing an A/B test", "State the metric, the lift, and whether the sample supports a conclusion. Be honest about significance; recommend ship/iterate/kill."),
    ("survey-analysis", "Survey Analysis", "Data & Analytics", "analysing survey results", "Summarise the quantitative results, theme the open responses, and surface the top 3 actionable insights with supporting numbers."),
    ("trend-analysis", "Trend Analysis", "Data & Analytics", "identifying trends in data", "Describe the direction, magnitude, and likely drivers; separate signal from noise and flag what to watch next."),
    ("csv-to-table", "CSV to Table", "Data & Analytics", "turning CSV data into a readable table", "Run the executable converter to render CSV as a clean Markdown table artifact; confirm the header row and flag ragged rows instead of guessing."),

    # --- Product & Project ---
    ("user-story-writing", "User Story Writing", "Product & Project", "writing user stories", "Use 'As a / I want / so that' with clear, testable acceptance criteria. Keep stories small and independent."),
    ("product-roadmap", "Product Roadmap", "Product & Project", "building a product roadmap", "Organise by now/next/later tied to outcomes, not dates-as-promises. Make priority and rationale explicit."),
    ("feature-spec", "Feature Spec / PRD", "Product & Project", "writing a feature spec or PRD", "Cover problem, goals, user stories, scope, non-goals, and success metrics. Make the cut-line explicit."),
    ("sprint-planning", "Sprint Planning", "Product & Project", "planning a sprint", "Set a sprint goal, pull a realistic set of stories against capacity, flag dependencies, and define done."),
    ("bug-report", "Bug Report", "Product & Project", "structuring a bug report", "Capture steps to reproduce, expected vs actual, environment, and severity. Make it reproducible by someone else."),
    ("release-notes", "Release Notes", "Product & Project", "writing release notes", "Group by new / improved / fixed, write user-facing benefits not internal jargon, and lead with the highlight."),
    ("competitive-feature", "Feature Comparison", "Product & Project", "comparing features against competitors", "Build a feature matrix, mark parity/gap/advantage, and conclude with the differentiation worth investing in."),

    # --- Personal Assistant ---
    ("daily-brief", "Daily Brief", "Personal Assistant", "starting the day or asking what's on the plate", "Pull together today's date, open tasks by priority, and anything time-sensitive into one tight morning brief. Use the executable generator to save a file."),
    ("weekly-review", "Weekly Review", "Personal Assistant", "reviewing the week or planning the next one", "Walk through wins, misses, lessons, and open loops, then set the top three priorities for next week. Keep it honest and forward-looking."),
    ("focus-sprint", "Focus Sprint Planner", "Personal Assistant", "planning a deep-work or pomodoro session", "Break the goal into 25-50 minute sprints with one concrete outcome each, schedule short breaks, and name the single distraction to eliminate."),
    ("habit-coach", "Habit Coach", "Personal Assistant", "building or breaking a habit", "Anchor the new habit to an existing routine, start embarrassingly small, define the trigger-action-reward loop, and suggest a simple streak tracker."),
    ("reading-list", "Reading List", "Personal Assistant", "saving an article, book, or link to read later", "Capture title, link, and a one-line reason it matters, then keep the list prioritised. Use the executable generator to append to the saved list."),
    ("meal-planner", "Meal Planner", "Personal Assistant", "planning meals for the week", "Plan simple meals around the user's preferences and constraints, build one consolidated grocery list, and reuse ingredients across meals to cut waste."),
    ("workout-plan", "Workout Plan", "Personal Assistant", "planning exercise or training", "Build a realistic weekly plan around available time and equipment, balance intensity with recovery, and start below what feels doable."),
    ("travel-packing", "Travel Packing List", "Personal Assistant", "packing for a trip", "Build a checklist from trip length, weather, and activities; group by category, flag documents and chargers first, and keep it carry-on minded."),
    ("learning-plan", "Learning Plan", "Personal Assistant", "learning a new skill or topic", "Define the target competence, sequence resources from fundamentals to practice projects, and set a weekly cadence with a visible milestone."),
    ("gift-ideas", "Gift Ideas", "Personal Assistant", "choosing a gift for someone", "Ask for the person's interests, the occasion, and budget, then suggest a shortlist from thoughtful to practical with a one-line why for each."),
    ("decision-helper", "Decision Helper", "Personal Assistant", "weighing a decision or trade-off", "Lay out the options against the criteria that actually matter, score them quickly, name the reversible vs irreversible parts, and recommend one with the reasoning."),
    ("wellness-break", "Wellness Break", "Personal Assistant", "taking a stress, stretch, or screen break", "Prescribe a 3-5 minute reset matched to the moment — breathing, stretch, walk, or eyes-off-screen — and say when to take the next one."),
    ("sleep-routine", "Sleep Wind-down", "Personal Assistant", "improving sleep or an evening wind-down", "Design a consistent wind-down: fixed lights-out, screens off beforehand, and one relaxing replacement activity. Adjust gently — no heroic overnight changes."),

    # --- IT & Dev ---
    ("code-review-helper", "Code Review Helper", "IT & Dev", "reviewing a code change", "Check correctness, edge cases, readability, and reuse. Lead with the highest-impact issues; suggest concrete fixes, not just problems."),
    ("api-documentation", "API Documentation", "IT & Dev", "documenting an API", "Document each endpoint: purpose, method/path, params, request/response examples, and errors. Make it copy-paste runnable."),
    ("tech-troubleshooting", "Tech Troubleshooting", "IT & Dev", "troubleshooting a technical issue", "Form a hypothesis, isolate variables, and give the most likely cause first with the exact step to confirm it. Avoid shotgun fixes."),
    ("deployment-checklist", "Deployment Checklist", "IT & Dev", "preparing a deployment", "Produce a pre/deploy/post checklist covering backups, migrations, rollback, monitoring, and smoke tests."),
    ("database-query", "Database Query", "IT & Dev", "writing a SQL or database query", "Write the exact query, explain the logic, and note performance/index considerations. Prefer safe, readable SQL."),
    ("automation-script", "Automation Script", "IT & Dev", "drafting an automation script", "Write a small, robust script with comments and error handling, and explain how to run it. Keep dependencies minimal."),
    ("password-generator", "Password Generator", "IT & Dev", "generating a strong password or passphrase", "Generate a cryptographically random password of the requested length and character mix. Use the executable generator; never reuse or invent passwords by hand."),
    ("markdown-to-html", "Markdown to HTML", "IT & Dev", "converting markdown into a shareable HTML page", "Convert the markdown into a clean dark-theme HTML page. Use the executable generator to save a previewable file."),
    ("regex-builder", "Regex Builder", "IT & Dev", "writing or explaining a regular expression", "Write the exact pattern, explain each part, show a matching and a non-matching example, and warn about catastrophic backtracking or engine differences."),
    ("git-helper", "Git Helper", "IT & Dev", "composing or untangling git commands", "Give the exact command sequence, state what each step changes, and flag anything destructive with a safe alternative. Never suggest a force-push without a warning."),
    ("json-formatter", "JSON Formatter", "IT & Dev", "validating or pretty-printing JSON", "Run the executable formatter to validate and pretty-print; report the exact error position when invalid. Use it before sharing or committing JSON."),

    # --- Communications ---
    ("meeting-summary", "Meeting Summary", "Communications", "summarising a meeting or call", "Capture decisions, action items with owners, and open questions in a tight summary. Lead with what changed and what's next."),
    ("executive-summary", "Executive Summary", "Communications", "writing an executive summary", "One paragraph: the situation, the recommendation, and the ask. Numbers first, jargon out, fits on half a page."),
    ("translation", "Business Translation", "Communications", "translating business text", "Translate accurately while preserving tone and intent, adapt idioms, and flag anything culturally sensitive or ambiguous."),
    ("announcement-writing", "Announcement Writing", "Communications", "writing an internal announcement", "Lead with the change and why it matters, what's expected, and where to ask questions. Keep the tone clear and reassuring."),
    ("email-reply", "Email Reply Drafting", "Communications", "drafting a reply to an email", "Mirror the sender's tone and formality, answer every question asked, keep it shorter than the original, and end with a clear next step."),
    ("difficult-conversation", "Difficult Conversation Prep", "Communications", "preparing for a hard conversation", "Script the opening line, name the issue with facts not judgments, anticipate the two most likely reactions, and define the acceptable outcome before walking in."),
    ("negotiation-prep", "Negotiation Prep", "Communications", "preparing for a negotiation", "Establish the target, walk-away point, and best alternative; list what's cheap for you but valuable to them; and plan the first offer with room to trade."),
    ("text-stats", "Writing Analyzer", "Communications", "checking word count, reading time, or wordiness of a text", "Run the executable analyzer for exact counts, reading and speaking time, and the wordiest sentence, then suggest the single best trim."),

    # --- Research & Insights ---
    ("fact-check", "Fact Check", "Research & Insights", "verifying a claim, number, or quote", "Restate the claim precisely, check it against the most reliable sources available, and report verdict, confidence, and the best source. Say plainly when evidence is thin."),
    ("compare-products", "Product Comparison", "Research & Insights", "comparing products, tools, or services before buying", "Build a short comparison on the criteria that matter to the user — price, fit, lock-in, reviews — and recommend one with the reasoning and the runner-up."),
    ("news-digest", "News Digest", "Research & Insights", "catching up on news about a topic or industry", "Gather the latest developments, group them by theme, and deliver the five items that actually matter with one-line takeaways. Skip duplicates and fluff."),
    ("deep-dive", "Deep Dive Research", "Research & Insights", "researching a topic in depth", "Define the question, gather from multiple angles, separate established facts from speculation, and finish with a structured brief: context, findings, open questions, recommendation."),
]
# fmt: on

# Slugs whose enabling injects an explicit note that an executable generator exists.
EXECUTABLE_SLUGS = {
    "invoice-creation", "sop-writing", "meeting-agenda", "email-newsletter",
    "proposal-writing", "meeting-minutes", "expense-tracking",
    "daily-brief", "reading-list", "password-generator", "unit-converter", "markdown-to-html",
    "text-stats", "json-formatter", "csv-to-table", "timezone-converter",
    "date-calculator", "checklist-builder",
}

# Default skills enabled on a fresh install (broadly useful for most users).
DEFAULT_ENABLED = {
    "meeting-summary", "email-triage", "todo-prioritization",
    "support-response", "blog-writing", "calendar-scheduling",
    "daily-brief",
}


def init_skills_db():
    """Create the skills table and seed the catalog (idempotent)."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS skills (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            when_to_use TEXT NOT NULL,
            instructions TEXT NOT NULL,
            executable INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 0,
            builtin INTEGER DEFAULT 1,
            source TEXT DEFAULT 'bundled',
            use_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
        CREATE INDEX IF NOT EXISTS idx_skills_enabled ON skills(enabled);
    """)

    now = time.time()
    for slug, name, category, when, how in _CATALOG:
        executable = 1 if slug in EXECUTABLE_SLUGS else 0
        enabled = 1 if slug in DEFAULT_ENABLED else 0
        # Insert if new; refresh static fields without clobbering user enable state.
        existing = conn.execute("SELECT enabled FROM skills WHERE slug = ?", (slug,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO skills (slug, name, category, description, when_to_use, "
                "instructions, executable, enabled, builtin, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'bundled', ?)",
                (slug, name, category, when.capitalize(), when, how, executable, enabled, now),
            )
        else:
            conn.execute(
                "UPDATE skills SET name=?, category=?, description=?, when_to_use=?, "
                "instructions=?, executable=? WHERE slug=?",
                (name, category, when.capitalize(), when, how, executable, slug),
            )
    conn.commit()
    conn.close()
    log.info(f"Skills catalog ready ({len(_CATALOG)} skills)")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["executable"] = bool(d.get("executable"))
    d["enabled"] = bool(d.get("enabled"))
    d["builtin"] = bool(d.get("builtin"))
    return d


def list_skills(category: str | None = None, enabled_only: bool = False) -> list[dict]:
    conn = _get_db()
    q = "SELECT * FROM skills"
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY category, name"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_skill(slug: str) -> dict | None:
    conn = _get_db()
    row = conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def set_skill_enabled(slug: str, enabled: bool) -> bool:
    conn = _get_db()
    cur = conn.execute("UPDATE skills SET enabled = ? WHERE slug = ?", (1 if enabled else 0, slug))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    if changed:
        log.info(f"Skill {'enabled' if enabled else 'disabled'}: {slug}")
    return changed


def bump_use_count(slug: str):
    conn = _get_db()
    conn.execute("UPDATE skills SET use_count = use_count + 1 WHERE slug = ?", (slug,))
    conn.commit()
    conn.close()


def categories_summary() -> list[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT category, COUNT(*) AS total, SUM(enabled) AS enabled "
        "FROM skills GROUP BY category ORDER BY category"
    ).fetchall()
    conn.close()
    return [{"category": r["category"], "total": r["total"], "enabled": r["enabled"] or 0} for r in rows]


def counts() -> dict:
    conn = _get_db()
    row = conn.execute("SELECT COUNT(*) AS total, SUM(enabled) AS enabled FROM skills").fetchone()
    conn.close()
    return {"total": row["total"] or 0, "enabled": row["enabled"] or 0}


def search_skills(query: str, limit: int = 20) -> list[dict]:
    """Keyword search across name, description, and instructions."""
    terms = [t for t in query.lower().split() if len(t) > 2]
    if not terms:
        return []
    conn = _get_db()
    like = "%" + "%".join(terms[:1]) + "%"
    rows = conn.execute(
        "SELECT * FROM skills WHERE LOWER(name || ' ' || description || ' ' || "
        "instructions || ' ' || category) LIKE ? ORDER BY enabled DESC, name LIMIT ?",
        (like, limit),
    ).fetchall()
    conn.close()
    results = [_row_to_dict(r) for r in rows]
    # Rank by how many query terms appear.
    def score(s: dict) -> int:
        blob = f"{s['name']} {s['description']} {s['instructions']} {s['category']}".lower()
        return sum(1 for t in terms if t in blob)
    results.sort(key=score, reverse=True)
    return results


def _tokens(text: str) -> list[str]:
    return [w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split() if len(w) > 3]


def _term_matches(term: str, words: set[str]) -> bool:
    """Loose match tolerant of plurals/stems (hiring~hire, descriptions~description)."""
    if term in words:
        return True
    stem = term[:5]
    return any(w == term or w.startswith(stem) or term.startswith(w[:5]) for w in words)


def recommend_skills(goal_text: str, limit: int = 8) -> list[dict]:
    """Recommend skills for a free-text goal (used by onboarding)."""
    terms = _tokens(goal_text)
    if not terms:
        return []
    conn = _get_db()
    rows = conn.execute("SELECT * FROM skills").fetchall()
    conn.close()
    scored = []
    for r in rows:
        s = _row_to_dict(r)
        words = set(_tokens(f"{s['name']} {s['description']} {s['when_to_use']} {s['instructions']}"))
        cat_words = set(_tokens(s["category"]))
        score = 0
        for t in terms:
            if _term_matches(t, words):
                score += 1
            if _term_matches(t, cat_words):
                score += 2  # a category hit is a strong signal
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:limit]]


# ---------------------------------------------------------------------------
# Prompt building — what JARVIS sees about its skills
# ---------------------------------------------------------------------------

def enabled_skills_prompt(max_skills: int = 25) -> str:
    """Full instructions for enabled skills, injected into the system prompt."""
    enabled = list_skills(enabled_only=True)[:max_skills]
    if not enabled:
        return ""
    lines = [
        "ACTIVE SKILLS (load these instructions as working memory when the situation fits):",
        "- Skill protocol: pick the smallest relevant enabled skill, follow its concrete instructions, and mention the skill only when useful to the user.",
        "- Executable skills produce files via [ACTION:RUN_SKILL] slug ||| {json params}; prompt-only skills shape the response directly.",
        "- If no enabled skill fits but an available skill would help, briefly suggest enabling it instead of pretending it is active.",
    ]
    for s in enabled:
        tag = " [executable — use [ACTION:RUN_SKILL] " + s["slug"] + "]" if s["executable"] else ""
        lines.append(f"- {s['name']} — when {s['when_to_use']}: {s['instructions']}{tag}")
    return "\n".join(lines)


def catalog_index_prompt(limit: int = 60) -> str:
    """A lightweight menu of available (not-yet-enabled) skills JARVIS can suggest."""
    available = [s for s in list_skills() if not s["enabled"]][:limit]
    if not available:
        return ""
    by_cat: dict[str, list[str]] = {}
    for s in available:
        by_cat.setdefault(s["category"], []).append(s["name"])
    lines = ["AVAILABLE SKILLS (not yet enabled — suggest enabling when relevant):"]
    for cat, names in by_cat.items():
        lines.append(f"- {cat}: {', '.join(names)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Executable handlers (hybrid model) — produce real artifacts
# ---------------------------------------------------------------------------

def _save_artifact(name: str, content: str) -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    path = ARTIFACTS_DIR / f"{int(time.time())}_{safe}"
    path.write_text(content, encoding="utf-8")
    return path



def list_artifacts(limit: int = 50) -> list[dict]:
    """Return recent artifacts produced by executable skills."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(ARTIFACTS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        if not path.is_file():
            continue
        stat = path.stat()
        items.append({
            "name": path.name,
            "path": str(path),
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
            "download_url": f"/api/artifacts/{path.name}",
        })
    return items


def read_artifact(name: str, max_chars: int = 12000) -> dict:
    """Read an artifact by safe file name for previewing in the UI."""
    safe = Path(name).name
    path = ARTIFACTS_DIR / safe
    if not path.exists() or not path.is_file():
        return {"error": "Artifact not found"}
    content = path.read_text(encoding="utf-8", errors="replace")
    return {"name": safe, "path": str(path), "content": content[:max_chars], "truncated": len(content) > max_chars}

def _run_invoice(params: dict) -> dict:
    from datetime import datetime, timedelta
    seller = params.get("seller", "Your Company")
    client = params.get("client", "Client")
    items = params.get("items") or [{"description": params.get("description", "Services"),
                                     "qty": 1, "unit_price": float(params.get("amount", 0) or 0)}]
    tax_rate = float(params.get("tax_rate", 0) or 0)
    number = params.get("number", f"INV-{int(time.time()) % 100000}")
    due = params.get("due_date") or (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")

    subtotal = sum(float(i.get("qty", 1)) * float(i.get("unit_price", 0)) for i in items)
    tax = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax, 2)

    lines = [f"INVOICE {number}", f"Date: {datetime.now().strftime('%Y-%m-%d')}   Due: {due}", "",
             f"From: {seller}", f"To:   {client}", "", "Items:"]
    for i in items:
        qty = float(i.get("qty", 1)); up = float(i.get("unit_price", 0))
        lines.append(f"  - {i.get('description', 'Item')}: {qty:g} x {up:.2f} = {qty * up:.2f}")
    lines += ["", f"Subtotal: {subtotal:.2f}", f"Tax ({tax_rate:g}%): {tax:.2f}",
              f"TOTAL DUE: {total:.2f}", "", f"Payment terms: {params.get('terms', 'Net 14')}"]
    content = "\n".join(lines)
    path = _save_artifact(f"invoice_{number}.txt", content)
    return {"summary": f"Invoice {number} for {total:.2f}, due {due}", "path": str(path), "content": content}


def _run_sop(params: dict) -> dict:
    title = params.get("title", "Standard Operating Procedure")
    purpose = params.get("purpose", "")
    steps = params.get("steps") or []
    lines = [f"SOP: {title}", "", f"Purpose: {purpose}", "", "Steps:"]
    for n, step in enumerate(steps, 1):
        lines.append(f"  {n}. {step}")
    if not steps:
        lines.append("  (add steps)")
    lines += ["", "Quality check: confirm each step was completed and the output meets standard."]
    content = "\n".join(lines)
    path = _save_artifact(f"sop_{title}.txt", content)
    return {"summary": f"SOP '{title}' with {len(steps)} steps", "path": str(path), "content": content}


def _run_agenda(params: dict) -> dict:
    title = params.get("title", "Meeting Agenda")
    objective = params.get("objective", "")
    topics = params.get("topics") or []
    lines = [f"AGENDA: {title}", "", f"Objective: {objective}", "", "Topics:"]
    for t in topics:
        if isinstance(t, dict):
            lines.append(f"  - {t.get('topic', '')} ({t.get('minutes', '?')} min, owner: {t.get('owner', 'TBD')})")
        else:
            lines.append(f"  - {t}")
    if not topics:
        lines.append("  (add topics)")
    content = "\n".join(lines)
    path = _save_artifact(f"agenda_{title}.txt", content)
    return {"summary": f"Agenda '{title}' with {len(topics)} topics", "path": str(path), "content": content}



def _run_email_newsletter(params: dict) -> dict:
    audience = params.get("audience", "customers")
    topic = params.get("topic") or params.get("description", "Company update")
    cta = params.get("cta", "Reply with any questions")
    sections = params.get("sections") or ["Why it matters", "What is changing", "Next step"]
    lines = [
        f"Subject option A: {topic}: the short version",
        f"Subject option B: A useful update for {audience}",
        f"Preview text: {params.get('preview', 'A concise update with one clear next step.')}",
        "",
        f"Hi {params.get('greeting', 'there')},",
        "",
        f"Here is the practical update on {topic}.",
    ]
    for section in sections:
        lines += ["", f"## {section}", params.get(str(section).lower().replace(' ', '_'), "Add the key point here in two or three skimmable sentences.")]
    lines += ["", f"Primary CTA: {cta}", "", f"Sign-off: {params.get('signoff', 'Best,')}" ]
    content = "\n".join(lines)
    path = _save_artifact(f"newsletter_{topic}.md", content)
    return {"summary": f"Newsletter draft for {audience} about {topic}", "path": str(path), "content": content}


def _run_proposal(params: dict) -> dict:
    client = params.get("client", "Client")
    project = params.get("project") or params.get("description", "Project")
    deliverables = params.get("deliverables") or ["Discovery", "Implementation", "Handoff"]
    timeline = params.get("timeline", "TBD")
    price = params.get("price", "TBD")
    lines = [
        f"# Proposal: {project}",
        f"Prepared for: {client}",
        "",
        "## Client Problem",
        params.get("problem", "State the client's problem in their words."),
        "",
        "## Recommended Scope",
    ]
    for item in deliverables:
        lines.append(f"- {item}")
    lines += [
        "", "## Timeline", str(timeline),
        "", "## Investment", str(price),
        "", "## Assumptions", params.get("assumptions", "Scope, access, and approval timing to be confirmed before kickoff."),
        "", "## Next Step", params.get("next_step", "Approve this proposal and schedule kickoff."),
    ]
    content = "\n".join(lines)
    path = _save_artifact(f"proposal_{client}_{project}.md", content)
    return {"summary": f"Proposal draft for {client}: {project}", "path": str(path), "content": content}


def _run_meeting_minutes(params: dict) -> dict:
    title = params.get("title", "Meeting Minutes")
    attendees = params.get("attendees") or []
    decisions = params.get("decisions") or []
    actions = params.get("actions") or []
    open_questions = params.get("open_questions") or []
    lines = [f"# Minutes: {title}", "", f"Date: {params.get('date', time.strftime('%Y-%m-%d'))}", f"Attendees: {', '.join(attendees) if attendees else 'TBD'}", "", "## Summary", params.get("summary", "What changed and what happens next."), "", "## Decisions"]
    lines += [f"- {d}" for d in decisions] or ["- None captured"]
    lines += ["", "## Action Items"]
    if actions:
        for item in actions:
            if isinstance(item, dict):
                lines.append(f"- {item.get('owner', 'TBD')}: {item.get('task', '')} — due {item.get('due', 'TBD')}")
            else:
                lines.append(f"- {item}")
    else:
        lines.append("- None captured")
    lines += ["", "## Open Questions"]
    lines += [f"- {q}" for q in open_questions] or ["- None captured"]
    content = "\n".join(lines)
    path = _save_artifact(f"minutes_{title}.md", content)
    return {"summary": f"Meeting minutes for {title}", "path": str(path), "content": content}


def _run_expense_tracking(params: dict) -> dict:
    expenses = params.get("expenses") or [{"vendor": params.get("vendor", "Vendor"), "category": params.get("category", "General"), "amount": float(params.get("amount", 0) or 0), "date": params.get("date", time.strftime('%Y-%m-%d'))}]
    total = sum(float(e.get("amount", 0) or 0) for e in expenses)
    lines = ["date,vendor,category,amount,notes"]
    for e in expenses:
        notes = str(e.get("notes", "")).replace('"', '""')
        lines.append(f"{e.get('date', '')},{e.get('vendor', '')},{e.get('category', '')},{float(e.get('amount', 0) or 0):.2f},\"{notes}\"")
    content = "\n".join(lines) + f"\nTOTAL,,,{total:.2f},\n"
    path = _save_artifact(f"expenses_{time.strftime('%Y%m%d')}.csv", content)
    return {"summary": f"Expense log with {len(expenses)} item(s), total {total:.2f}", "path": str(path), "content": content}

def _run_daily_brief(params: dict) -> dict:
    from datetime import datetime
    import memory
    now = datetime.now()
    tasks = memory.get_open_tasks()
    lines = [f"# Daily Brief — {now.strftime('%A, %B %d, %Y')}", ""]
    focus = params.get("focus") or params.get("description", "")
    if focus:
        lines += [f"Focus: {focus}", ""]
    lines.append("## Open Tasks")
    if tasks:
        for t in tasks[:15]:
            due = f" — due {t['due_date']}" if t.get("due_date") else ""
            lines.append(f"- [{t.get('priority', 'medium')}] {t.get('title', '')}{due}")
    else:
        lines.append("- Nothing on the docket. A rare luxury, sir.")
    lines += ["", "## Top Three", "1. ", "2. ", "3. ", "",
              "## Notes", params.get("notes", "")]
    content = "\n".join(lines)
    path = _save_artifact(f"daily_brief_{now.strftime('%Y%m%d')}.md", content)
    open_count = len(tasks)
    return {"summary": f"Daily brief for {now.strftime('%A')} with {open_count} open task(s)", "path": str(path), "content": content}


def _run_password(params: dict) -> dict:
    import secrets
    import string
    length = max(8, min(128, int(params.get("length", 20) or 20)))
    charset = string.ascii_letters + string.digits
    if str(params.get("symbols", "true")).lower() not in ("0", "false", "no"):
        charset += "!@#$%^&*()-_=+[]{}"
    pwd = "".join(secrets.choice(charset) for _ in range(length))
    # Deliberately no artifact file — secrets don't belong on disk.
    return {"summary": f"Generated a {length}-character password", "content": pwd}


_UNIT_FACTORS = {
    # canonical: meters, grams, bytes
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "in": ("length", 0.0254), "ft": ("length", 0.3048), "yd": ("length", 0.9144), "mi": ("length", 1609.344),
    "mg": ("mass", 0.001), "g": ("mass", 1.0), "kg": ("mass", 1000.0), "oz": ("mass", 28.349523125),
    "lb": ("mass", 453.59237), "st": ("mass", 6350.29318),
    "b": ("data", 1.0), "kb": ("data", 1024.0), "mb": ("data", 1024.0 ** 2),
    "gb": ("data", 1024.0 ** 3), "tb": ("data", 1024.0 ** 4),
}

_UNIT_ALIASES = {
    "millimeter": "mm", "millimeters": "mm", "centimeter": "cm", "centimeters": "cm",
    "meter": "m", "meters": "m", "kilometer": "km", "kilometers": "km",
    "inch": "in", "inches": "in", "foot": "ft", "feet": "ft", "yard": "yd", "yards": "yd",
    "mile": "mi", "miles": "mi", "milligram": "mg", "milligrams": "mg",
    "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
    "ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb", "lbs": "lb",
    "stone": "st", "bytes": "b", "byte": "b",
    "kilobyte": "kb", "kilobytes": "kb", "megabyte": "mb", "megabytes": "mb",
    "gigabyte": "gb", "gigabytes": "gb", "terabyte": "tb", "terabytes": "tb",
    "c": "celsius", "f": "fahrenheit", "k": "kelvin",
}


def _run_unit_convert(params: dict) -> dict:
    raw_value = params.get("value", params.get("amount"))
    src = str(params.get("from", "")).strip().lower()
    dst = str(params.get("to", "")).strip().lower()
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return {"error": "I need a numeric value to convert, e.g. {\"value\": 5, \"from\": \"mi\", \"to\": \"km\"}."}
    src = _UNIT_ALIASES.get(src, src)
    dst = _UNIT_ALIASES.get(dst, dst)

    temps = {"celsius", "fahrenheit", "kelvin"}
    if src in temps and dst in temps:
        celsius = {"celsius": value, "fahrenheit": (value - 32) * 5 / 9, "kelvin": value - 273.15}[src]
        result = {"celsius": celsius, "fahrenheit": celsius * 9 / 5 + 32, "kelvin": celsius + 273.15}[dst]
    else:
        s, d = _UNIT_FACTORS.get(src), _UNIT_FACTORS.get(dst)
        if not s or not d:
            return {"error": f"I don't know how to convert '{src}' to '{dst}', sir."}
        if s[0] != d[0]:
            return {"error": f"Can't convert {s[0]} to {d[0]}, sir — different kinds of quantity."}
        result = value * s[1] / d[1]
    rounded = round(result, 6)
    answer = f"{value:g} {src} = {rounded:g} {dst}"
    return {"summary": answer, "content": answer}


def _md_inline(text: str) -> str:
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text


def _run_md_to_html(params: dict) -> dict:
    import html as html_mod
    source = params.get("markdown") or params.get("content") or params.get("description", "")
    if not source.strip():
        return {"error": "I need some markdown to convert, sir."}
    title = params.get("title", "Document")
    body: list[str] = []
    in_list = in_code = False
    for line in source.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                body.append("</pre>")
            else:
                body.append("<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(html_mod.escape(line))
            continue
        escaped = _md_inline(html_mod.escape(line))
        stripped = escaped.strip()
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{stripped[2:]}</li>")
            continue
        if in_list:
            body.append("</ul>")
            in_list = False
        if stripped.startswith("#"):
            level = min(6, len(stripped) - len(stripped.lstrip("#")))
            body.append(f"<h{level}>{stripped[level:].strip()}</h{level}>")
        elif stripped:
            body.append(f"<p>{stripped}</p>")
    if in_list:
        body.append("</ul>")
    if in_code:
        body.append("</pre>")
    content = (
        "<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{html_mod.escape(title)}</title>\n"
        "<style>body{background:#0a0e14;color:#dbe4ee;font-family:system-ui,sans-serif;"
        "max-width:760px;margin:3rem auto;padding:0 1rem;line-height:1.6}"
        "a{color:#00d4ff}code,pre{background:#11161f;border-radius:4px;padding:2px 6px}"
        "pre{padding:12px;overflow-x:auto}h1,h2,h3{color:#fff}</style>\n</head>\n<body>\n"
        + "\n".join(body) + "\n</body>\n</html>\n"
    )
    path = _save_artifact(f"{title}.html", content)
    return {"summary": f"Converted '{title}' to a dark-theme HTML page", "path": str(path), "content": content}


def _run_reading_list(params: dict) -> dict:
    title = params.get("title") or params.get("description", "Untitled")
    url = params.get("url", "")
    note = params.get("note", "")
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / "reading_list.md"
    if not path.exists():
        path.write_text("# Reading List\n\n", encoding="utf-8")
    entry = f"- [{time.strftime('%Y-%m-%d')}] {title}"
    if url:
        entry += f" — {url}"
    if note:
        entry += f" ({note})"
    with path.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")
    content = path.read_text(encoding="utf-8")
    count = sum(1 for line in content.splitlines() if line.startswith("- "))
    return {"summary": f"Added '{title}' to the reading list ({count} item(s) saved)", "path": str(path), "content": content}


def _run_text_stats(params: dict) -> dict:
    import re
    from collections import Counter
    text = str(params.get("text") or params.get("content") or params.get("description") or "")
    if not text.strip():
        return {"error": "I need some text to analyse, sir."}
    words = re.findall(r"[A-Za-z0-9'’-]+", text)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    word_count = len(words)
    reading_min = max(1, round(word_count / 200))
    speaking_min = max(1, round(word_count / 130))
    avg_sentence = word_count / max(1, len(sentences))
    stop = {"the", "and", "that", "with", "this", "from", "have", "your", "for",
            "are", "was", "but", "not", "you", "all", "can", "will", "has",
            "their", "they", "them", "were", "been", "into", "more", "than",
            "when", "what", "which", "about", "would", "could", "there"}
    top = Counter(w.lower() for w in words if len(w) > 3 and w.lower() not in stop).most_common(5)
    lines = [
        f"Words: {word_count}",
        f"Characters: {len(text)}",
        f"Sentences: {len(sentences)} (avg {avg_sentence:.1f} words)",
        f"Reading time: ~{reading_min} min   Speaking time: ~{speaking_min} min",
    ]
    if top:
        lines.append("Top words: " + ", ".join(f"{w} ({n})" for w, n in top))
    longest = max(sentences, key=lambda s: len(s.split()), default="")
    if longest and len(longest.split()) > 30:
        lines.append(f"Wordiest sentence ({len(longest.split())} words): {longest[:160]}…")
    content = "\n".join(lines)
    return {"summary": f"{word_count} words, ~{reading_min} min read", "content": content}


def _run_json_format(params: dict) -> dict:
    import json
    raw = params.get("json") or params.get("text") or params.get("content") or ""
    if isinstance(raw, (dict, list)):
        parsed = raw
    else:
        if not str(raw).strip():
            return {"error": "I need some JSON to format, sir."}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}."}
    sort_keys = str(params.get("sort_keys", "")).lower() in ("1", "true", "yes")
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=sort_keys)
    kind = type(parsed).__name__
    size = len(parsed) if isinstance(parsed, (dict, list)) else 1
    name = str(params.get("name", "formatted")).removesuffix(".json") + ".json"
    path = _save_artifact(name, pretty + "\n")
    return {"summary": f"Valid JSON ({kind}, {size} top-level item(s)), formatted and saved",
            "path": str(path), "content": pretty}


def _run_csv_table(params: dict) -> dict:
    import csv as csv_mod
    import io
    raw = str(params.get("csv") or params.get("text") or params.get("content") or "")
    if not raw.strip():
        return {"error": "I need CSV data to convert, sir."}
    rows = [r for r in csv_mod.reader(io.StringIO(raw.strip())) if r]
    if not rows:
        return {"error": "No rows found in that CSV, sir."}
    width = max(len(r) for r in rows)
    ragged = sum(1 for r in rows if len(r) != width)
    norm = [r + [""] * (width - len(r)) for r in rows]

    def cell(v: str) -> str:
        return str(v).replace("|", "\\|").strip()

    header, data = norm[0], norm[1:]
    lines = ["| " + " | ".join(cell(c) for c in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(cell(c) for c in r) + " |" for r in data]
    content = "\n".join(lines)
    path = _save_artifact(f"table_{time.strftime('%Y%m%d_%H%M%S')}.md", content + "\n")
    summary = f"Converted {len(data)} row(s) x {width} column(s) to a Markdown table"
    if ragged:
        summary += f" ({ragged} ragged row(s) padded)"
    return {"summary": summary, "path": str(path), "content": content}


# Spoken city names → IANA zones (lowercase keys). Anything not listed falls
# through to ZoneInfo(name) so full IANA names always work.
_TZ_ALIASES = {
    "warsaw": "Europe/Warsaw", "london": "Europe/London", "berlin": "Europe/Berlin",
    "paris": "Europe/Paris", "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "kyiv": "Europe/Kyiv", "dubai": "Asia/Dubai", "delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "tokyo": "Asia/Tokyo", "seoul": "Asia/Seoul", "sydney": "Australia/Sydney",
    "auckland": "Pacific/Auckland", "new york": "America/New_York", "nyc": "America/New_York",
    "boston": "America/New_York", "miami": "America/New_York", "chicago": "America/Chicago",
    "austin": "America/Chicago", "denver": "America/Denver", "phoenix": "America/Phoenix",
    "san francisco": "America/Los_Angeles", "los angeles": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "sf": "America/Los_Angeles", "la": "America/Los_Angeles",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "sao paulo": "America/Sao_Paulo", "utc": "UTC", "gmt": "UTC",
}


def _run_timezone(params: dict) -> dict:
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    src_name = str(params.get("from", "")).strip()
    dst_name = str(params.get("to", "")).strip()
    if not src_name or not dst_name:
        return {"error": "I need both zones, sir — e.g. {\"time\": \"15:00\", \"from\": \"Warsaw\", \"to\": \"San Francisco\"}."}
    try:
        src = ZoneInfo(_TZ_ALIASES.get(src_name.lower(), src_name))
        dst = ZoneInfo(_TZ_ALIASES.get(dst_name.lower(), dst_name))
    except Exception:
        return {"error": "I couldn't resolve one of those timezones, sir — try an IANA name like Europe/Warsaw."}
    time_str = str(params.get("time", "") or "").strip().lower()
    date_str = str(params.get("date", "") or "").strip()
    try:
        if not time_str or time_str == "now":
            base = datetime.now(tz=src)
        else:
            m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", time_str)
            if not m:
                raise ValueError(time_str)
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if m.group(3) == "pm" and hour < 12:
                hour += 12
            elif m.group(3) == "am" and hour == 12:
                hour = 0
            day = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
            base = datetime(day.year, day.month, day.day, hour, minute, tzinfo=src)
    except (ValueError, TypeError):
        return {"error": "I couldn't parse that time, sir — use HH:MM (24h or am/pm) and YYYY-MM-DD."}
    converted = base.astimezone(dst)
    day_note = ""
    if converted.date() > base.date():
        day_note = " (next day)"
    elif converted.date() < base.date():
        day_note = " (previous day)"
    answer = (f"{base.strftime('%H:%M')} in {src_name} ({src.key}) = "
              f"{converted.strftime('%H:%M')} in {dst_name} ({dst.key}){day_note} "
              f"on {base.strftime('%Y-%m-%d')}")
    return {"summary": answer, "content": answer}


def _run_date_calc(params: dict) -> dict:
    from datetime import date, datetime, timedelta

    def parse(d) -> date:
        return datetime.strptime(str(d).strip(), "%Y-%m-%d").date()

    try:
        if params.get("add_days") is not None or params.get("days") is not None:
            start = parse(params.get("date") or params.get("from") or date.today().isoformat())
            delta = int(params.get("add_days", params.get("days")))
            result = start + timedelta(days=delta)
            verb = "after" if delta >= 0 else "before"
            answer = f"{abs(delta)} day(s) {verb} {start.isoformat()} is {result.strftime('%A, %Y-%m-%d')}"
            return {"summary": answer, "content": answer}
        start = parse(params.get("from") or date.today().isoformat())
        end = parse(params.get("to") or params.get("until"))
        days = (end - start).days
        weeks, rem = divmod(abs(days), 7)
        first = min(start, end)
        weekdays = sum(1 for i in range(abs(days)) if (first + timedelta(days=i)).weekday() < 5)
        answer = (f"{start.isoformat()} ({start.strftime('%A')}) to {end.isoformat()} ({end.strftime('%A')}): "
                  f"{days} day(s) — {weeks} week(s) and {rem} day(s), {weekdays} weekday(s)")
        return {"summary": answer, "content": answer}
    except (TypeError, ValueError):
        return {"error": "I need ISO dates, sir — e.g. {\"from\": \"2026-06-09\", \"to\": \"2026-12-24\"} or {\"date\": \"2026-06-09\", \"add_days\": 30}."}


def _run_checklist(params: dict) -> dict:
    title = str(params.get("title") or params.get("description") or "Checklist").strip()
    items = params.get("items") or params.get("steps") or []
    if isinstance(items, str):
        items = [i.strip("-• \t") for i in items.splitlines() if i.strip()]
    lines = [f"# {title}", ""]
    for item in items:
        if isinstance(item, dict):
            note = f" — {item['note']}" if item.get("note") else ""
            lines.append(f"- [ ] {item.get('item') or item.get('title', '')}{note}")
        else:
            lines.append(f"- [ ] {item}")
    if not items:
        lines.append("- [ ] (add items)")
    content = "\n".join(lines) + "\n"
    path = _save_artifact(f"checklist_{title}.md", content)
    return {"summary": f"Checklist '{title}' with {len(items)} item(s)", "path": str(path), "content": content}


EXECUTABLE_HANDLERS = {
    "invoice-creation": _run_invoice,
    "sop-writing": _run_sop,
    "meeting-agenda": _run_agenda,
    "email-newsletter": _run_email_newsletter,
    "proposal-writing": _run_proposal,
    "meeting-minutes": _run_meeting_minutes,
    "expense-tracking": _run_expense_tracking,
    "daily-brief": _run_daily_brief,
    "password-generator": _run_password,
    "unit-converter": _run_unit_convert,
    "markdown-to-html": _run_md_to_html,
    "reading-list": _run_reading_list,
    "text-stats": _run_text_stats,
    "json-formatter": _run_json_format,
    "csv-to-table": _run_csv_table,
    "timezone-converter": _run_timezone,
    "date-calculator": _run_date_calc,
    "checklist-builder": _run_checklist,
}


def run_skill(slug: str, params: dict | None = None) -> dict:
    """Execute an executable skill's handler. Returns {summary, path, content} or {error}."""
    handler = EXECUTABLE_HANDLERS.get(slug)
    if not handler:
        return {"error": f"Skill '{slug}' is not executable."}
    try:
        result = handler(params or {})
        bump_use_count(slug)
        log.info(f"Ran executable skill {slug}: {result.get('summary')}")
        return result
    except Exception as e:
        log.error(f"Skill {slug} failed: {e}")
        return {"error": str(e)}


# Initialize on import
init_skills_db()
