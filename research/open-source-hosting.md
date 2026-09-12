# Open-source Clipshelf and paid managed hosting

Research date: **2026-09-12**. Companion to [server plan rev. 4](server-plan.md),
sections 10–12. This is research and a proposed commercial direction, not a
launched service, legal opinion, license change, or approved price.

Evidence: **[SRC]** inspected repository or first-party document;
**[INFERENCE]** recommendation derived from that evidence;
**[ASSUMPTION]** an unmeasured planning input. Prices are dated observations,
not quotes or capacity guarantees; business jurisdiction is not yet established.

## 1. Recommendation

Keep the existing MIT application and offer paid operation of the same software.
Start with a maximum of five manually provisioned customer instances on commercial
infrastructure, not the household NAS. Charge for managed hosting, bounded model
usage/storage, upgrades, verified mail, backups and support. Do not create an
enterprise fork, require self-hosters to subscribe, or promise unlimited AI,
indefinite unlimited media, or guaranteed TikTok acquisition. **[INFERENCE]**

Keep shared product/security/portability improvements public. Choose whether and
how to automate billing/provisioning after a measured paid pilot, not before
learning customer demand and resource/support costs. The self-hosted release
checks remain mandatory; paid hosting adds gates rather than replacing them.

## 2. Repository facts that change the commercial plan

| Inspected evidence [SRC] | Consequence [INFERENCE] |
|---|---|
| [LICENSE](../LICENSE) already grants MIT rights, including use, modification, distribution and sale. [README](../README.md) identifies MIT. | No relicensing is needed to sell hosting. Public source is not itself a new feature or evidence of an operable paid service. |
| [models.py](../clipshelf/models.py) has users, collections and one `ServerSettings` record with instance identity/model settings; no tenant/subscription model. [admin_views.py](../clipshelf/admin_views.py) operates on instance-wide accounts. | Do not confuse private collection permissions with a complete SaaS tenant boundary, or give unrelated paying users application-admin rights. |
| [compose.yaml](../compose.yaml) runs web and one worker on one local data volume, with a configured origin, UID/GID and SMTP. | Reuse this deployable unit per customer. Isolation, resource limits, commercial public ingress and off-host recovery still need actual tests. |
| [models.py](../clipshelf/models.py) protects user/owner references against deletion. [Server plan](server-plan.md) explicitly says disable-only accounts and no automatic source expiry. | Erasure and bounded commercial retention require explicit product decisions and implementation, not a different privacy-policy sentence. |
| [urls.py](../clipshelf/urls.py) has capture, collection, source and import routes, but no customer export/billing/erasure routes. Operator [backup](../clipshelf/management/commands/backup.py) and [restore](../clipshelf/management/commands/restore.py) commands exist. | Backup is disaster recovery, not a portable, authorized customer export. A safe account-level exit path is unfinished commercial scope. |
| [requirements.txt](../requirements.txt) installs gallery-dl and yt-dlp; [Dockerfile](../Dockerfile) installs FFmpeg and labels Clipshelf MIT. [acquisition.py](../clipshelf/acquisition.py) invokes extractors as subprocesses. | Audit all shipped dependencies and corresponding-source/notice obligations. An MIT image label does not mean every bundled component is MIT. |
| [Android build](../android/app/build.gradle) uses one `dev.clipshelf.app` identity and an external release signing key. | Reuse the same app/signing identity for self-hosted and hosted connections. Do not expose signing secrets or use a separately signed cloud fork. |

### License choice

| Choice | Rights and obligations [SRC] | Commercial consequence [INFERENCE] |
|---|---|---|
| Keep MIT | Commercial use, modification and redistribution are permitted; retain its copyright/license notice. It has no network-source requirement. [MIT][mit] | Simplest default. Other people may also sell hosting, including closed modifications; compete on service rather than license enforcement. |
| AGPL-3.0 for a future covered version | Section 13 requires a modified network-interactive version to prominently offer its Corresponding Source to those users. Charging remains permitted; sections 2, 4 and 5 also matter. [AGPL][agpl] | Adds reciprocity, not exclusive hosting rights. Requires provenance, dependency compatibility and contribution-policy review; do not treat it as a revenue guarantee. |
| A license forbidding competing commercial hosts | OSI's definition forbids discrimination against fields of endeavor, including business use. [Open Source Definition, section 6][osd] | Such a restriction is not the genuinely open-source model requested. Do not call source-available code open source merely because it is readable. |

A future license choice cannot be assumed to withdraw permissions already granted
with published MIT copies. Inspect actual contribution rights and compatibility;
a single name in `LICENSE` is not proof of sole authorship. Nor does the first
MIT-licensed outside contribution automatically make a later copyleft distribution
impossible. Do not introduce a CLA, dual licensing or relicensing work without a
concrete governance reason. The proposal leaves `LICENSE` unchanged. **[INFERENCE]**

### Nearby products: precedent, not demand validation

Published offers inspected on the research date:

| Product | Source license | Hosted offer [SRC] | Useful comparison |
|---|---|---|---|
| [Linkwarden][linkwarden] | AGPLv3, stated on its pricing page. | $3/user/month **billed yearly**, 30,000 links/user, 14-day trial. | Sells managed operation, updates and bundled AI; advertises export. Do not mistake the annual-equivalent figure for monthly billing. |
| [Karakeep][karakeep] | [AGPL-3.0][karakeep-license]. | Pro $4/month or $40/year, 50,000 bookmarks and 50 GB; a small free tier also exists. | Self-hosting remains free; cloud includes AI tagging and storage. Advertises export and 30-day availability after cancellation. |
| [wallabag.it][wallabag] | [wallabag repository identifies MIT][wallabag-license]. | EUR 4/three months, EUR 11/year, EUR 30/year supporter; no automatic renewal. | Permissive licensing coexists with paid hosting; all plans include the advertised features. |

These are adjacent bookmarking/read-later products, not equivalent video/vision
workloads. Their prices establish neither Clipshelf's costs nor willingness to
pay. Validate the specific value: turning a recommendation in a shared page,
video or carousel into usable repositories, verbatim prompts and guides rather
than another forgotten bookmark. Demonstrate supported acquisition honestly to
prospective users before choosing a price. **[INFERENCE]**

## 3. Smallest credible hosting architecture

| Option | Benefit | Cost/risk | Recommendation [INFERENCE] |
|---|---|---|---|
| Unrelated subscribers in today's shared instance | Lowest deployment count. | Settings, invitations, administration, support/recovery and accounting are instance-wide; business isolation is not defined by collection filtering. | Do not use as an accidental SaaS design. |
| One existing application instance per customer/household | Reuses current data model, local SQLite, worker, backup and permissions; clear billing/restore scope. | Repeated resident memory, provisioning/upgrades, host-wide failure and per-instance usage controls. | Five-customer paid pilot, measured before expansion. |
| Purpose-built shared-tenant app | Potentially denser operation at scale. | Tenant-scoped settings, users/invitations, jobs, assets, caches, billing, support, erasure and migrations must be designed and verified. | Only if measured economics or operations reject instance-per-customer. |

SQLite explicitly supports server-side application databases and notes that
separate database files can separate workloads. It still has one writer per
database; network filesystems and high write concurrency are reasons to choose
another topology/database. There is no need to switch to PostgreSQL merely
because payment exists. Do not infer a Clipshelf capacity figure from SQLite's
generic website examples. [SRC: SQLite][sqlite]

Docker's documentation describes namespace isolation and resource control but
also warns about kernel/configuration escape risk and the root-equivalent Docker
daemon surface. Separate containers are not separate kernels, dedicated VMs, or
high availability. Use distinct volumes/permissions/networks, least privilege,
CPU/RAM/PID/disk bounds, and extractor egress restrictions; keep provisioning
credentials outside customer-facing applications. [SRC: Docker][docker]

**ponytail:** manually provision at most five customer instances, with a measured
resource budget. Automate that deployment when operator time justifies it; change
tenancy only when measurements justify the extra authorization/migration surface.

The operator's home NAS/GPU is not the commercial failure domain. Each managed
instance uses public HTTPS and its own identity/secrets. Customers remain normal
application users; no Docker, Tailscale or model-key setup is required. A cloud
payer does not gain exceptional recovery or access to a household member's
Personal collection. **[INFERENCE]**

## 4. Distribution and project readiness

Clipshelf's own MIT license does not replace dependency licenses. gallery-dl's
license text is GPLv2; FFmpeg is LGPL 2.1-or-later, with optional GPL components
changing the resulting build's obligations. Inspect the **actual shipped versions,
Debian configuration and binary/source correspondence**, not just upstream names.
[SRC: gallery-dl][gallery-license], [FFmpeg][ffmpeg-license]

The FSF distinguishes aggregation from a combined program using both communication
mechanism and semantics; ordinary command-line/process boundaries generally favor
separate programs, but subprocesses/containers are not an automatic legal escape.
The current subprocess boundary supports an audit; it does not settle every
licensing question. Preserve notices and meet source obligations for each shipped
component before promoting the image/APK. [SRC: FSF FAQ][gpl-aggregate]

Minimum public-project work: clear supported release/upgrade instructions,
contribution expectations, responsible security reporting and response ownership,
a third-party inventory, release checksums/source identity, and permanent signed
APK downloads. GitHub supplies private vulnerability reporting for public
repositories; enable and monitor it rather than building a disclosure service.
Do not claim it is already enabled. [SRC: GitHub][security-reporting]

### Android distribution changes the purchase flow

Google permits distribution from a website without Play Billing. Play-distributed
apps selling digital cloud services are generally subject to its Payments policy;
consumption-only clients are allowed, while alternate payment links/options have
regional/program-specific conditions. Website checkout plus the existing signed
APK is the smallest pilot. Recheck current policy before a Play launch rather than
adding an unreviewed Stripe link to the app. [SRC: Google Play][play-billing]

If an app enables account creation, including directing users to an outside
creation flow, Google's deletion policy requires in-app and web deletion-request
paths, subject to the documented scope/exceptions. Account disabling is not a
replacement. Data-safety declarations and any permitted retention need explicit
review. [SRC: Google Play][play-deletion]

Android developer-verification requirements are also evolving: the official page
read on this date lists a September 30, 2026 regional participating-store milestone
and global expansion in 2027. Review the applicable distribution/registration path
before broad sideloaded release; neither assume sideloading is banned nor that a
signed APK alone satisfies every future distribution requirement.
[SRC: Android Developers][android-verification]

## 5. Dated cost inputs and pricing experiment

### Provider observations

These are comparison inputs, not selected vendors. Check current availability,
tax, region, agreements, product eligibility and actual usage before purchase.

| Component | Observed rate [SRC] | Important boundary |
|---|---|---|
| [DigitalOcean Droplet][do-vm] | 2 vCPU / 4 GiB / 80 GiB SSD / 4,000 GiB transfer: $24/month. | A price reference, **not** proof that five web/worker pairs or concurrent extractors fit. |
| [DigitalOcean Spaces][do-spaces] | $5/month includes 250 GiB and 1 TiB outbound; extra storage $0.02/GiB and transfer $0.01/GiB. | Private off-host backup candidate, not a live SQLite filesystem. Full backup generations multiply storage. |
| [Postmark Basic][postmark] | $15/month for 10,000 emails; extra $1.80/1,000. | Sender approval/domain authentication and real invitation/recovery delivery still need verification. Account logs/message retention also enter the privacy review. |
| [OpenAI standard pricing][openai-price] | `gpt-5.6-luna`: $0.20 input / $1.20 output per million tokens; `gpt-5.4-mini`: $0.75 / $4.50. | Price references only. Neither has passed Clipshelf's vision/JSON/accuracy evaluation here. Include all billed output/reasoning and actual image accounting. |
| [Paddle][paddle-price] | 5% + $0.50 per checkout transaction; page asks sellers below $10 or needing invoicing to discuss custom pricing. | Merchant-of-record comparison only. Its prohibited categories include **streaming downloaders** and infringement-enabling services; no assumption of Clipshelf eligibility. [AUP][paddle-aup] |
| [Stripe, German pricing page][stripe-price] | Standard EEA cards: 1.5% + EUR 0.25; Billing pay-as-you-go: 0.7% of billing volume; Tax Basic no-code: 0.5% per applicable transaction. | Different products/fees, not one all-inclusive rate. Ordinary Payments + Billing is not merchant-of-record service; eligibility, tax registrations/filing and further costs remain to assess. [Restrictions][stripe-restrictions] |

Hetzner remains a candidate, but its pricing page returned blank prices and
unavailable products in both the reader and rendered browser during this research.
Do not fill those cells with remembered cheap prices or claim a purchasable quote.
[SRC: Hetzner][hetzner]

### Image-heavy interpretation is not one fixed-cost bookmark

**[ASSUMPTION]** Each job uses 1,500 text-input tokens and 600 total billed
output tokens. Images are 1024 x 1024 at a compatible high-detail setting:
32 x 32 patches times the documented 1.2 multiplier, rounded up, gives 1,229
billable input tokens/image. Image resizing/detail/model can change that number;
compare actual provider usage before promising an allowance.
[SRC: image accounting][openai-vision]

For **300 jobs/month**, budget an additional **20% paid attempts** for
retries/failures. That retry assumption is separate from the image multiplier.
No caching, batch discount, paid transcription or extra model passes are assumed.

```text
input_tokens/job = 1,500 + images * 1,229
monthly_model_cost =
  300 * 1.20 * (input_tokens/job * input_rate + 600 * output_rate) / 1,000,000
```

| Hypothetical workload | Input tokens/job | Luna/month | Mini/month |
|---|---:|---:|---:|
| One image | 2,729 | $0.46 | $1.71 |
| Eight-image carousel | 11,332 | $1.08 | $4.03 |
| Twenty images/frames | 26,080 | $2.14 | $8.01 |

These calculations were executed, not measured against production traffic.
Large text, more frames, reasoning, retries beyond the allowance and model
escalations increase the bill. Acquisition CPU, retained video and support are
additional costs even when a model call is cheap or a capture ultimately fails.

### Small-pilot worksheet, not a price recommendation

**[ASSUMPTION]** Five customers share a $24 VM, $5 backup tier, $15 mail plan
and a $5/month domain/monitoring budget: **$49/month fixed pool**. Assume ten
support minutes/customer/month valued at $30/hour, or **$5/customer**.
Assume each uses the eight-image workload above. Use 5% + $0.50 solely as an
illustrative payment-fee benchmark, not a decision that Paddle can serve us.

```text
contribution/customer = price - (price * 0.05 + 0.50)
                       - 49 / 5 - 5 - monthly_model_cost
```

| Hypothetical monthly price | Contribution with Luna | Contribution with Mini |
|---|---:|---:|
| $12 | -$4.98 | -$7.93 |
| $19 | $1.67 | -$1.28 |

This excludes taxes, FX, refunds, setup/development, legal/accounting and costs
above those base tiers; it is **not net profit**. Resident-memory capacity,
customer count, retention and support time are unmeasured. A larger VM, mature
backup set or more support makes these figures worse. Replacing assumptions with
measured costs and validating willingness to pay matters more than choosing a
competitor's headline price. Start monthly; no annual lock-in or lifetime deal
before the economics and exit path are proven. **[INFERENCE]**

Storage compounds: at an assumed **8 MB retained/job**, 300 jobs add **2.4 GB
per customer/month**. A nominal 10 GB allowance fills in about **4.2 months**
from empty without deletion; video-heavy use may be much larger. Thirty full
10 GB backups alone occupy 300 GB, already above the quoted backup tier.
Measure retained bytes, temporary extraction peaks and backup generations;
do not sell indefinite unlimited retention against a small fixed disk.

## 6. Privacy, payment and platform constraints

### Legal/product work before selling

Business jurisdiction and customer markets are unknown. Determine them before
selecting tax treatment, notices, contracts or a payment provider; an EU VM does
not by itself resolve jurisdiction or international data transfers.
If GDPR applies, relevant provisions include territorial scope (article 3),
lawfulness/minimization/retention (5–6), notices (13–14), qualified erasure and
portability rights (17 and 20), processor contracts (28), security (32), breach
notification (33–34) and international transfers (chapter V).
[SRC: GDPR][gdpr]

The implementation review must map accounts, source URLs/content, prompts,
images/video, model requests, SMTP payloads/logs, payments, operational logs and
backups to purposes, recipients, retention, access and deletion. Determine
controller/processor roles per purpose. A merchant of record handles its agreed
payment/tax functions, not Clipshelf's content privacy, security or support.
Hosting operators can technically access stored data; do not claim zero knowledge.
**[INFERENCE]**

The current disable-only account model is not erasure. A tested, authorized
operator-mediated export/erasure workflow can serve five customers, but must
cover individual household members, Personal collections, shared provenance,
in-flight jobs, provider data and backup expiry without removing other members'
legitimate material. A disaster restore must not resurrect erased data into
service. Product export should be useful beyond minimum statutory portability;
never substitute a database/secret dump. **[INFERENCE]**

EU guidance describes a general 14-day withdrawal period for online services,
with defined exceptions, and continuing obligations for digital services.
Do not copy a one-off downloadable-content waiver into a recurring hosting
subscription. Obtain applicable advice on pre-contract disclosure, renewal,
refunds, cancellation and any national requirements before taking payment.
[SRC: withdrawal][eu-withdrawal], [digital-service guarantees][eu-guarantees]

Proposed closure schedule for owner/legal review: reading/export for 30 days
after paid service ends, then live deletion and backup expiry within a further
30 days. Valid erasure requests and justified legal retention have separate
handling. Notify users, stop new expensive work and show actual state; never
let a payment webhook immediately destroy a library. These periods are product
proposals, not statutory defaults. **[INFERENCE]**

### TikTok permission is a release gate, not an extractor retry

The EEA/UK/CH terms inspected, updated July 2026, prohibit extracting platform
data/content with automated software not supplied by TikTok or approved by it
in writing. Public accessibility and a technically successful gallery-dl or
yt-dlp fetch do not establish permission to operate a commercial acquisition
service. Terms, copyright permissions and applicable legal exceptions need
qualified assessment for the actual offering and market.
[SRC: TikTok terms][tiktok-terms]

Paddle explicitly lists streaming downloaders among prohibited categories;
Stripe also has restricted/prohibited-business and IP-related rules.
Resolve product-specific eligibility with prospective providers rather than
assuming "SaaS" approval or hiding the acquisition behavior. Review model and
hosting policies and permitted content too. [SRC: Paddle][paddle-aup],
[Stripe][stripe-restrictions]

If automatic acquisition cannot be offered lawfully and reliably, offer only
links/metadata and specifically authorized imports, with truthful feature
disclosure. A browser import is **not** a blanket legal workaround. Revalidate
customer demand for that narrower hosted offer before proceeding; keep the
self-hosted contract unchanged unless the owner approves a separate change.
**[INFERENCE]**

## 7. Decisions and evidence still needed

The [server plan, sections 10–12](server-plan.md) contains the integrated offer,
isolation contract, usage/payment state requirements and sequenced acceptance
gates. This research approves no new runtime behavior or spending.

| Decision/evidence | Before |
|---|---|
| Accept MIT plus paid operation; define customer segment and validate willingness to pay against actual supported acquisition. | Building hosted-only automation or publishing a price. |
| Seller jurisdiction, launch markets, applicable privacy/consumer/tax duties, TikTok permission and host/model/mail/payment eligibility. | Taking payment or promising supported content. |
| Approve hosted exceptions for scoped erasure, retention, portable export/import, public ingress and cost control. | Admitting paid pilot users; existing self-hosted rules remain intact. |
| Measure two-instance isolation, load, resident/peak memory, retained bytes, backup growth, model accuracy/cost and support time. | Choosing VM density, storage/job limits and final price. |
| Complete existing SMTP, fresh-shortlink, real-phone, NAS and deployed recovery gates; additionally prove commercial-region acquisition, public HTTPS and off-host recovery. | Claiming either distribution/deployment or hosted launch ready. |
| Complete at least one paid renewal cycle with acceptable measured contribution and real continued use, including cancellation/export. | Automating self-service or expanding beyond five customers. |

No customer interviews, production load run, cloud purchase, provider approval,
trademark search or jurisdiction-specific legal opinion was performed. Pricing
pages establish dated offers only; no subscription or billing implementation was
added. Recheck time-sensitive prices and policies before commitment.

[sqlite]: https://www.sqlite.org/whentouse.html
[docker]: https://docs.docker.com/engine/security/
[gallery-license]: https://raw.githubusercontent.com/mikf/gallery-dl/master/LICENSE
[ffmpeg-license]: https://ffmpeg.org/legal.html
[gpl-aggregate]: https://www.gnu.org/licenses/gpl-faq.html#MereAggregation
[security-reporting]: https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository
[play-billing]: https://support.google.com/googleplay/android-developer/answer/10281818?hl=en
[play-deletion]: https://support.google.com/googleplay/android-developer/answer/13327111?hl=en
[android-verification]: https://developer.android.com/developer-verification
[mit]: https://choosealicense.com/licenses/mit/
[agpl]: https://www.gnu.org/licenses/agpl-3.0.en.html
[osd]: https://opensource.org/osd
[linkwarden]: https://linkwarden.app/pricing
[karakeep]: https://karakeep.app/pricing/
[karakeep-license]: https://raw.githubusercontent.com/karakeep-app/karakeep/main/LICENSE
[wallabag]: https://www.wallabag.it/en/pricing/
[wallabag-license]: https://api.github.com/repos/wallabag/wallabag
[do-vm]: https://www.digitalocean.com/pricing/droplets
[do-spaces]: https://www.digitalocean.com/pricing/spaces-object-storage
[postmark]: https://postmarkapp.com/pricing
[openai-price]: https://developers.openai.com/api/docs/pricing
[openai-vision]: https://developers.openai.com/api/docs/guides/images-vision
[paddle-price]: https://www.paddle.com/pricing
[paddle-aup]: https://www.paddle.com/help/start/intro-to-paddle/what-am-i-not-allowed-to-sell-on-paddle
[stripe-price]: https://stripe.com/en-de/pricing
[stripe-restrictions]: https://stripe.com/legal/restricted-businesses
[hetzner]: https://www.hetzner.com/cloud/regular-performance/
[gdpr]: https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679
[eu-withdrawal]: https://europa.eu/youreurope/citizens/consumers/shopping/returns/index_en.htm
[eu-guarantees]: https://europa.eu/youreurope/citizens/consumers/shopping/guarantees/index_en.htm
[tiktok-terms]: https://www.tiktok.com/legal/page/eea/terms-of-service/en
