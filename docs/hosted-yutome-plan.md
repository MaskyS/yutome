# Hosted Yutome Plan: Polar + Gemini + Webshare + Cloudflare

Last updated: 2026-05-25

## Purpose

Make Yutome useful for non-expert users and easy for hosters by replacing the current user-owned setup path with a hosted path:

- User signs into a Yutome frontend.
- Yutome owns and meters Polar billing, Gemini AI, Webshare residential proxying, hosted connector infrastructure, hosted ingest execution, and the chosen hosted search/storage substrate.
- Users do not create Cloudflare, Gemini, Voyage, or Webshare accounts in the normal path.
- Advanced users may still use the local-first/BYO mode, but that is not the default hosted onboarding.

## Current Product Reality

Current `yutome setup` is a local CLI wizard with six user-facing steps:

1. Project setup and local SQLite catalog creation.
2. Webshare residential proxy setup.
3. Gemini transcript repair/fallback setup.
4. Semantic search setup through Voyage + LanceDB.
5. YouTube source import and first sync.
6. Assistant connection through local MCP or a user-deployed Cloudflare Worker.

Current storage/search shape:

- SQLite is the system of record for channels, library sources, videos, transcript versions, chunks, embedding status, transcript attempts, and jobs.
- SQLite FTS5 powers lexical chunk/video search, `bm25`, snippets, raw FTS mode, and literal phrase escaping.
- LanceDB stores chunk embedding rows plus a subset of chunk/video/transcript metadata.
- Hybrid search uses LanceDB hybrid search over vectors + text, then enriches and validates rows against SQLite.
- Semantic/hybrid query embeddings are generated through Voyage.

Current remote connector shape:

- `yutome connect --deploy` deploys a Cloudflare Worker into the user's own Cloudflare account.
- The Worker exposes `/mcp` with OAuth and forwards MCP tool/resource calls to a local `yutome serve bridge` process over a Durable Object WebSocket relay.
- The corpus stays local in connector-only mode.
- Replica/always-on search is planned but not implemented as a hosted default.

## Target Hosted UX

The hosted path should feel like:

1. Create Yutome account.
2. Choose a plan or add credits.
3. Connect YouTube sources.
4. Click "Connect to ChatGPT" or "Connect to Claude."
5. Ask questions in the assistant.

The user should not see Cloudflare Workers, Wrangler, D1, Vectorize, Webshare credentials, Gemini keys, or embedding-provider setup during basic onboarding.

## Architecture Decision Summary

Recommended V1 hosted product:

- Billing: Polar subscriptions and prepaid credits.
- AI: Gemini for transcript cleanup and fallback transcription/video understanding.
- Proxying: Webshare rotating residential, brokered by Yutome with per-user sub-users or equivalent bandwidth attribution.
- Cloud: Cloudflare for the public frontend, auth/session edge, remote MCP connector, lightweight scheduler triggers, and R2 artifact storage where useful. Do not run media ingest, `yt-dlp`, or VectorChord inside Cloudflare Workers.
- Runtime: Railway is the default hosted deployment for the API, always-on worker, cron tick, and initial Postgres deployment. Postgres is the durable job/schedule source of truth. Modal is optional later for bursty backfills or expensive media fallback jobs; Fly Machines are a lower-priority worker fallback.
- Search/storage: keep SQLite + LanceDB locally until the hosted refactor lands, but target **one hosted canonical database** for V1. The default candidate is Postgres + VectorChord Suite, deployed on Railway as a custom Postgres service unless a managed VectorChord-capable host is selected. Managed Postgres + `pgvector` + built-in Postgres FTS is the fallback if VectorChord database operations are not ready for paid production. D1 + Vectorize, Turbopuffer, Typesense, Weaviate, and OpenSearch remain bakeoff controls, but they either split catalog/search or require document-store consistency tradeoffs.
- Embeddings: use `voyage-4-lite` for hosted semantic search unless evals produce strong evidence that another model is materially better on speed, cost, and retrieval accuracy. Current Yutome already uses `voyage-4-lite`, so this preserves ranking behavior and avoids changing two variables at once.

Hosted embedding candidates:

- **Voyage `voyage-4-lite`**: default. It is the current local model, costs $0.02/M input tokens after the account free allowance, supports 1024-dimensional output as configured today, and has a known Yutome code path. Source: [Voyage pricing](https://docs.voyageai.com/docs/pricing).
- **Cloudflare Workers AI embeddings**: lower same-cloud operational friction and some cheaper models, but only after evals show acceptable quality and speed. Examples on Cloudflare pricing include `bge-m3` and `qwen3-embedding-0.6b` at $0.012/M input tokens, `bge-small-en-v1.5` at $0.020/M input tokens, and `bge-reranker-base` at $0.003/M input tokens. Source: [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).
- **Gemini Embedding**: useful if provider consolidation around Gemini matters more than embedding COGS. Google lists `gemini-embedding-001` at $0.15/M input tokens standard and $0.075/M input tokens batch; `gemini-embedding-2` supports multimodal embedding but is much more expensive for audio/video. Source: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

Decision for hosted V1: keep `voyage-4-lite` as the embedding default. Treat Workers AI and Gemini Embedding as explicit benchmark candidates, not default replacements.

Non-goals for hosted V1:

- Do not expose provider account setup to normal users.
- Do not give every user a raw Gemini, Webshare, or Cloudflare credential.
- Do not promise per-user provider free tiers; provider free/included usage is account-level margin buffer.
- Do not switch storage/search substrates until retrieval parity is testable.

## Hosted Control Plane

Hosted Yutome should split into three layers:

1. **Control plane**: Yutome account, workspace, billing, entitlements, job orchestration, provider allocations, audit logs, and connector registration.
2. **Ingest plane**: YouTube discovery, transcript acquisition, Webshare proxy use, Gemini repair/fallback, chunking, embedding, and corpus synchronization.
3. **Query plane**: MCP tools, search APIs, citation/resource reads, hosted replica reads, and optionally laptop bridge forwarding.

Every cost-bearing operation crosses an entitlement gate:

```text
request -> estimate -> reserve credits -> execute -> collect provider usage -> settle -> emit usage event
```

Jobs that cannot reserve enough credits should not start. Jobs that exhaust reserved credits mid-run should pause cleanly with a resumable state.

## Section A: Billing, Entitlements, And Provider Usage

<!-- SUBAGENT-BILLING-START -->
### Decision

Use Polar for human/org checkout, subscriptions, customer portal, prepaid top-ups, Merchant-of-Record tax handling, and optional customer-visible usage meters. Keep Yutome's own usage ledger as the enforcement source of truth for both humans and agents. The hosted product has expensive and bursty provider work, so Yutome must block or pause work before calling Gemini, Webshare, Voyage, or hosted search/storage infrastructure, not after an invoice is generated.

The safe V1 is credits-only spending: do not attach Polar metered prices that create automatic overage invoices until real usage distributions are known. Polar's Credits docs explicitly support prepaid usage and say credits can be issued through subscription products or one-time products; they also state that Polar does not block usage when a customer exceeds balance, so the application must enforce that itself. Sources: [Polar credits](https://polar.sh/docs/features/usage-based-billing/credits), [Polar usage billing introduction](https://polar.sh/docs/features/usage-based-billing/introduction).

### Polar / Stripe Reality Check

Polar does still use Stripe behind the scenes. Polar's Merchant of Record docs say Polar is built on Stripe, and Polar's payment-processor disclosure says Stripe provides the payment-processing infrastructure for Polar transactions, including card processing, authorization, settlement, fraud/risk, refunds, chargebacks, and Stripe Connect payouts. Sources: [Polar MoR introduction](https://docs.polar.sh/merchant-of-record/introduction), [Polar payment processor partners](https://polar.sh/legal/payment-processor-partners).

That does not make Polar "just Stripe." The distinction is:

- **Stripe direct:** Stripe is a payment service provider. It gives lower-level payment, billing, tax-calculation, invoice, and subscription APIs, but Yutome remains the merchant and is responsible for international sales-tax registration, filing, and remittance unless it separately builds or buys that compliance layer.
- **Polar:** Polar is the Merchant of Record/reseller layer above Stripe. Polar handles checkout, product/order/subscription abstractions, customer portal, and international sales-tax liability for sales through Polar. Polar still relies on Stripe for card rails and payouts.

Why Polar is better for Yutome V1:

- Faster launch: fewer billing primitives to assemble than direct Stripe Billing + Stripe Tax + customer portal + invoice/receipt/payment-method recovery wiring.
- Merchant-of-Record posture: Polar takes on sales-tax/VAT/GST collection and remittance for Polar sales; Yutome still owes its own income/revenue tax.
- Customer portal: customers can manage subscriptions, payment methods, invoices/receipts, cancellations, benefits, and meter visibility without Yutome storing card data. Polar says the hosted portal cannot be fully disabled because it is part of cancellation, receipt, and failed-payment recovery obligations. Source: [Polar customer portal](https://docs.polar.sh/features/customer-portal).
- Usage/credits abstraction: Polar can expose credits and customer meter state in its API/portal, but Yutome can still enforce credits internally.

Why Polar may be worse than direct Stripe:

- Higher fees and less flexibility than raw Stripe. Current Polar public fees are Starter `5% + $0.50`, Pro `$20/mo + 3.8% + $0.40`, Growth `$100/mo + 3.6% + $0.35`, and Scale `$400/mo + 3.4% + $0.30`, plus `+1.5%` for international cards. Current docs also say organizations created before **May 27, 2026** stay on the Early Member rate; because this plan is being written on **May 25, 2026**, create the Polar organization immediately if we want that option. Source: [Polar fees](https://docs.polar.sh/merchant-of-record/fees).
- Stripe is still in the stack for payment processing, Connect/KYC, payouts, disputes, and card-network behavior. Polar reduces Yutome's billing integration and tax burden; it does not remove payment-rail risk.
- MoR sales tax can mean more customers are charged tax than if Yutome waited to cross thresholds directly. Polar's own docs call this out as a tradeoff.
- Polar is not a machine-payment rail. It does not make an autonomous agent a financially liable customer with its own bank/card/crypto wallet in V1.
- Polar is not a runtime authorization service. Its docs explicitly say it does not block usage when customers exceed credits, so Yutome must hard-cap before provider calls.

### Payer Model: Humans, Organizations, And Agents

Polar customers should represent **billing payers**, not every actor that can spend.

Recommended Yutome model:

```text
Polar customer
  -> Yutome billing_account
      -> one or more workspaces
      -> one or more credit wallets
      -> human principals and agent principals with spend policies
```

Human payer examples:

- Individual user buys Starter/Pro for a personal workspace.
- Organization admin buys Pro/Team for a shared workspace.
- Human/org admin buys one-time AI or Reliable Fetch top-ups.

Agent payer examples:

- An MCP agent, cron agent, or external automation spends from a workspace wallet allocated by a human/org payer.
- An agent can request a top-up link, but the human/org payer approves and pays through Polar.
- Later, an agent may fund a wallet through x402/stablecoin/Stripe machine payments, but Yutome still converts that funding into the same internal wallet ledger.

This means "agents are paying users" in product terms, but not necessarily Polar customers. They are **spending principals** with delegated budgets, scopes, and audit trails under a payer account. If a legally incorporated "agent company" has an email, payment method, and tax identity, it can be a normal Polar customer; most autonomous agents should not be.

### Polar Implementation Model

Create Polar products:

- `Yutome Starter` monthly subscription: grants monthly included entitlements.
- `Yutome Pro` monthly subscription: grants larger monthly included entitlements and higher concurrency caps.
- `AI Credits Top-Up`: one-time product that grants AI credits.
- `Reliable Fetch GB Top-Up`: one-time product that grants Webshare-backed fetch bandwidth.
- Later: `Hosted Search Pack` if always-on hosted search becomes materially expensive.

Create Polar meters:

- `ai_credits`: product unit for Gemini, Voyage, embedding/rerank, and other AI provider spend.
- `fetch_mib`: product unit for Webshare-backed fetching.
- `hosted_search_queries`: bundled query allowance if Section B chooses an always-on hosted search backend.
- `connector_requests`: optional meter for heavy hosted MCP usage.

Use Polar Credits benefits on subscription and one-time products where the balance should be visible in Polar. A monthly subscription grant maps to the start of each billing cycle; a one-time top-up grants credits once at purchase. If we want no surprise overage charges, configure credits-only behavior by not creating a metered price for that meter. Source: [Polar credits](https://polar.sh/docs/features/usage-based-billing/credits).

Use Polar customer fields this way:

- `external_id`: Yutome `billing_account_id`, not an agent id and not necessarily a user id. If V1 enforces exactly one payer per workspace, `billing_account_id` can equal `workspace_id`; otherwise keep it separate so one organization can pay for multiple workspaces.
- `customer_metadata`: `workspace_id`, `payer_type = individual|organization`, `plan_source`, and migration/debug metadata.
- Polar's own customer id: store as reconciliation metadata, but do not make it the primary key for internal authorization.

Create checkout sessions server-side:

1. Resolve payer and workspace.
2. Create or reuse Polar customer by `external_id`.
3. Create checkout for subscription or top-up product.
4. Include metadata linking the checkout/order to `billing_account_id`, `workspace_id`, `wallet_id`, intended credit grant, and idempotency key.
5. After payment, grant credits in Yutome only from trusted webhook/API confirmation.

Listen to Polar webhooks and process them idempotently:

- `order.paid`: grant one-time top-ups and confirm paid subscription-cycle orders.
- `order.created` with `billing_reason = subscription_cycle`: begin renewal reconciliation, but do not grant irreversible credits until payment is paid.
- `subscription.active`, `subscription.updated`, `subscription.past_due`, `subscription.canceled`, `subscription.revoked`: update plan state and paid entitlement gates.
- `customer.state_changed`: reconcile active subscriptions, benefits, and Polar-side meter balances.

Source: [Polar webhook events](https://polar.sh/docs/integrate/webhooks/events).

Webhook rules:

- Store every webhook by provider event id and payload hash before applying effects.
- Apply credit grants exactly once per `order_id`, `billing_reason`, product, and billing period.
- Treat `order.created` renewal orders as pending; grant irreversible subscription-cycle credits after `order.paid`.
- On `subscription.past_due`, keep existing prepaid credits usable but stop granting new monthly included credits until payment recovers.
- On `subscription.revoked`, remove subscription-only capabilities and future grants, but do not delete purchased/top-up credits unless the refund/chargeback policy requires it.
- On refund or dispute events, reduce balances only according to an explicit policy, because provider work may already have been consumed.

Keep in Polar:

- Customer identity, checkout sessions, subscriptions, orders, refunds, portal access, product catalog, and optional product-unit meter balances.
- Product-level meters such as `ai_credits`, `fetch_mib`, and `hosted_search_queries` if we want Polar's customer state and portal to reflect usage.

Keep in Yutome:

- Provider-native usage details: Gemini model, route, modality token counts, cached tokens, cache token-hours, output tokens, Webshare bytes, Webshare sub-user, Cloudflare request/CPU/row/storage/vector/container units, provider request IDs, retries, errors, and raw estimated provider cost.
- Reservations and hard-cap enforcement. Polar is not the authorization system for a Gemini call or Webshare request.
- Provider free-tier allocation. Provider free tiers are account-level host margin buffers, not per-user entitlements.

Send to Polar only settled product-unit events, not raw provider traces. Example: after a Gemini cleanup job completes, Yutome records full provider detail internally, then optionally sends Polar `ai_credits` consumption. If Polar ingestion is delayed or down, Yutome's ledger remains authoritative and reconciles later.

### Human And Agent Billing Flows

Human signup flow:

```text
user creates Yutome account
  -> creates personal workspace or joins org workspace
  -> selects plan/top-up in Yutome UI
  -> Yutome creates Polar checkout session
  -> human pays through Polar
  -> Polar webhook confirms order/subscription
  -> Yutome grants wallet credits and plan entitlements
```

Agent setup flow:

```text
human/org admin creates agent principal
  -> assigns scopes and spend policy
  -> allocates wallet budget
  -> agent authenticates by API key/OAuth client
  -> every request estimates, reserves, executes, and settles from the Yutome wallet
```

Agent top-up flow:

```text
agent hits insufficient_credits
  -> Yutome returns structured error with required credits and top-up URL/request id
  -> human/org payer approves checkout in Polar
  -> Yutome grants credits to the wallet after order.paid
  -> agent retries the job
```

Do not make Polar webhooks unlock an agent's scopes. Scopes are a Yutome authorization concern; Polar only changes payer balance and subscription status.

### Polar Lock-In And Migration Risks

Use Polar to move fast, but keep Yutome's billing model portable.

Known implications:

- Product and pricing shape should be treated as mostly append-only. If we change billing cycles, pricing types, or entitlement shapes, create new Polar products and migrate users deliberately rather than mutating historical product meaning.
- Meter definitions are schema. Do not create fine-grained provider meters in Polar too early. Keep provider-native usage in Yutome and mirror only stable product units like `ai_credits`, `fetch_mib`, and `hosted_search_queries`.
- Do not assume payment methods or active subscriptions can be ported to another provider. If Yutome later moves from Polar to Stripe/Paddle/Dodo, expect a customer re-checkout campaign unless Polar gives an explicit migration path.
- Polar's customer portal is always available and not fully brand-customizable. Build Yutome's own usage/budget UI for product clarity, but link to Polar for invoices, receipts, payment methods, cancellation, and failed-payment recovery.
- Usage billing is good for customer-visible accounting, not fraud prevention. Yutome's reserve/settle ledger must remain the runtime guard because provider calls can create cost faster than a billing webhook can recover money.
- Refunds, disputes, and chargebacks can happen after provider cost is already incurred. Keep a margin buffer in credit pricing and reserve policy; do not sell provider pass-through at zero margin.
- Stripe remains an operational dependency through Polar. If Stripe blocks a payment method, payout, account, dispute process, or supported country, Polar cannot fully abstract that away.

Practical portability rule: every Polar id is metadata on a Yutome-owned payer, wallet, credit grant, usage event, and invoice reconciliation row. No provider call should require a live Polar API call in the hot path.

### User-Facing Entitlements

Expose simple units:

- `AI credits`: used for Gemini cleanup/fallback, Voyage embeddings, and any alternate embedding/reranking provider selected by the search plan. Recommended display unit: `1,000 AI credits = $1.00 of Yutome AI allowance`.
- `Reliable Fetch GB`: Webshare rotating residential bandwidth. Internal unit should be MiB or bytes; UI can show GB.
- `Hosted Connector`: included request/session allowance bundled in the subscription.
- `Hosted Search`: included query allowance bundled in the subscription until Section B picks the hosted search substrate.

Do not expose Gemini tokens, Webshare usernames, Cloudflare rows, Vectorize dimensions, or Durable Object GB-seconds in normal UI. Show estimates in product units: "This channel is estimated to use 0.7 Reliable Fetch GB and 180 AI credits."

### Gemini Usage Plan

Hosted Yutome should use paid Gemini API projects, not free-tier quota. Google states unpaid Gemini API quota may be used to improve Google products and warns not to submit sensitive, confidential, or personal information to unpaid services; paid services are not used to improve Google products. The Gemini terms also say user-facing API clients in the EEA, Switzerland, or UK must use Paid Services. Sources: [Gemini API terms](https://ai.google.dev/gemini-api/terms), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

Recommended routes:

- Realtime small cleanup: Gemini standard endpoint.
- Background library backfills and non-urgent transcript repair: Gemini Batch API.
- Avoid Search Grounding for transcript cleanup. It adds separate grounded-result restrictions and search-query pricing that Yutome does not need for transcript repair.

Current relevant paid prices:

- `gemini-2.5-flash-lite`: standard input is $0.10/M text/image/video tokens and $0.30/M audio tokens; output is $0.40/M. Batch is $0.05/M text/image/video input, $0.15/M audio input, and $0.20/M output.
- `gemini-2.5-flash`: standard input is $0.30/M text/image/video tokens and $1.00/M audio tokens; output is $2.50/M. Batch is $0.15/M text/image/video input, $0.50/M audio input, and $1.25/M output.
- `gemini-3.1-flash-lite`: standard input is $0.25/M text/image/video tokens and $0.50/M audio tokens; output is $1.50/M. Batch/flex input is $0.125/M text/image/video tokens and $0.25/M audio tokens; output is $0.75/M.

Source: [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

Meter Gemini internally by:

- `model`
- `route`: `standard`, `batch`, `flex`, or `priority`
- `input_text_tokens`
- `input_image_tokens`
- `input_video_tokens`
- `input_audio_tokens`
- `cached_input_tokens`
- `cache_storage_token_hours`
- `output_tokens`
- `request_count`
- `batch_job_name`
- `provider_error_code`

For estimates, use provider token counts when available and conservative modality estimates otherwise. Google's docs describe audio as tokenized at 32 tokens/second; video is roughly 300 tokens/second at default media resolution or roughly 100 tokens/second at low media resolution. Sources: [Gemini audio understanding](https://ai.google.dev/gemini-api/docs/audio), [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding).

Batch constraints:

- Batch is priced at 50% of equivalent interactive API cost.
- Target turnaround is 24 hours.
- Inline requests must stay under the request-size limit; larger jobs should use JSONL input files, with a 2 GB input file limit.
- Batch creation is not idempotent, so Yutome must store its own `batch_job_name` and dedupe submission by job/reservation ID.

Source: [Gemini Batch API](https://ai.google.dev/gemini-api/docs/batch-api).

Rate-limit constraints:

- Gemini rate limits are per Google Cloud project, not per API key.
- Limits are measured by RPM, TPM, and RPD, vary by model/tier, and are not guaranteed.
- Yutome needs per-workspace throttles so one workspace cannot consume the shared project quota.

Source: [Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

### Webshare Usage Plan

Use Webshare Rotating Residential for hosted reliable fetching. Its pricing unit is bandwidth, not proxy count. Current monthly public pricing starts at 1 GB for $3.50/mo, then 10 GB for $27.50, 25 GB for $65, 50 GB for $122.50, 100 GB for $225, 250 GB for $500, 500 GB for $875, 1,000 GB for $1,500, and 3,000 GB for $4,200, so the effective rate ranges from $3.50/GB down to $1.40/GB. Source: [Webshare pricing](https://www.webshare.io/pricing).

Use one Webshare sub-user per Yutome workspace for hosted mode when possible:

- Webshare sub-users can have bandwidth limits, thread limits, separate credentials, and proxy-list customization.
- The API exposes `proxy_limit` in GB, `max_thread_count`, bandwidth window fields, aggregate stats, and `X-Subuser` masquerading for proxy APIs.

Sources: [Webshare sub-user help](https://help.webshare.io/en/articles/8448255-how-to-create-a-sub-user), [Webshare Sub-Users API](https://apidocs.webshare.io/subuser).

Meter Webshare internally by raw bytes, not requests:

- `subuser_id`
- `proxy_username`
- `bytes`
- `request_duration`
- `hostname` / `domain`
- `error_reason`
- `video_id` / `source_id`
- `job_id`

Webshare's Proxy Activity object defines `bytes` as downloaded plus uploaded data for authenticated proxy requests. Source: [Webshare Proxy Activity API](https://apidocs.webshare.io/proxystats/activity_object).

Operational constraints:

- Residential bandwidth resets monthly and does not roll over.
- Exceeding bandwidth stops proxies until upgrade, renewal, or reset.
- Backbone residential proxy connections use `p.webshare.io`; username suffixes can control country, city, sticky session, and rotation.
- Current local Yutome retry behavior can multiply proxy bandwidth, so retries must be charged to the job that caused them unless the failure is clearly provider-side.

Sources: [Webshare bandwidth limits](https://help.webshare.io/en/articles/8370524-how-does-the-bandwidth-limit-work), [Webshare proxy connection API](https://apidocs.webshare.io/proxy-connection).

### Cloudflare Usage Plan

Use Cloudflare as hosted app, auth/session edge, MCP connector, lightweight scheduler trigger, R2 artifact storage, and a possible hosted-search fallback if the bakeoff chooses a Cloudflare-native path. Cloudflare costs should mostly be bundled into the subscription until usage is large; still record raw usage so heavy workspaces can be capped or moved to a higher tier.

Track these Cloudflare units:

- Workers: requests and CPU milliseconds. Workers Paid is $5/mo minimum, includes 10M requests/mo and 30M CPU-ms/mo, then $0.30/M requests and $0.02/M CPU-ms. Source: [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).
- D1: rows read, rows written, and storage GB-month. Paid includes 25B rows read/mo, 50M rows written/mo, and 5 GB storage, then $0.001/M rows read, $1.00/M rows written, and $0.75/GB-month. Each query returns `meta.rows_read` and `meta.rows_written`, which Yutome can attach to request/job IDs. Source: [D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).
- R2: storage GB-month, Class A operations, and Class B operations. Standard storage is $0.015/GB-month, Class A is $4.50/M requests, Class B is $0.36/M requests, and direct R2 egress is free. Source: [R2 pricing](https://developers.cloudflare.com/r2/pricing/).
- Vectorize: stored vector dimensions and queried vector dimensions. Paid includes 50M queried dimensions/mo and 10M stored dimensions, then $0.01/M queried dimensions and $0.05/100M stored dimensions. Source: [Vectorize pricing](https://developers.cloudflare.com/vectorize/platform/pricing/).
- Durable Objects: requests, WebSocket message-equivalent requests, duration GB-seconds, and SQLite storage rows/storage if used. Paid includes 1M requests/mo and 400,000 GB-s/mo, then $0.15/M requests and $12.50/M GB-s. Incoming WebSocket messages are billed with a 20:1 ratio, and WebSocket hibernation is important for idle connector sessions. Source: [Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).
- Containers: memory GiB-seconds, vCPU-seconds, disk GB-seconds, and container network egress if Section B chooses LanceDB-in-container. Workers Paid includes 25 GiB-hours memory/mo, 375 vCPU-min/mo, and 200 GB-hours disk/mo, then $0.0000025/GiB-second, $0.000020/vCPU-second, and $0.00000007/GB-second. Source: [Containers pricing](https://developers.cloudflare.com/containers/pricing/).
- Workers AI, if used for embeddings/reranking instead of Gemini/Voyage: embeddings are priced per input token through neurons; examples include `@cf/baai/bge-small-en-v1.5` at $0.020/M input tokens, `@cf/baai/bge-m3` at $0.012/M input tokens, and `@cf/baai/bge-reranker-base` at $0.003/M input tokens. Source: [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).

For MVP billing, do not expose Cloudflare line items to users unless they exceed plan limits. Expose:

- `Hosted Connector included`
- `Assistant requests this month`
- `Hosted search queries this month`
- `Storage used`

Internally, allocate account-level included Cloudflare allowances across workspaces after the fact for margin analysis, but enforce caps on raw usage before account-level overages become material.

### Reservation And Hard-Cap Flow

Every cost-bearing operation uses the same flow:

1. Estimate provider units before work starts.
2. Convert estimated units to product entitlements.
3. Reserve from the Yutome ledger.
4. Refuse or pause the job if the workspace lacks balance or hits monthly hard caps.
5. Execute provider work.
6. Record provider-native actual usage.
7. Settle the reservation by charging actual product units and refunding unused reserve.
8. Optionally mirror settled product-unit usage to Polar.

Example estimates:

```text
Gemini realtime cost =
  text_video_input_tokens / 1_000_000 * model_text_video_rate
+ audio_input_tokens / 1_000_000 * model_audio_rate
+ output_token_cap / 1_000_000 * model_output_rate

Webshare reserve =
  estimated_proxy_bytes / 1_GiB * retail_fetch_gb_price

Vectorize query reserve =
  ((stored_vectors_scanned + query_vectors) * dimensions) / 1_000_000 * vectorize_query_rate

Turbopuffer query reserve =
  max(namespace_logical_bytes, 1.28_GB) * logical_bytes_queried_rate
+ estimated_returned_logical_bytes * logical_bytes_returned_rate
+ subquery_count * safety_margin
```

Hard caps:

- Workspace monthly dollar-equivalent cap.
- AI credit balance and optional per-day AI spend cap.
- Reliable Fetch GB balance and per-job max GB cap.
- Webshare sub-user `proxy_limit` and `max_thread_count`.
- Gemini per-workspace request/token throttle layered under the shared project limit.
- Cloudflare per-workspace assistant request and hosted search query limits.
- Maximum outstanding batch reservations per workspace.

Refund policy:

- If Yutome rejects work before provider execution, release the reservation.
- If Gemini/Webshare/Cloudflare charges for partially completed provider work, charge the actual consumed provider units even if the user-facing job failed.
- If failure is caused by a Yutome bug or provider outage and provider cost is negligible or recoverable, release or credit back at Yutome's discretion.

### Implementation Notes

- Add a provider gateway around Gemini, Webshare proxy dispatch, Voyage, and hosted search/storage operations so no direct provider or service call bypasses `usage_reservations`.
- Store all provider usage in `usage_events` with `provider`, `capability`, `unit`, `quantity`, `raw_cost_usd`, and `metadata_json`.
- Store user-facing balances in `credit_balances`; keep Polar customer/meter IDs as reconciliation metadata, not the only balance source.
- Add a nightly reconciliation job that compares Yutome ledger totals with Polar customer state, Gemini billing export/API usage, Webshare sub-user stats, and Cloudflare analytics.
- Admin UI must show margin by workspace: revenue, included entitlement grants, consumed provider cost, retries, failed-provider cost, and remaining hard-cap headroom.
<!-- SUBAGENT-BILLING-END -->

## Section B: Storage And Search: Hosted DB Bakeoff

<!-- SUBAGENT-STORAGE-START -->
### Recommendation

Assumption update: hosted Yutome does **not** need backward compatibility with SQLite + LanceDB internals. It does need product-level feature parity: catalog, jobs, active transcripts, chunks, lexical search, semantic search, hybrid search, grouped results, context expansion, billing attribution, and multi-tenant controls.

Under that assumption, do **not** design hosted V1 around SQLite plus a separate vector/search database. Use one canonical hosted database if it can pass retrieval evals.

Recommended hosted V1 default:

- Use **Postgres + VectorChord Suite** as the first implementation target. On Railway, this means a custom Postgres service/image with `vchord`, `vchord_bm25`, `pg_tokenizer`, and `vector`, unless a managed VectorChord-capable Postgres host is selected before launch.
- Store catalog, jobs, transcript versions, active chunks, embeddings, usage ledger, billing reservations, BM25 vectors, optional FTS sidecars, and dense vectors in the same Postgres database.
- Accept a new ranking contract: VectorChord BM25 is not SQLite FTS5 raw syntax, and raw SQLite FTS5 compatibility does not need to remain. Replace raw FTS5 with a documented BM25 query parser plus optional Postgres `websearch_to_tsquery`, `phraseto_tsquery`, or advanced `to_tsquery` sidecar modes where useful.
- Implement hybrid search as two top-N CTEs, BM25 lexical + VectorChord semantic, fused with RRF or a measured weighted rule. Keep optional reranking behind evals.

Production fallback:

- **Managed Postgres + `pgvector` + built-in Postgres FTS** remains the fallback if VectorChord cannot pass the database operations gate in time for paid production. It is no longer the default target, but it is the lowest-risk escape hatch because it preserves the one-Postgres substrate and ordinary app database semantics.

Not recommended as the single canonical hosted database:

- **LanceDB-only**: strong search primitives, but not a relational/workflow/billing database. It would require app-level integrity, denormalized state, custom job logic, scheduled optimize/compaction, and custom billing metrics.
- **Turbopuffer-only**: excellent managed first-stage retrieval, but not a relational/control-plane database. Use it only if paired with Postgres/D1 for jobs, billing, users, and catalog state.
- **D1 + Vectorize**: lowest Cloudflare-only friction, but violates the one-substrate preference and lacks native hybrid lexical/vector ranking.
- **Typesense / Weaviate / OpenSearch**: valid search-first bakeoff controls, but only become one-substrate candidates if Yutome accepts document-store consistency for jobs/catalog/billing.
- **Milvus / Vespa**: too much operational burden for V1 noob hosting unless simpler systems fail retrieval quality or scale tests.

### Current Yutome Storage Responsibilities

Current code uses SQLite as the canonical catalog and workflow database:

- `src/yutome/db.py` defines channels, library channels/sources, videos, transcript versions, chunks, embeddings, transcript attempts, and jobs. It also creates `videos_fts` and `chunks_fts`.
- `src/yutome/store.py` owns catalog writes: discovered video upserts, metadata upserts, active transcript replacement, chunk replacement, ingest status, transcript attempts, and FTS rebuilds.
- `src/yutome/query.py` compiles one `QueryRequest` contract into multiple plan kinds: SQL chunk/video/channel queries, SQLite FTS lexical queries, LanceDB semantic/hybrid queries, two-stage SQLite-then-Lance queries, and status breakdowns.
- `src/yutome/retrieval.py` resolves chunks, neighboring context, source URLs, snippets, and metadata from SQLite rows.
- `src/yutome/embeddings.py` reads active chunks from SQLite, embeds them with Voyage, writes a LanceDB `chunks` table, and records embedding status back into SQLite.

Current LanceDB usage is intentionally rebuildable and secondary:

- LanceDB stores active chunk rows with vectors plus duplicated chunk/transcript metadata.
- `ensure_lancedb_chunk_indexes()` creates a LanceDB FTS index on `text`.
- Hybrid search uses `table.search(query_type="hybrid").vector(...).text(...).where(..., prefilter=True).rerank()`.
- LanceDB filters only cover fields duplicated in its chunk table: `video_id`, `channel_id`, `chunk_id`, `source`, `language`, `is_generated`, `sequence`, `start_ms`, and `token_count`.
- Filters needing video/channel/job state (`channel_handle`, `published_at`, `duration_seconds`, `ingest_status`, `live_status`, `channel_selected`, `last_attempt_*`) force a two-stage plan through SQLite.
- Search results from LanceDB are validated and enriched against SQLite active transcript rows; stale LanceDB rows are dropped.

The tests encode this split. `tests/test_fts5_escape.py` protects SQLite FTS5 phrase escaping and raw FTS5 operator behavior. `tests/test_retrieval_exports.py` protects hybrid fallback to lexical when Voyage/LanceDB is unavailable and stale LanceDB schema detection. `tests/test_config_paths_db.py` asserts the SQLite catalog/FTS tables exist.

### Hosted Product Requirements Matrix

These requirements survive the no-backward-compatibility assumption:

| Area | Hosted requirement | What can change |
|---|---|---|
| Catalog/state | Channels, library sources, videos, transcript versions, active transcript uniqueness, chunks, embeddings, transcript attempts, jobs, and usage ledger need durable writes, uniqueness, indexing, and transactional updates. | Table names and migration format can change. SQLite rowids/triggers do not need to survive. |
| Ingest workflow | Idempotent metadata upserts, active transcript replacement, chunk replacement, failed/deferred status, retryable attempts, queue locks, and resumable embedding/indexing are required. | Current SQLite transaction shape can become Postgres transactions or equivalent. |
| Lexical search | Chunk text search, video title/description search, phrase-ish exact terms, boolean-ish power mode, field boosts, snippets/highlights, and stable eval behavior. | Raw SQLite FTS5 syntax can be replaced with a new documented Postgres/search syntax. |
| Semantic search | `voyage-4-lite` query/document embeddings, 1024-dimensional vectors, vector top-k, metadata filters, and semantic-only failure behavior when embeddings are unavailable. | Vector distance and score format can change if evals pass. |
| Hybrid search | Lexical + semantic fusion with reproducible ranking, lexical fallback path, and optional rerank stage. | Native LanceDB hybrid is not required; SQL/app RRF is acceptable. |
| Filters/sorting | `workspace_id`, video/channel/source/language/generated, publish date, duration, status, selected channels, chunk sequence/time/token count, and last-attempt filters. Sort by score, publish date, duration, title, status, sequence, start time, and last attempt time. | Some indexes/partitions can be hosted-only. |
| Grouping/context | Group-by-video with per-video hit limits, neighboring transcript context by sequence/time, and metadata-rich projections. | Implementation can use SQL window functions or app-side grouping if performance is acceptable. |
| Billing/tenancy | Every expensive write/search/embed/fetch needs per-workspace attribution: tokens, vector rows/dimensions, search count, top-k/candidate count, bytes/chunks, latency, provider cost, and background index work estimates. | Provider-native cost metrics are optional if app-level usage events are good enough. |

### Provider Capability Matrix

Legend: **Yes** means the provider natively fits the requirement. **Partial** means it works with app code, denormalization, custom ranking, or a second control-plane database. **No** means it is the wrong primitive for that requirement. Every row below has a source row in the source map. When the matrix says a search/vector system is not a canonical app database, that is an architectural inference from the documented API shape: document/vector/search APIs do not replace relational constraints, joins, ordinary SQL transactions, and queue/ledger tables.

#### Source Map For Matrix Claims

| Candidate | Primary sources used for the row | Claims these sources ground |
|---|---|---|
| Managed Postgres + pgvector + built-in FTS | [pgvector README](https://github.com/pgvector/pgvector), [Postgres transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html), [Postgres text search controls](https://www.postgresql.org/docs/current/textsearch-controls.html), [Postgres FTS indexes](https://www.postgresql.org/docs/current/textsearch-indexes.html), [Postgres SELECT locking](https://www.postgresql.org/docs/current/sql-select.html) | Exact/ANN vector search with HNSW/IVFFlat and SQL filters; ACID transactions; `tsvector`/`tsquery`, `websearch_to_tsquery`, `phraseto_tsquery`, weights, `ts_rank`/`ts_rank_cd`, GIN/GiST indexes, and `ts_headline`; `FOR UPDATE SKIP LOCKED` for SQL-backed work queues. |
| Postgres + VectorChord Suite | [VectorChord Suite](https://docs.vectorchord.ai/vectorchord/getting-started/vectorchord-suite.html), [VectorChord hybrid search](https://docs.vectorchord.ai/vectorchord/use-case/hybrid-search.html), [VectorChord Cloud limits](https://docs.vectorchord.ai/cloud/limit/cloud-limit.html), [VectorChord README](https://github.com/tensorchord/VectorChord), [VectorChord license](https://raw.githubusercontent.com/tensorchord/VectorChord/main/LICENSE), [VectorChord-BM25 README](https://github.com/tensorchord/VectorChord-bm25), [VectorChord-BM25 license](https://raw.githubusercontent.com/tensorchord/VectorChord-bm25/main/LICENSE), [pg_tokenizer.rs license](https://raw.githubusercontent.com/tensorchord/pg_tokenizer.rs/main/LICENSE), [AGPLv3 section 13](https://www.gnu.org/licenses/agpl-3.0.html.en), [Elastic License v2](https://www.elastic.co/licensing/elastic-license) | Postgres-compatible extension suite with vector, tokenizer, and BM25 components; documented BM25 + vector hybrid examples with RRF/reranking; current cloud HA limitation; VectorChord/VectorChord-BM25 dual AGPLv3/ELv2 licensing, pg_tokenizer.rs Apache-2.0 licensing, and the likely hosted-Yutome license path. |
| Postgres + ParadeDB Enterprise/BYOC + pgvector | [ParadeDB deploy overview](https://docs.paradedb.com/deploy/overview), [ParadeDB Enterprise](https://docs.paradedb.com/deploy/enterprise), [ParadeDB full-text overview](https://docs.paradedb.com/documentation/full-text/overview), [pgvector README](https://github.com/pgvector/pgvector) | Postgres-based BM25/fuzzy/faceted text search; Community production caveat around WAL/crash/reindex risk; Enterprise WAL/replication/crash-recovery posture; BYOC posture; vector support via pgvector rather than ParadeDB alone. |
| Cloudflare D1 + Vectorize | [D1 SQL statements](https://developers.cloudflare.com/d1/sql-api/sql-statements/), [D1 Worker API and batch transactions](https://developers.cloudflare.com/d1/worker-api/d1-database/), [D1 Time Travel](https://developers.cloudflare.com/d1/platform/time-travel/), [D1 limits](https://developers.cloudflare.com/d1/platform/limits/), [Vectorize client API](https://developers.cloudflare.com/vectorize/reference/client-api/), [Vectorize metadata filtering](https://developers.cloudflare.com/vectorize/reference/metadata-filtering/) | D1 SQL/SQLite/FTS5 control plane, foreign keys, batch transactions, Time Travel, and D1 metrics; Vectorize async insert/upsert/delete and topK limits; metadata index/filter limits; split lexical/vector consistency and Worker-side fusion. |
| LanceDB-only | [LanceDB full-text search](https://docs.lancedb.com/search/full-text-search), [LanceDB SQL FTS](https://docs.lancedb.com/search/sql/fts-sql), [LanceDB hybrid search](https://docs.lancedb.com/search/hybrid-search), [LanceDB filtering](https://docs.lancedb.com/search/filtering), [LanceDB table updates](https://docs.lancedb.com/tables/update), [LanceDB storage configuration](https://docs.lancedb.com/storage/configuration) | FTS/BM25-like search, Enterprise-only SQL FTS, native hybrid search and reranking, scalar filtering, mutable rows, object-store/local storage; lack of ordinary relational workflow/ledger semantics is inferred from the documented table/search API surface. |
| Turbopuffer | [Turbopuffer pricing](https://turbopuffer.com/pricing), [Turbopuffer query API](https://turbopuffer.com/docs/query), [Turbopuffer hybrid search](https://turbopuffer.com/docs/hybrid-search), [Turbopuffer write API](https://turbopuffer.com/docs/write) | BM25, ANN/kNN, sparse vectors, filters, multi-query hybrid pattern, strong/eventual consistency, namespace modeling, `limit.per` limits, write/query billing fields, monthly minimums, logical-byte floor, write floor, and batch discounts. |
| Typesense | [Typesense vector search](https://typesense.org/docs/30.2/api/vector-search.html), [Typesense search API](https://typesense.org/docs/30.2/api/search.html), [Typesense API keys](https://typesense.org/docs/30.2/api/api-keys.html), [Typesense high availability](https://typesense.org/docs/30.2/guide/high-availability.html) | Keyword and vector/hybrid search, filters/sorts/facets, grouping, highlights/snippets, API-key scoped access, and HA/cloud/self-host posture. |
| Weaviate | [Weaviate hybrid search](https://docs.weaviate.io/weaviate/search/hybrid), [Weaviate BM25](https://docs.weaviate.io/weaviate/search/bm25), [Weaviate filters](https://docs.weaviate.io/weaviate/search/filters), [Weaviate multi-tenancy](https://docs.weaviate.io/weaviate/manage-collections/multi-tenancy), [Weaviate installation](https://docs.weaviate.io/deploy/installation-guides) | BM25F, vector/hybrid alpha/fusion, filters, multi-tenancy primitives, Docker/cloud/Kubernetes deployment posture, and document-object rather than relational model. |
| OpenSearch / Elasticsearch | [OpenSearch neural/hybrid tutorial](https://docs.opensearch.org/docs/3.0/tutorials/vector-search/neural-search-tutorial/), [OpenSearch full-text queries](https://docs.opensearch.org/docs/latest/query-dsl/full-text/), [Elasticsearch RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion), [Elasticsearch kNN search](https://www.elastic.co/docs/solutions/search/vector/knn) | Lucene/BM25 text search, phrase/query-string/analyzer capability, vector/kNN search, hybrid/RRF options, highlights/aggs/collapse, and heavier managed/JVM cluster posture. |
| Qdrant | [Qdrant hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/), [Qdrant filtering](https://qdrant.tech/documentation/concepts/filtering/), [Qdrant grouping](https://qdrant.tech/documentation/concepts/hybrid-queries/#grouping), [Qdrant multitenancy](https://qdrant.tech/documentation/guides/multiple-partitions/) | Dense/sparse hybrid with RRF/DBSF, payload filters and formula scoring, grouping API, tenant partition guidance; weak fielded lexical/catalog fit follows from vector/payload-store model. |
| Meilisearch | [Meilisearch hybrid search](https://www.meilisearch.com/docs/learn/ai_powered_search/hybrid_search), [Meilisearch search API](https://www.meilisearch.com/docs/reference/api/search), [Meilisearch settings API](https://www.meilisearch.com/docs/reference/api/settings), [Meilisearch tenant tokens](https://www.meilisearch.com/docs/learn/security/tenant_tokens) | Simple lexical search, semantic/hybrid embedders, filters/sort/facets/highlights/crops, distinct-like behavior, tenant-token access; not a relational app database. |
| Milvus | [Milvus full-text search](https://milvus.io/docs/full-text-search.md), [Milvus multi-vector hybrid search](https://milvus.io/docs/multi-vector-search.md), [Milvus filtered search](https://milvus.io/docs/filtered-search.md), [Milvus grouping search](https://milvus.io/docs/grouping-search.md), [Milvus standalone Docker Compose](https://milvus.io/docs/install_standalone-docker-compose.md) | BM25 sparse vectors, dense+sparse hybrid search, RRF/weighted rankers, scalar filtering, grouping search, and etcd/MinIO-heavy standalone ops posture. |
| Vespa | [Vespa hybrid search lab](https://learn.vespa.ai/vector-search/lab-hybrid/), [Vespa BM25](https://docs.vespa.ai/en/ranking/bm25.html), [Vespa nearest neighbor search](https://docs.vespa.ai/en/querying/nearest-neighbor-search.html), [Vespa grouping](https://docs.vespa.ai/en/grouping.html), [Vespa deployment](https://docs.vespa.ai/en/cloud/deploy-vespa-cloud.html) | BM25, HNSW nearest-neighbor/vector search, rank profiles/global-phase/RRF-style ranking patterns, grouping, and platform/deployment surface area. |

#### Canonical Database Fit

This is the part that decides whether Yutome can avoid hosting both a SQL/catalog database and a separate search/vector database.

| Candidate | One canonical DB for catalog + jobs + chunks? | Transactions / integrity | Queue / job state | Active transcript replacement | Usage ledger / billing attribution | Hosted/noob ops posture | Single-substrate verdict |
|---|---|---|---|---|---|---|---|
| Managed Postgres + pgvector + built-in FTS | Yes | Yes: ordinary SQL, constraints, indexes, transactions, backups. | Yes: `FOR UPDATE SKIP LOCKED`, retry columns, ordinary admin queries. | Yes: one transaction can deactivate old transcript, insert new active version, replace chunks, and enqueue embedding/index work. | Yes: first-class `usage_events`, reservations, cost reconciliation tables. | Strong: widely managed; pgvector is available on common Postgres providers. | **Production fallback if VectorChord ops fail.** Search ranking is weaker than BM25 systems, but product/database fit is strong. |
| Postgres + VectorChord Suite | Yes | Yes, because it remains Postgres. | Yes. | Yes. | Yes. | Medium: self-hosted suite image exists; managed/HA maturity needs proof. | **Default target.** Native BM25 + vector in the same Postgres substrate is the desired hosted shape. |
| Postgres + ParadeDB Enterprise/BYOC + pgvector | Yes, if Enterprise/BYOC is the primary DB | Yes, assuming Enterprise WAL support. | Yes. | Yes. | Yes. | Medium/low for noob V1: sales-led Enterprise/BYOC; Community is not acceptable for paid production. | Serious only if Enterprise terms are easy enough. |
| Cloudflare D1 + Vectorize | No | Partial: D1 gives managed SQLite/FTS5, foreign keys, batch transactions, and Time Travel; Vectorize is a separate async vector index. | Partial: D1 can store jobs, but vector/index consistency is split. | Partial: D1 can own active state; Vectorize must be updated separately. | Partial: D1 and Vectorize emit different provider units. | Strong on Cloudflare, but two stores. | Cloudflare-only fallback, not the one-substrate answer. |
| LanceDB-only | Partial | Partial: mutable tables, but no SQL relational constraints/triggers/joins like Postgres. | Partial/no: jobs become app-level rows without SQL queue ergonomics. | Partial: must model active chunks/materialized docs and cleanup carefully. | Partial: app must meter queries/indexing/compaction itself. | Medium: OSS can use object storage but still needs a hosted service/container and optimize/compaction policy. | Not V1 canonical DB unless Yutome becomes Lance-native. |
| Turbopuffer-only | No | No: document retrieval API, not relational DB. | No. | Partial/no: possible document writes, but no workflow transactions. | Partial: excellent query/write billable byte metrics, but not total app billing state. | Strong managed retrieval, weak canonical DB. | Retrieval service only, paired with Postgres/D1. |
| Typesense-only | Partial | Partial: document collection writes, no relational constraints. | Partial/no: possible as documents, but app-enforced and awkward. | Partial: denormalized active docs and batch updates. | Partial: app-level usage events needed. | Strong: simple service/Cloud, HA path exists. | Possible only if we accept document-store consistency. Better paired with Postgres. |
| Weaviate-only | Partial | Partial: object store with schema/multi-tenancy, not relational DB. | Partial/no. | Partial: denormalized active objects. | Partial: app-level usage events needed. | Medium: Docker easy; production often Cloud/Kubernetes. | Possible only if we accept document-store consistency. Better paired with Postgres. |
| OpenSearch/Elasticsearch-only | Partial | Partial: document store with optimistic concurrency, refresh semantics, no relational constraints. | Partial/no: possible but poor fit. | Partial: bulk/update-by-query patterns, app-enforced invariants. | Partial: app-level usage events needed. | Low for noob self-host; good managed but expensive/ops-heavy. | Search parity ceiling, not default canonical DB. |
| Qdrant-only | No | No: vector/payload store, not catalog DB. | No. | Partial/no. | Partial: app-level usage events needed. | Strong vector ops, weak canonical DB. | Retrieval control only. |
| Meilisearch-only | No/partial | Partial: document index, async tasks, no relational constraints. | No. | Partial: denormalized docs. | Partial: app-level usage events needed. | Strong noob simplicity. | Noob search control, not canonical DB. |
| Milvus-only | No | No: vector DB, not workflow/catalog DB. | No. | Partial/no. | Partial: app-level usage events needed. | Low for small SaaS: etcd/MinIO/cluster posture. | Out for V1 canonical DB. |
| Vespa-only | Partial | Partial: powerful document/ranking platform, but not ordinary app DB. | No/partial. | Partial: feed/update flows. | Partial: app-level usage events needed. | Low for V1: schemas/rank profiles/cluster ops. | Future ranking platform only. |

#### Retrieval And Search Fit

This is the part that decides whether the canonical DB recommendation is good enough for Yutome's assistant-facing search quality.

| Candidate | Chunk lexical search | Video title/description search | Vector search | Hybrid search | Filters + sorting | Group-by-video | Snippets/context | Main search gap |
|---|---|---|---|---|---|---|---|---|
| Managed Postgres + pgvector + built-in FTS | Yes: `tsvector`, `tsquery`, phrase/web search, weights, `ts_rank`/`ts_rank_cd`. | Yes: weighted `tsvector` over title/description. | Yes: pgvector exact/ANN with HNSW/IVFFlat. | Partial: custom SQL/app RRF from lexical and vector top-N. | Yes: SQL indexes, joins, partitions. | Yes: SQL window functions. | Yes: `ts_headline`; context via ordered chunk rows. | No native BM25; hybrid fusion is ours. |
| Postgres + VectorChord Suite | Yes: `vchord_bm25` native BM25 vectors/tokenizers. | Yes: BM25 columns or weighted design. | Yes: VectorChord or pgvector. | Partial/yes: docs show BM25 + vector with RRF/rerank; fusion still app/SQL-level. | Yes: SQL. | Yes: SQL window functions. | Partial: app or SQL headline approach needs design. | Maturity/licensing/HA, not core capability. |
| Postgres + ParadeDB Enterprise/BYOC + pgvector | Yes: BM25/Tantivy-style text search. | Yes. | Yes via pgvector. | Partial: likely SQL/app fusion with pgvector. | Yes: SQL plus ParadeDB indexes. | Yes: SQL window functions. | Yes/partial depending query path. | Enterprise access/terms and operational posture. |
| Cloudflare D1 + Vectorize | Yes: D1 FTS5. | Yes: D1 FTS5. | Yes: Vectorize ANN. | Partial: Worker-side D1 + Vectorize fusion; there is no native single-query lexical+vector rank across both products. | Partial: D1 rich filters; Vectorize metadata indexes are capped and Vectorize mutations are async. | Partial: Worker/D1 grouping. | Partial: D1 snippets or app snippets. | Split ranking, consistency, and metadata limits. |
| LanceDB-only | Yes: FTS/BM25-like APIs; Enterprise SQL FTS is separate. | Yes with extra columns/indexes. | Yes. | Yes: native hybrid query builder + rerank. | Partial: scalar filters yes; broad relational filters require denormalization. | Partial: app-side or Enterprise SQL path. | Partial: app-side snippets/context. | Great retrieval, weak canonical DB. |
| Turbopuffer | Yes: BM25 full-text fields. | Yes with fields. | Yes: ANN/kNN, sparse vectors. | Partial: docs recommend multi-query + client-side fusion/rerank. | Yes for indexed attributes; namespace modeling matters. | Partial: `group_by` is aggregation; `limit.per` not for BM25/ANN yet. | Partial: app-side snippets/context. | Managed retrieval only; grouping/fusion in app. |
| Typesense | Yes: keyword, phrase-ish behavior, typo tolerance, highlighting. | Yes with fields/boosts. | Yes. | Yes: hybrid/vector search with rank fusion controls. | Yes: filters/sort/facets. | Yes: `group_by`/`group_limit`. | Yes: highlights/snippets; context app-side. | Not relational/job DB. |
| Weaviate | Yes: BM25F. | Yes with properties. | Yes. | Yes: native hybrid alpha/fusion. | Yes: filters/sort-ish patterns. | Yes/partial: grouping available, but output semantics need eval. | Partial: app-side snippets/context likely. | Not relational/job DB; cross-reference caution. |
| OpenSearch/Elasticsearch | Yes: Lucene BM25, phrase/proximity/query-string/analyzers. | Yes: field boosts. | Yes: kNN/vector. | Yes: hybrid/RRF/score normalization options. | Yes: filters/sort/aggs. | Yes: collapse/aggregations. | Yes: highlights; context app-side. | Ops/cost/noob burden. |
| Qdrant | Partial: text/phrase payload filtering and sparse/BM25-style vectors, not full search-engine lexical. | Partial. | Yes: dense/sparse/named vectors. | Yes for dense+sparse fusion/RRF/DBSF. | Yes: payload filters and formula scoring. | Yes/partial: grouping exists, semantics need eval. | Partial: app-side snippets/context. | Weak fielded lexical search and catalog fit. |
| Meilisearch | Yes: simple lexical/product search, filters, highlighting/crops. | Yes. | Yes: semantic/hybrid embedders. | Yes/partial: hybrid exists but less ranking control. | Yes: filters/sort/facets with constraints. | Partial: distinct can approximate video grouping. | Yes highlights/crops; context app-side. | Less control and not canonical DB. |
| Milvus | Partial: BM25 function/sparse text. | Partial. | Yes. | Yes: dense+sparse hybrid, weighted/RRF. | Yes: scalar filters. | Yes: grouping search. | Partial: app-side snippets/context. | Heavy ops and not catalog DB. |
| Vespa | Yes: BM25 and rich ranking. | Yes. | Yes. | Yes: rank profiles, nearest neighbor, RRF/global-phase. | Yes. | Yes. | Partial/yes with custom ranking/app context. | Too much platform surface for V1. |

### VectorChord Licensing Read

This is not legal advice, but the license read is concrete enough to change the implementation recommendation.

VectorChord and VectorChord-BM25 are each dual-licensed under AGPLv3 or Elastic License v2. pg_tokenizer.rs is Apache-2.0, and pgvector is PostgreSQL-licensed. The `vchord-suite` image should be treated as packaging of those components, not as a separate permissive license grant.

Hosted Yutome should not rely on the Elastic License v2 path. ELv2 prohibits providing the software to third parties as a hosted or managed service when the service provides access to a substantial set of the software's features or functionality. Yutome would not expose raw VectorChord APIs as a database service, but the restriction is close enough to hosted search infrastructure that ELv2 is the wrong default path.

The workable no-permission path is AGPLv3 with unmodified VectorChord components:

- Use upstream, unmodified `vchord` and `vchord_bm25` binaries/extensions.
- Keep Yutome application code cleanly separated from the Postgres extensions; call them through SQL rather than linking proprietary code into extension code.
- Keep component notices and license inventory in the hosted deployment.
- If Yutome modifies VectorChord or VectorChord-BM25, expect AGPLv3 section 13 to require offering the Corresponding Source of the modified AGPL-covered component to remote users.
- If Yutome distributes a Docker image/appliance containing the AGPL components, satisfy AGPL object-code/source distribution obligations for those components.

So VectorChord Suite is not blocked by licensing if the product is comfortable with AGPL operational discipline and does not modify the TensorChord components. It remains a maturity/ops/HA question more than a permission question.

Sources: [VectorChord license](https://raw.githubusercontent.com/tensorchord/VectorChord/main/LICENSE), [VectorChord-BM25 license](https://raw.githubusercontent.com/tensorchord/VectorChord-bm25/main/LICENSE), [pg_tokenizer.rs license](https://raw.githubusercontent.com/tensorchord/pg_tokenizer.rs/main/LICENSE), [pgvector license](https://raw.githubusercontent.com/pgvector/pgvector/master/LICENSE), [AGPLv3](https://www.gnu.org/licenses/agpl-3.0.html.en), [Elastic License v2](https://www.elastic.co/licensing/elastic-license), [Elastic ELv2 FAQ](https://www.elastic.co/licensing/elastic-license/faq).

### Turbopuffer Bakeoff Cost Model

Turbopuffer is a first-class bakeoff candidate, but only for the retrieval/search substrate role unless the product accepts a separate canonical app database.

Its public pricing page currently lists:

- Launch: $64/month minimum usage.
- Scale: $256/month minimum usage.
- Enterprise: at least $4,096/month with a 35% usage premium.
- Unused monthly commitments do not roll over, and usage beyond the minimum is charged as actual usage.

Track these Turbopuffer units per Yutome workspace during the bakeoff:

- namespace logical bytes by workspace, with vectors, FTS-indexed fields, filterable attributes, and non-filterable attributes separated.
- query logical bytes queried and returned.
- write logical bytes written, including the 10 KB write-request floor and batch-write discount behavior.
- number of query subrequests per user search, because Turbopuffer hybrid search commonly means one vector query plus one BM25 query plus optional rerank/fusion work.
- namespace count, branch/copy operations, and schema/index changes.
- cache temperature and server latency metrics for margin/performance alerts.

The key hosted-Yutome risk is many-small-workspace economics. Turbopuffer query pricing is based on logical bytes queried plus returned, and queried bytes use the actual namespace size or a 1.28 GB floor, whichever is greater. Small isolated namespaces can waste that floor unless Yutome consolidates tenants carefully. Its query API supports BM25, ANN, kNN, sparse vectors, filters, multi-query execution, and strong/eventual consistency; its write API returns billable write/query bytes. It does not provide relational joins, foreign keys, ordinary SQL transactions, or a queue/usage-ledger database, so the bakeoff question is whether its retrieval quality and managed ops are worth pairing with Postgres/D1.

Sources: [Turbopuffer pricing](https://turbopuffer.com/pricing), [query API](https://turbopuffer.com/docs/query), [hybrid search](https://turbopuffer.com/docs/hybrid-search), [write API](https://turbopuffer.com/docs/write).

### Bakeoff Reading

The recommendation is not "Postgres has the best search." It is:

1. Yutome's hosted backend needs a reliable application database as much as it needs retrieval.
2. VectorChord Suite is the default target because it keeps catalog, jobs, usage, BM25 lexical search, and dense-vector search inside one Postgres-compatible substrate.
3. Managed Postgres + pgvector + built-in FTS is the fallback if VectorChord hosting cannot pass the production database operations gate before paid launch.
4. Turbopuffer, Typesense, Weaviate, OpenSearch, Qdrant, Meilisearch, Milvus, and Vespa should be judged as "is the retrieval win large enough to justify a second control-plane database or document-store consistency tradeoff?"

### LanceDB Alone: Technically Possible, Not Parity-Preserving

Official LanceDB docs show that LanceDB supports several primitives Yutome already uses or could use:

- Full-text search with BM25-like scoring, FTS indexes, phrase queries, fuzzy search, filters, and `create_fts_index()`: [LanceDB full-text search](https://docs.lancedb.com/search/full-text-search).
- Enterprise-only beta SQL FTS with `fts(table_name, json_query)`, `_score`, phrase queries when the index is created with `with_position = true`, fuzzy search, boolean query composition, multi-match, field boosts, and surrounding SQL `WHERE`, `GROUP BY`, and `JOIN`: [LanceDB FTS SQL](https://docs.lancedb.com/search/sql/fts-sql).
- Native hybrid search combining vector and FTS results with rerankers such as RRF: [LanceDB hybrid search](https://docs.lancedb.com/search/hybrid-search).
- SQL-like metadata filters with prefilter/postfilter, `IN`, `LIKE`, ranges, booleans, scalar functions, and scalar indexes: [LanceDB filtering](https://docs.lancedb.com/search/filtering), [scalar indexes](https://docs.lancedb.com/indexing/scalar-index).
- Mutations through `update`, `delete`, and `merge_insert` upserts: [LanceDB table updates](https://docs.lancedb.com/tables/update).
- Local path and object-store storage for OSS LanceDB: [storage configuration](https://docs.lancedb.com/storage/configuration).

Important distinction: the `fts()` SQL table function is **not** the same surface as the regular OSS LanceDB FTS/hybrid APIs Yutome uses today. The official SQL pages are labeled Enterprise-only and describe an Enterprise FlightSQL endpoint. Local OSS LanceDB provides `create_fts_index()`, `table.search(...)`, `MatchQuery`/`PhraseQuery`/`MultiMatchQuery`/`BoostQuery`, metadata filters, and hybrid search query builders, but the installed OSS `lancedb==0.30.2` package does not expose a local `db.sql(...)`/`db.execute(...)` API or a local FlightSQL endpoint for `SELECT ... FROM fts(...)`. Treat the SQL FTS page as evidence for LanceDB Enterprise behavior, not as a feature available to the local OSS backend or as proof that LanceDB-only would simplify Yutome V1.

But replacing SQLite with LanceDB alone would still require a substantial rewrite and would lose exact behavior unless we reimplement it:

- **Relational integrity:** current SQLite uses primary keys, foreign keys, triggers, uniqueness, indexes, and WAL behavior. LanceDB tables can store rows and be updated, but LanceDB OSS is not a relational database with SQLite-style foreign keys, cascading deletes, triggers, or transactional catalog semantics.
- **Catalog queries:** video/channel listing, active transcript joins, latest transcript attempt subqueries, status breakdowns, counts, and selected library-source logic are ordinary SQLite today. LanceDB SQL FTS can combine FTS results with surrounding SQL joins, grouping, and filters, but that page is marked Enterprise-only and beta. Relying on it would make "LanceDB-only" a dependency on LanceDB Enterprise FlightSQL behavior, not a simplification of the current local OSS product.
- **FTS5 raw mode:** Yutome exposes raw SQLite FTS5 syntax for power users and tests special behavior around `AND`/`OR`/`NOT`, column scoping, prefix operators, phrase escaping, and snippets. LanceDB's SQL FTS page supports boolean queries through JSON query objects built by `MatchQuery`, `PhraseQuery`, `BoostQuery`, and `MultiMatchQuery`, but it is not SQLite FTS5 raw syntax. Supporting current `--raw` semantics would still require a parser/translator or a documented behavior change.
- **Video title/description FTS:** SQLite has a dedicated `videos_fts` table and column-scoped `title:(...)` / `description:(...)` search. LanceDB can index multiple text columns and use `fts_columns`, but query syntax, scoring, highlighting/snippet behavior, and raw mode will drift.
- **Ordering and pagination:** current SQLite plans order by `published_at`, `duration_seconds`, `title`, `ingest_status`, `sequence`, `start_ms`, and `last_attempt_created_at`, then paginate. LanceDB Enterprise SQL can express ordinary SQL ordering around `fts()`, but the OSS table-query path Yutome currently uses does not give us the same general relational query surface without moving to SQL/FlightSQL or application-side sorting.
- **Active transcript logic:** current indexing selects only `tv.active = 1`; writes atomically deactivate old transcript versions, insert the new active version, replace chunks, and update ingest status. LanceDB-only would need either materialized `active_chunks` tables or careful multi-table update choreography.
- **Job/attempt state:** `jobs` and `transcript_attempts` are operational state, not retrieval documents. They need idempotent enqueue/resume, lock ownership, retry windows, latest-attempt projections, and admin status queries. LanceDB can store such rows, but it is not the obvious queue/catalog substrate.
- **Rebuild/resume:** today SQLite is canonical and LanceDB can be dropped/rebuilt. A LanceDB-only design makes the vector index and the source of truth the same system, so failed embedding batches, stale indexes, compaction, and partial updates become higher-risk.
- **Index maintenance:** LanceDB docs say added rows after FTS/scalar/vector indexes remain queryable through slower unindexed fragments until `optimize()` folds them into indexes. Updates move rows out of indexes and large updates may require rebuild/optimize. Yutome would need scheduled optimize/compaction and billing attribution for that background work.

Bottom line: LanceDB alone is feasible only if we accept a new query engine and a denormalized data model. It would not preserve current functionality "as-is."

### LanceDB-Only Migration Shape, If Ever Attempted

Minimum work:

1. Create LanceDB tables for `channels`, `library_sources`, `videos`, `transcript_versions`, `chunks`, `transcript_attempts`, `jobs`, and `embeddings`, or materialize one wide `active_chunks` table plus separate operational tables.
2. Replace SQLite joins with application joins or add DuckDB for SQL over Lance tables.
3. Rebuild lexical behavior around LanceDB query objects: phrase query for default literal search, custom parser for raw-ish boolean queries, and separate video title/description indexes.
4. Add scalar indexes for every metadata filter used by `QueryRequest`.
5. Add explicit `optimize()` scheduling and index health checks.
6. Rebuild consistency checks: active transcript uniqueness, stale chunk removal, embedding status, and failed/resumable jobs.
7. Build parity tests comparing current SQLite + LanceDB results against the new backend for lexical, semantic, hybrid, group-by-video, status, list, show, and raw query behavior.

This does not simplify noob onboarding enough to justify doing before hosted V1.

### Alternative Backends

| Option | Feature fit | What remains lacking |
|---|---|---|
| Keep SQLite + LanceDB | Best current local parity. SQLite owns catalog/FTS/jobs; LanceDB owns semantic/hybrid chunk search. | Not the hosted target under the one-substrate assumption. Hosted mode needs a service/container or replica path and still leaves two stores. |
| Cloudflare D1 + Vectorize | Operationally easiest Cloudflare-native fallback. D1 gives managed SQLite-like catalog, FTS5, foreign keys, batch transactions, Time Travel, and rows-read/written metrics; Vectorize gives managed vector search and metadata filters. | Not one substrate. No native hybrid lexical+vector search, so Workers must dual-write and fuse D1 FTS + Vectorize. D1 and Vectorize have separate APIs, limits, consistency behavior, and billing units. Vectorize metadata indexes are capped at 10 properties, metadata filters are compact JSON under 2048 bytes, indexed strings only use the first 64B, upserts/deletes are async, and topK is capped at 100 or 50 when returning full metadata. Ranking will drift from LanceDB/Postgres. Sources: [D1 SQL/FTS5](https://developers.cloudflare.com/d1/sql-api/sql-statements/), [D1 Worker API](https://developers.cloudflare.com/d1/worker-api/d1-database/), [D1 Time Travel](https://developers.cloudflare.com/d1/platform/time-travel/), [D1 limits](https://developers.cloudflare.com/d1/platform/limits/), [Vectorize metadata filtering](https://developers.cloudflare.com/vectorize/reference/metadata-filtering/), [Vectorize API](https://developers.cloudflare.com/vectorize/reference/client-api/). |
| DuckDB + LanceDB | Strong for SQL analytics over Lance tables. LanceDB docs show DuckDB's Lance extension can run SQL, vector search, FTS, and hybrid search over Lance tables. | Adds another native engine, not Cloudflare-native, and DuckDB FTS docs warn the FTS index does not update automatically when the input table changes. Better for batch/admin analytics than hosted noob infra. Sources: [LanceDB DuckDB integration](https://docs.lancedb.com/integrations/data/duckdb), [DuckDB FTS](https://duckdb.org/docs/current/core_extensions/full_text_search.html). |
| SQLite + sqlite-vec | Attractive local single-file direction: keep SQLite catalog, FTS5, jobs, and add vector KNN in SQL. | `sqlite-vec` docs are still marked work-in-progress; hybrid/rerank behavior would be custom; Cloudflare D1 cannot load arbitrary native SQLite extensions. Source: [sqlite-vec docs](https://alexgarcia.xyz/sqlite-vec/). |
| SQLite + sqlite-vss | Older single-SQLite vector option. | Not in active development; docs say use `sqlite-vec` instead. Filtering on top of KNN, updates, memory/index limits are known gaps. Source: [sqlite-vss README](https://github.com/asg017/sqlite-vss). |
| Turbopuffer | Strong managed first-stage retrieval candidate. It combines vector search, BM25 full-text search, sparse vectors, filters, multi-query snapshot execution, strong/eventual consistency choices, batch writes, conditional writes, delete-by-filter, and namespace branching on an object-storage-backed architecture. Pricing is materially easier than running a cluster if the $64/month Launch minimum or $256/month Scale minimum is acceptable. | Not a canonical Yutome database. It has no joins/relational constraints/job queue/billing ledger. Hybrid search is multi-query plus client-side fusion/reranking, not one native LanceDB-style hybrid call. `limit.per` diversification/grouping is currently only supported for order-by-attribute queries, so Yutome's group-by-video path would need client-side grouping or an extra catalog query. Query pricing is based on logical bytes queried/returned, with a 1.28 GB minimum namespace size for queried bytes; writes have a 10 KB/request floor and batch discounts. Enterprise is at least $4,096/month plus a 35% usage premium, so use it only as retrieval paired with Postgres/D1 if Launch/Scale are enough. Sources: [pricing](https://turbopuffer.com/pricing), [docs](https://turbopuffer.com/docs), [query API](https://turbopuffer.com/docs/query), [hybrid search](https://turbopuffer.com/docs/hybrid-search), [write API](https://turbopuffer.com/docs/write). |
| Meilisearch | Mature product search: lexical search, filters, sort, pagination, highlighting, distinct, and hybrid/semantic search with embedders. | Not a catalog/job database. Ranking, filtering syntax, snippets, and raw FTS semantics differ. Requires another hosted service/container. Good search front-end, not full Yutome source of truth. Sources: [Meilisearch hybrid search](https://www.meilisearch.com/docs/learn/ai_powered_search/hybrid_search), [Meilisearch search API](https://www.meilisearch.com/docs/reference/api/search), [settings](https://www.meilisearch.com/docs/reference/api/settings). |
| Typesense | Strong near-parity for user-facing search: keyword, semantic/hybrid rank fusion, filters, sorting, grouping, pagination, facets, typo tolerance. Native `group_by`/`group_limit` maps well to Yutome group-by-video. | Single-substrate only if Yutome accepts denormalized document-store consistency for jobs, attempts, active transcript swaps, and catalog integrity. Better as search service paired with Postgres than as canonical DB. Sources: [Typesense vector/hybrid search](https://typesense.org/docs/30.2/api/vector-search.html), [Typesense search/filter/grouping](https://typesense.org/docs/30.2/api/search.html). |
| Weaviate | Strong direct chunk-search candidate: hybrid combines vector search with keyword BM25F over raw text properties, configurable weighting/fusion, filters, grouping, `limit`/`offset`, updates, deletes, and useful multi-tenancy primitives. | Single-substrate only if Yutome accepts denormalized document-store consistency. Cross-reference-heavy catalog/job state is not its strength; scalable production usually means Kubernetes or Weaviate Cloud. Better as search service paired with Postgres than as canonical DB. Sources: [Weaviate hybrid search](https://docs.weaviate.io/weaviate/search/hybrid), [BM25 search](https://docs.weaviate.io/weaviate/search/bm25), [filters](https://docs.weaviate.io/weaviate/search/filters), [installation](https://docs.weaviate.io/deploy/installation-guides). |
| Qdrant | Strong vector/hybrid engine with dense+sparse vectors, RRF/DBSF fusion, formula scoring, grouping, payload filters, and text/phrase payload filtering. Single-service ops are attractive. | Ranked lexical search is sparse-vector modeled, not a SQLite FTS5/BM25F text engine. It still needs SQL/D1/Postgres/SQLite for videos, jobs, attempts, and transcript state. Sources: [Qdrant hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/), [filtering](https://qdrant.tech/documentation/concepts/filtering/), [quickstart](https://qdrant.tech/documentation/quick-start/). |
| Milvus | Serious scalable vector database with raw text analyzed into BM25 sparse vectors, dense+sparse hybrid search, RRF/weighted rerankers, scalar filters, grouping search, upserts, and deletes. | Operationally heavy for small SaaS. Standalone Docker Compose includes Milvus plus etcd and MinIO, with materially larger memory/disk expectations than Typesense/Qdrant. Sources: [Milvus full-text search](https://milvus.io/docs/full-text-search.md), [multi-vector hybrid search](https://milvus.io/docs/multi-vector-search.md), [filtered search](https://milvus.io/docs/filtered-search.md), [standalone Docker Compose](https://milvus.io/docs/install_standalone-docker-compose.md). |
| OpenSearch / Elasticsearch | Best conventional search-platform parity: Lucene BM25/raw text, analyzers, phrase/proximity/query-string, highlighting, aggregations, filters, kNN/vector search, hybrid queries, RRF/score normalization, bulk updates/deletes. | JVM cluster ops are the largest burden: heap, shards, replicas, snapshots, upgrades, TLS/security, disk sizing. Good managed option, poor "easy self-host" default. Sources: [OpenSearch semantic/hybrid tutorial](https://docs.opensearch.org/docs/3.0/tutorials/vector-search/neural-search-tutorial/), [Elasticsearch RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion). |
| Vespa | Most powerful ranking platform: BM25 text, HNSW vector search, filters, custom rank profiles, RRF/global-phase reranking, grouping, and multi-phase ranking. | Too much surface area for V1 unless ranking is the product. Requires learning schemas, rank profiles, feed/deploy flow, and production cluster ops. Sources: [Vespa hybrid lab](https://learn.vespa.ai/vector-search/lab-hybrid/), [Vespa BM25](https://docs.vespa.ai/en/ranking/bm25.html), [nearest neighbor search](https://docs.vespa.ai/en/querying/nearest-neighbor-search.html). |
| Postgres + pgvector + built-in FTS | **Production fallback.** One durable SQL system can own catalog, jobs, billing, transcript state, chunks, FTS, vectors, transactional active transcript replacement, queue locking, and usage ledger. `pgvector` supports exact/approx vector search with HNSW/IVFFlat and ordinary SQL filters; Postgres FTS supports `tsvector`, phrase/web search queries, field weights, ranking, GIN/GiST indexes, and `ts_headline`. | Built-in FTS is not BM25 and hybrid ranking is custom SQL/application fusion. Use it only if VectorChord cannot pass the production database operations gate in time. Approx vector filtering needs partitioning/partial indexes/iterative-scan tuning. Sources: [pgvector README](https://github.com/pgvector/pgvector), [Postgres transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html), [Postgres text search controls](https://www.postgresql.org/docs/current/textsearch-controls.html), [Postgres FTS indexes](https://www.postgresql.org/docs/current/textsearch-indexes.html). |
| Postgres + ParadeDB + pgvector | Stronger Postgres search quality if Enterprise/BYOC is acceptable: ParadeDB brings Elastic-style BM25/fuzzy/faceted search inside Postgres and currently recommends `pgvector` alongside it for vector/hybrid search. | Do not use ParadeDB Community for paid hosted V1: ParadeDB docs discourage production applications serving paying customers because Community lacks WAL support and crash/restart can require reindex/downtime. Enterprise/BYOC is managed but sales-led; it advertises WAL replication, crash recovery, PITR, MVCC-safe BM25, concurrent non-blocking writes, BM25 scoring, highlighting, hybrid search, and efficient top-K. Sources: [ParadeDB deploy overview](https://docs.paradedb.com/deploy/overview), [ParadeDB Enterprise](https://docs.paradedb.com/deploy/enterprise), [ParadeDB text search overview](https://docs.paradedb.com/documentation/full-text/overview). |
| Postgres + VectorChord + VectorChord-BM25 | **Recommended hosted V1 default.** VectorChord Suite puts `vchord`, `vchord_bm25`, `pg_tokenizer`, and `vector` in Postgres; docs describe native BM25 + vector hybrid search with RRF or reranking. This most directly matches the desire for one DB with real BM25 and vectors. | Newer stack with maturity, managed availability, HA, replication, and non-English/tokenizer behavior to prove. Licensing is probably workable without explicit permission if Yutome uses the AGPLv3 option, keeps VectorChord components unmodified, and avoids ELv2 as the hosted-SaaS basis. Railway deployment likely means a custom Postgres image and therefore explicit DB operations ownership until a managed VectorChord-capable host is chosen. Sources: [VectorChord Suite](https://docs.vectorchord.ai/vectorchord/getting-started/vectorchord-suite.html), [VectorChord hybrid search](https://docs.vectorchord.ai/vectorchord/use-case/hybrid-search.html), [VectorChord Cloud limits](https://docs.vectorchord.ai/cloud/limit/cloud-limit.html), [VectorChord license](https://raw.githubusercontent.com/tensorchord/VectorChord/main/LICENSE), [VectorChord-BM25 README](https://github.com/tensorchord/VectorChord-bm25), [VectorChord-BM25 license](https://raw.githubusercontent.com/tensorchord/VectorChord-bm25/main/LICENSE). |

### Hosted DB Bakeoff

The serious hosted question is no longer "which vector database should sit next to SQLite?" It is:

```text
one canonical database for catalog/state/billing/jobs/chunk search
```

versus:

```text
SQL control plane + dedicated retrieval/search service
```

Prefer the first option if retrieval quality is acceptable. Run the bakeoff with `voyage-4-lite` embeddings unless measured speed, cost, or accuracy proves another embedding model wins.

Required feature gates:

- **Search modes:** lexical, semantic, hybrid, semantic-with-lexical-fallback, video/title-description search, grouped results by video, neighboring context lookup.
- **Lexical quality:** phrase queries, exact technical terms, boolean-ish query behavior, snippets/highlights, field boosts for title/description/text, and a documented replacement for current SQLite FTS5 raw mode.
- **Hybrid quality:** built-in fusion or reproducible RRF/weighted fusion; stable scores enough for tests; optional reranking path.
- **Filters:** `workspace_id`, `video_id`, `channel_id`, `source`, `language`, `is_generated`, `published_at`, `duration_seconds`, `ingest_status`, `live_status`, `selected`, `sequence`, `start_ms`, and `token_count`.
- **Mutation behavior:** idempotent upsert, delete-by-video/workspace, active transcript replacement, rebuild/resume, stale-row detection, backup/restore.
- **Billing hooks:** per-workspace write count, stored bytes/vectors, search query count, top-k/candidate count, embedding token count, and background compaction/indexing work.
- **Ops:** managed availability, backup/restore, HA path, connection pooling/serverless access, extension support, tenant isolation, and a self-hosted/container escape hatch if managed options become limiting.

Evaluate in this order:

1. **VectorChord Suite** as the V1 default.
2. **Managed Postgres + pgvector + built-in FTS** as the production fallback if VectorChord database operations are not ready.
3. **ParadeDB Enterprise/BYOC + pgvector** only if pricing, licensing, WAL posture, and managed deployment are acceptable.
4. **Typesense** and **Weaviate** as denormalized search-first challengers if Yutome is willing to enforce catalog/job consistency in application code.
5. **Turbopuffer** as the managed retrieval service control, paired with Postgres/D1 if its retrieval quality and logical-byte economics beat running search.
6. **OpenSearch/Elasticsearch** as the search parity ceiling, not the noob-hosting default.
7. **Qdrant** as a retrieval control if vector/sparse operations matter more than lexical/catalog parity.
8. **Meilisearch** only as the fastest/noobest search-service control.
9. **Milvus** and **Vespa** only if simpler systems fail quality or scale tests.

Near-term implementation shape for the bakeoff:

1. Export a representative Yutome corpus from current SQLite + LanceDB: videos, active chunks, chunk metadata, title/description text, and existing LanceDB vectors if compatible.
2. Re-embed with `voyage-4-lite` where needed and store model/dimension/version on every backend row.
3. Implement a thin `HostedSearchStore` adapter for each candidate with `upsert_catalog`, `replace_active_transcript`, `index_chunks`, `delete_video`, `lexical_search`, `semantic_search`, `hybrid_search`, `group_by_video`, `context_neighbors`, `enqueue_job`, `claim_job`, `record_usage`, and `health`.
4. Build eval queries from real Yutome usage: exact names, quoted phrases, acronym/code-ish terms, broad natural-language questions, recency filters, generated-vs-official transcript filters, and channel/video scoping.
5. Compare against current SQLite + LanceDB on recall@k, MRR/NDCG where labels exist, latency p50/p95, RAM/disk, operational steps, cost model, and failure/rebuild behavior.
6. Pick the hosted V1 substrate only after the bakeoff. Until then, treat Postgres + VectorChord Suite as the implementation default and managed Postgres + pgvector + Postgres FTS as the production fallback if VectorChord database operations are not ready. D1 + Vectorize remains a Cloudflare-only fallback, not the final architecture.

### Hosted Cloudflare Implication

For the assumed hosted Cloudflare frontend path, the pragmatic low-ops architecture changes:

- Cloudflare still owns the public app, auth/session edge, connector routing, lightweight scheduler triggers, and R2 artifact storage where useful.
- The canonical hosted Yutome database should be Postgres with VectorChord Suite if the bakeoff passes. Workers call the Yutome API/database layer rather than splitting state across D1 + Vectorize by default.
- Postgres stores authoritative catalog, jobs, transcript/chunk rows, embeddings, lexical indexes, usage ledger, billing reservations, and tenant state.
- Hybrid search runs in Postgres SQL or the Yutome API layer: lexical top-N + vector top-N + RRF/weighted fusion + optional rerank.
- D1 + Vectorize remains the fallback only if the priority becomes "no external database outside Cloudflare" rather than "one substrate with feature parity."

This still avoids user-owned Cloudflare setup. It also avoids hosters running SQLite and a separate vector database, at the cost of introducing one Postgres service outside the Cloudflare-only stack.

If VectorChord Suite or another Postgres-compatible stack wins, Cloudflare remains the edge/app layer and Postgres remains the canonical hosted database. If Turbopuffer, Typesense, Weaviate, or OpenSearch wins retrieval quality by a large margin, treat that as a deliberate two-substrate decision and document why the quality/cost gain is worth the added sync and billing complexity.

### Runtime And Database Placement Addendum

The hosted runtime should not treat Cloudflare, Fly, Modal, or Railway as interchangeable queues. The durable product state is Postgres; the compute substrate only claims and executes work. The detailed Fly/Modal/Railway comparison for database hosting, cron, ingest, networking, pricing, startup, and scaling lives in `docs/hosted-provider-broker-plan.md`. Default implementation is Railway-first.

Default placement:

- **Ingest execution:** Railway workers first. They claim Postgres jobs, run normal indexing work, and write final state back to Postgres. Store executor refs on jobs/operations, but keep the job row canonical.
- **Scheduled subscription refresh:** one Railway Cron service by default. The tick scans Postgres `source_refresh_policies`, locks due rows, enqueues discovery jobs, advances `next_run_at` with jitter, and exits. Skipped or late Railway Cron runs must be harmless because due state remains in Postgres.
- **Modal:** optional burst/backfill executor. Use `.spawn()` only when Railway workers become inefficient for large backfills, media-heavy fallback, or temporary parallel indexing bursts.
- **Fly Machines:** lower-priority fallback worker pool. Fly scheduled Machines are too coarse for per-workspace subscription auto-indexing; if Fly schedules are used, prefer Cron Manager, Supercronic, or an in-app scheduler that writes jobs to Postgres.
- **Railway:** default all-in-one hosted deployment with API services, worker services, cron services, private networking, volumes, and Postgres in one project.
- **Postgres/VectorChord:** not on Cloudflare. A VectorChord production database needs a Postgres host that supports the extension/image plus HA, backups, PITR or equivalent restore, monitoring, connection management, and extension upgrades.

Railway Postgres verdict:

- Railway custom VectorChord Postgres is the initial hosted database path. Railway is still the default deployment platform; VectorChord remains the default search/storage substrate.
- Railway's pgvector template or another managed Postgres + `pgvector` + built-in FTS host is the fallback if VectorChord database operations cannot pass the paid-production gate in time.
- The paid-production gate for Railway custom VectorChord Postgres is explicit database operations ownership: WAL/PITR or equivalent, backup restore drills, failover practice, security/version upgrades, vacuum/storage alarms, replica monitoring, connection management, and incident response. This is a launch gate for the default, not a reason to demote VectorChord from the target architecture.

Subscription auto-indexing implication:

- The user-facing toggle sets `source_refresh_policies.enabled`.
- The scheduler creates `discover_source` jobs only for due enabled policies.
- Discovery creates idempotent `index_video` jobs for newly seen subscription/channel/playlist videos.
- Disabling the toggle stops future discovery without deleting indexed content.
- Plan limits bound refresh cadence, new videos per run, concurrent jobs, proxy GB, Gemini fallback seconds, and embedding tokens.

Primary docs for this addendum: [Fly Managed Postgres](https://fly.io/docs/mpg/), [Fly extensions](https://fly.io/docs/mpg/extensions/), [Fly pricing](https://fly.io/docs/about/pricing/), [Fly Postgres unmanaged](https://fly.io/docs/postgres/), [Fly Machines](https://fly.io/docs/machines/overview/), [Fly task scheduling](https://fly.io/docs/blueprints/task-scheduling/), [Railway PostgreSQL](https://docs.railway.com/databases/postgresql), [Railway PostgreSQL HA](https://docs.railway.com/databases/postgresql-ha), [Railway PITR](https://docs.railway.com/volumes/point-in-time-recovery), [Railway cron/workers/queues](https://docs.railway.com/guides/cron-workers-queues), [Railway pricing](https://docs.railway.com/pricing/plans), [Modal scheduling](https://modal.com/docs/guide/cron), [Modal job processing](https://modal.com/docs/guide/job-queue), [Modal timeouts](https://modal.com/docs/guide/timeouts), [Modal autoscaling](https://modal.com/docs/guide/scale), [Modal resources](https://modal.com/docs/guide/resources), and [Modal pricing](https://modal.com/pricing).

### Decision

Use **Postgres + VectorChord Suite** as the hosted V1 default unless the bakeoff disproves it. Keep SQLite + LanceDB as the current local implementation, not the hosted architectural target.

Current candidate order:

1. **VectorChord Suite**: default one-substrate BM25/vector implementation; use only via the AGPLv3/unmodified-component path, and still prove HA, managed availability or accepted self-managed operations, and maturity.
2. **Managed Postgres + pgvector + built-in FTS**: fallback one-substrate hosted implementation if VectorChord operations are not ready.
3. **ParadeDB Enterprise/BYOC + pgvector**: only if paid production terms and WAL support are acceptable.
4. **Typesense** or **Weaviate**: if denormalized document-store consistency is acceptable or if paired with Postgres as a search service.
5. **Turbopuffer**: if managed retrieval quality and per-workspace logical-byte billing beat running search; not the canonical database.
6. **D1 + Vectorize**: Cloudflare-only fallback, not preferred under one-substrate parity.
7. **OpenSearch/Elasticsearch**: search parity ceiling if simpler systems fail.
8. **Qdrant/Meilisearch**: retrieval/noob-control candidates, not canonical database favorites.

Do not choose **LanceDB-only** unless the product is willing to become Lance-native and own app-level relational integrity, job semantics, compaction, and billing. Do not choose **Milvus** or **Vespa** for V1 unless corpus scale or ranking complexity becomes the product.
<!-- SUBAGENT-STORAGE-END -->

## Section C: Remote Connector And Hosted Cloudflare Adaptation

<!-- SUBAGENT-REMOTE-START -->
### Current Connector Mechanics

The current remote connector is a solid single-owner prototype, not yet a hosted multi-tenant service.

Current flow:

```text
Claude / ChatGPT / MCP client
  -> user-owned Cloudflare Worker /mcp
  -> OAuthProvider + YutomeMcpAgent
  -> YutomeRelay Durable Object named "default"
  -> /relay/connect WebSocket from local `yutome serve bridge`
  -> local contract.py handlers
  -> local SQLite + LanceDB + transcript artifacts
```

Important implementation facts:

- `src/yutome/cli.py` owns the noob setup path, deploys `cloudflare/yutome-capsule`, pushes `YUTOME_RELAY_TOKEN` and `YUTOME_PAIRING_CODE` as Worker secrets, saves local state, and starts the bridge.
- `src/yutome/remote_connection.py` stores a local `connection.json` with `endpoint_url`, `mcp_url`, `relay_token`, `pairing_code`, `mode`, `replica_enabled`, and Cloudflare resource metadata.
- `cloudflare/yutome-capsule/src/index.ts` exposes `/mcp`, `/authorize`, `/token`, `/register`, `/.well-known/oauth-*`, `/pair`, `/relay/connect`, `/relay/status`, and `/healthz`.
- `cloudflare/yutome-capsule/src/pairing.ts` gates OAuth consent with a printed pairing code and stores short-lived OAuth state in `OAUTH_KV`.
- `cloudflare/yutome-capsule/src/yutome-mcp-agent.ts` reads `contract.json`, registers the canonical `find`, `list`, `show`, `q` tools plus resources, and forwards calls to the relay.
- `cloudflare/yutome-capsule/src/yutome-relay.ts` is a Durable Object WebSocket relay. It accepts one local bridge, sends `{type:"job"}` frames, waits for `{type:"result"}`, and returns an offline error when no bridge is connected.
- `tests/test_contract_parity.py` protects the Python/TypeScript MCP contract and canonical scope `yutome.search.read`.
- `tests/test_setup_helpers.py` protects URL normalization, worker readiness polling, bridge start/status behavior, and deploy helper behavior.

Hosted implication: the protocol shape is reusable, but the identity model is wrong for hosted Yutome. Today the Worker is effectively "one Worker, one owner, one bridge, one pairing code." Hosted Yutome needs "one public service, many workspaces, many assistant clients, many bridge installs, and optional hosted replicas."

### Hosted Connector URL

Use one stable production connector URL:

```text
https://mcp.yutome.com/mcp
```

Do not require users to paste Cloudflare `workers.dev` URLs. The same URL should work for Claude, ChatGPT, and generic remote MCP clients. If a user belongs to multiple Yutome workspaces, the OAuth consent screen should ask which workspace/library to connect instead of encoding the workspace in the public URL.

Reasons:

- Claude remote connectors must be reachable from Anthropic cloud infrastructure, not `localhost` or a private network.
- ChatGPT Apps/custom MCP expects a public HTTPS MCP server URL.
- MCP Streamable HTTP expects a single endpoint such as `/mcp`.
- A stable URL makes assistant setup and support copy much easier.

Path-scoped URLs like `https://mcp.yutome.com/w/<workspace>/mcp` can be added later for enterprise/admin distribution, but the noob path should not make the user reason about workspace-specific connector URLs.

### Yutome Auth And Pairing

Hosted Yutome should replace the printed Worker pairing code with account-backed OAuth consent.

Hosted assistant OAuth flow:

```text
Assistant opens OAuth
  -> Yutome /authorize
  -> user signs into Yutome if needed
  -> user selects workspace/library
  -> user consents to yutome.search.read
  -> Yutome issues MCP access/refresh token
  -> assistant calls /mcp with Authorization: Bearer <token>
```

The OAuth grant props should carry:

```text
user_id
workspace_id
connector_grant_id
assistant_client_id
assistant_client_name
scope = yutome.search.read
mode = bridge | replica | hybrid
plan/entitlement snapshot or lookup key
```

Laptop bridge pairing remains separate from assistant OAuth. The bridge is a device/install credential, not an assistant credential.

Hosted bridge setup should look like:

```bash
yutome connect --hosted
```

or:

```bash
yutome connect --endpoint https://mcp.yutome.com/mcp --relay-token <install_token>
```

Preferred UX:

1. User signs into `app.yutome.com`.
2. Dashboard shows "Connect this computer" with a short-lived code or deep link.
3. CLI exchanges that code for a per-install bridge token.
4. CLI stores `endpoint_url`, `mcp_url`, `relay_url`, `workspace_id`, `install_id`, and the install token in local remote state.
5. `yutome serve bridge` connects with `Authorization: Bearer <install_token>`.

The current static `YUTOME_PAIRING_CODE` should not survive in hosted mode. It is fine for user-owned Worker/BYO mode.

### OAuth Client Registration Constraints

Hosted Yutome should implement MCP auth as a real OAuth 2.1 protected resource.

Required behavior:

- Serve protected resource metadata for the MCP resource.
- Return `401 Unauthorized` with `WWW-Authenticate: Bearer resource_metadata="..."` when auth is missing/invalid.
- Publish OAuth authorization server metadata.
- Support authorization code + PKCE S256.
- Echo/validate the MCP `resource` parameter and bind issued tokens to the `/mcp` resource audience.
- Validate issuer, audience, expiry, scopes, and workspace access on every MCP request.
- Never accept tokens in query parameters.

Client registration:

- Keep Dynamic Client Registration support for generic MCP clients.
- Support Client ID Metadata Documents because both Claude and ChatGPT support it and it avoids unbounded DCR client rows at scale.
- For Claude directory or higher-volume distribution, prefer CIMD or provider-held OAuth credentials over per-connection DCR when available.
- For ChatGPT, expect CIMD, DCR, or predefined OAuth clients. ChatGPT can also present OpenAI-managed mTLS; treat that as client identification, not a replacement for end-user OAuth.

Current code already uses `@cloudflare/workers-oauth-provider` with `/register`, PKCE S256 enforcement, `clientIdMetadataDocumentEnabled: true`, and scope `yutome.search.read`. Hosted work should preserve that shape but replace "pairing code proves owner" with "Yutome account session proves user and workspace."

### Preserve MCP Tool And Resource Contract

The current MCP contract must remain the source of truth:

- Tools: `find`, `list`, `show`, `q`.
- Resources: `yutome://chunk/{id}`, `yutome://video/{id}`, `yutome://channel/{id}`, `yutome://transcript/{id}`.
- Scope: `yutome.search.read`.
- Read-only tool annotations stay `readOnlyHint: true`.
- Tool responses should keep both text `content` and `structuredContent`.

Do not fork a separate hosted retrieval API. Instead:

```text
contract.py
  -> contract emit
  -> cloudflare/yutome-capsule/src/contract.json
  -> hosted Worker/Agent registration
  -> bridge dispatch or replica dispatch
```

OpenAI's data-only MCP guidance recommends `search` and `fetch` tools for ChatGPT deep research/company knowledge compatibility. If we need that distribution surface, add `search` and `fetch` as additive compatibility aliases over `find` and `show`/resources. Do not remove or rename the existing Yutome tools.

### Bridge Mode Vs Hosted Replica Mode

Hosted Yutome should support two modes behind the same `/mcp` URL.

#### Mode 1: Laptop Bridge

Bridge mode is the fastest hosted MVP because it reuses the current local retrieval stack.

```text
/mcp authenticated request
  -> resolve workspace_id from token
  -> choose active install/bridge for workspace
  -> Durable Object relay for workspace_id + install_id
  -> local yutome serve bridge
  -> local SQLite + LanceDB
```

Properties:

- Lowest migration risk.
- No hosted corpus required.
- Works only while the user's computer/bridge is online.
- Hosted costs are mostly Cloudflare request/DO/WebSocket costs.
- Gemini/Webshare/Voyage usage is only involved if the local machine runs jobs through managed hosted APIs.

Adaptation required:

- Change `YutomeMcpAgent.relay()` from `idFromName("default")` to a tenant/install-derived Durable Object name.
- Change `YutomeRelay` auth from a single `env.YUTOME_RELAY_TOKEN` to per-install token validation.
- Add bridge heartbeat metadata: `workspace_id`, `install_id`, `yutome_version`, `contract_version`, `corpus_counts`, `semantic_enabled`, and `last_seen_at`.
- Add a deterministic offline response that includes last seen time and whether a hosted replica exists.

#### Mode 2: Hosted Replica

Replica mode answers from Cloudflare-hosted state when no bridge is online.

```text
/mcp authenticated request
  -> resolve workspace_id from token
  -> hosted search/read adapter
  -> hosted catalog + artifact + vector/search substrate
```

Properties:

- Always-on assistant search.
- Requires hosted storage/search from Section B.
- Query-time semantic embeddings or vector queries become billable hosted usage.
- Ingestion, Webshare, Gemini fallback, transcript cleanup, and embedding jobs must pass through the entitlement/usage ledger.

The same `find`, `list`, `show`, `q`, and resource reads should work in both modes. If parity is incomplete, hosted mode should return explicit capability metadata rather than silently degrading search quality.

#### Mode 3: Hybrid

Hybrid mode is the eventual default:

1. Use local bridge when online and contract versions match.
2. Fall back to hosted replica when bridge is offline.
3. Include `served_from = "bridge" | "replica"` in structured responses and usage events.

### Multi-Tenant Isolation

Never derive tenant identity from tool arguments. Tenant context must come from the verified OAuth token or bridge install token.

Required isolation rules:

- Every OAuth grant maps to one `workspace_id`.
- Every bridge token maps to one `workspace_id` and one `install_id`.
- Every Durable Object name is derived from `workspace_id` plus `install_id` or `connector_grant_id`, not `"default"`.
- Every hosted search/storage query is scoped by `workspace_id`.
- Every usage event includes `workspace_id`, `user_id`, `connector_grant_id`, `assistant_client_id`, and `served_from`.
- Enforce per-workspace rate limits before dispatching to bridge or replica.
- Reject requests when a token's workspace no longer has the entitlement, subscription, or connector grant.

For shared D1/R2/vector resources, use tenant keys and mandatory query guards. For higher-trust or enterprise tiers, consider per-workspace buckets/indexes/namespaces, but do not require that for noob hosted MVP.

### Token And Secret Storage

Use different storage classes for platform secrets and per-tenant credentials.

Platform-level secrets:

- Polar webhook secret.
- Gemini API key(s).
- Webshare parent account/API credentials.
- Internal signing keys.

Store these as Cloudflare Worker secrets or Cloudflare Secrets Store bindings. Do not put them in Wrangler `vars`.

Per-tenant dynamic secrets:

- Bridge install tokens.
- OAuth refresh tokens/grants.
- Webshare sub-user IDs and generated sub-user credentials if needed.
- Connector grant records.

Do not store these as Worker secrets because Worker secrets are deployment-level, not dynamic tenant records. Store them in the hosted control-plane DB encrypted at rest, with only hashed bridge tokens used for lookup. The bridge token should be shown once, rotated from the dashboard, and revocable per install.

Local machine:

- `connection.json` may keep the hosted `endpoint_url`, `mcp_url`, `relay_url`, `workspace_id`, `install_id`, and bridge token with `0600` permissions.
- Continue excluding local `.env`, Gemini keys, Webshare credentials, Google OAuth tokens, logs, caches, and job internals from replica sync.

### Cloudflare Resource Layout

Recommended hosted layout:

```text
app.yutome.com
  -> Yutome frontend, account, billing, source setup, bridge setup

api.yutome.com
  -> control plane APIs, Polar webhooks, provider allocation, usage ledger

mcp.yutome.com/mcp
  -> remote MCP Streamable HTTP endpoint
  -> OAuth metadata, /authorize, /token, /register
  -> YutomeMcpAgent

mcp.yutome.com/relay/connect
  -> bridge WebSocket endpoint
  -> WorkspaceRelay Durable Object
```

Cloudflare primitives:

- Workers/Agents for frontend APIs, OAuth, and MCP.
- `McpAgent` for Streamable HTTP MCP sessions.
- Durable Objects for bridge relay, session coordination, and optional rate-limit counters.
- WebSocket Hibernation for low idle bridge cost.
- D1 for control-plane data that needs SQL queries and usage ledgers.
- R2 for transcript artifacts and export files in replica mode.
- Queue/Workflows for hosted ingestion and backfills.
- Storage/search substrate from Section B for hosted query mode.
- Jurisdiction-aware `McpAgent.serve(..., { jurisdiction: "eu" })` or equivalent routing for EU data residency if promised.

### Observability And Billing Hooks

Instrument at the MCP/relay boundary, not inside every tool implementation first.

For every MCP request:

```text
workspace_id
user_id
connector_grant_id
assistant_client_id/client_name
tool_or_resource
arguments_size_bytes
result_size_bytes
served_from = bridge | replica
duration_ms
status = ok | offline | auth_error | rate_limited | provider_error
error_code
request_id
```

For bridge mode:

- Count MCP tool/resource calls, relay dispatch duration, WebSocket messages, bridge online/offline transitions, and bytes returned.
- Usually bundle this into the subscription unless usage is extreme.

For replica mode:

- Add query embedding tokens, vector/query units, search rows read, R2 bytes read, and any Gemini/Voyage/Cloudflare AI usage.
- Emit `usage_events` compatible with Section D.
- If a query would exceed plan caps, return a structured MCP error explaining that the Yutome workspace is out of credits rather than leaking provider errors.

Operational dashboards need:

- Connector installs by assistant type.
- OAuth success/failure rate by client registration mode.
- Bridge online rate and last seen.
- Tool call latency p50/p95 by served mode.
- Replica parity failures.
- Cost per workspace and gross margin per provider.

### Migration From User-Deployed Worker

Keep `yutome connect --deploy` as "advanced/BYO Cloudflare" until hosted mode is stable.

Migration path:

1. Add hosted dashboard action: "Move this library to hosted connector."
2. User runs `yutome connect --hosted` or pastes a one-time dashboard code.
3. CLI saves hosted remote state and starts/restarts the same local bridge service.
4. User adds the new `https://mcp.yutome.com/mcp` connector in Claude/ChatGPT.
5. Old user-owned Worker remains usable until user runs `yutome disconnect --remove-cloudflare` or deletes it manually.

Important limitation: OAuth grants in Claude/ChatGPT are bound to the old Worker URL, so they cannot be silently transferred. The user must connect the hosted URL once. Local corpus data does not need to move for bridge mode.

Implementation changes:

- Add `provider = "yutome_hosted"` to remote state or widen the existing provider enum.
- Add `relay_url`, `workspace_id`, `install_id`, `bridge_token_expires_at`, and `hosted_account_url` to remote state.
- Keep `provider = "cloudflare"` for user-owned Worker mode.
- Add `connect --hosted`; keep `connect --deploy`.
- Make bridge WebSocket URL configurable instead of deriving only `/{base}/relay/connect`.
- Add tests that hosted mode never uses `idFromName("default")`, never accepts tenant IDs from tool args, and preserves `contract.json` parity.

### Sources Reviewed

- Claude custom connectors and remote MCP: <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>
- Claude connector auth, DCR, CIMD, PKCE, and callback constraints: <https://claude.com/docs/connectors/building/authentication>
- OpenAI Apps SDK auth for MCP, protected resource metadata, CIMD/DCR, PKCE, resource audience, and mTLS: <https://developers.openai.com/apps-sdk/build/auth>
- OpenAI MCP/data-only app guidance: <https://developers.openai.com/api/docs/mcp>
- MCP authorization spec: <https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization>
- MCP Streamable HTTP transport spec: <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- Cloudflare remote MCP guide: <https://developers.cloudflare.com/agents/guides/remote-mcp-server/>
- Cloudflare securing MCP servers and `workers-oauth-provider`: <https://developers.cloudflare.com/agents/guides/securing-mcp-server/>
- Cloudflare `McpAgent` API: <https://developers.cloudflare.com/agents/api-reference/mcp-agent-api/>
- Cloudflare Durable Object WebSocket Hibernation: <https://developers.cloudflare.com/durable-objects/best-practices/websockets/>
- Cloudflare Workers secrets: <https://developers.cloudflare.com/workers/configuration/secrets/>
<!-- SUBAGENT-REMOTE-END -->

## Section D: Hosted Data Model And Metering Ledger

Proposed core hosted tables:

```sql
users(id, email, created_at)
workspaces(id, owner_user_id, name, plan_id, created_at)
subscriptions(id, workspace_id, polar_customer_id, polar_subscription_id, status, current_period_start, current_period_end)
credit_balances(workspace_id, capability, balance_units, updated_at)
usage_reservations(id, workspace_id, user_id, job_id, capability, estimated_units, status, created_at, settled_at)
usage_events(id, workspace_id, user_id, job_id, source_id, provider, capability, unit, quantity, raw_cost_usd, occurred_at, provider_request_id, metadata_json)
provider_accounts(id, provider, scope, credentials_ref, status, created_at)
provider_allocations(id, workspace_id, provider, provider_account_id, external_subuser_id, limits_json, status)
sources(id, workspace_id, source_type, source_url, selected, auto_index_allowed, auth_grant_id, status, last_discovered_at, last_indexed_at)
source_refresh_policies(id, workspace_id, source_id, enabled, cadence_seconds, jitter_seconds, next_run_at, cursor_jsonb, max_new_videos_per_run, max_index_jobs_per_day)
jobs(id, workspace_id, source_id, job_type, status, idempotency_key, run_after, executor_kind, executor_ref, lease_owner, lease_expires_at)
```

The key principle is to store provider-native units exactly while showing product-native credits in the UI.

## Section E: Implementation Roadmap

### Phase 0: Keep Local/BYO Stable

- Do not break current local-first setup.
- Keep local SQLite + LanceDB working.
- Keep `yutome setup`, local MCP, and user-owned Cloudflare Worker path available for advanced users and migration fallback.

### Phase 1: Hosted Account, Billing, And Usage Ledger

- Add hosted backend with Yutome user/workspace accounts.
- Integrate Polar checkout, subscription webhooks, customer portal, and credit top-ups.
- Implement usage reservation and settlement before any Gemini/Webshare/embedding operation.
- Enforce workspace hard caps.
- Add admin/operator dashboards for provider spend, user spend, and stuck jobs.

### Phase 2: Managed Fetch + Gemini

- Move Webshare and Gemini credentials out of user setup and into Yutome-managed provider accounts.
- Add per-workspace Webshare sub-users or usage attribution.
- Add Gemini request accounting for cleanup and fallback routes.
- Add job preflight estimates before indexing a source.

### Phase 3: Hosted Remote Connector

- Replace user-owned `connect --deploy` default with a hosted Yutome connector URL.
- Use Yutome auth for pairing/consent.
- Route assistant MCP calls through Yutome-hosted Cloudflare infrastructure.
- Preserve the existing bridge protocol where helpful, but make the noob path account-backed.

### Phase 4: Hosted DB, Scheduler, And Runtime

- Run the Section B bakeoff with `voyage-4-lite` embeddings before committing to the hosted search substrate.
- Start implementation from Postgres + VectorChord Suite, then compare it against managed Postgres + pgvector + built-in FTS, ParadeDB Enterprise/BYOC, Typesense, Weaviate, Turbopuffer, OpenSearch/Elasticsearch, Qdrant, Meilisearch, D1 + Vectorize, and containerized SQLite + LanceDB.
- Treat Railway custom VectorChord Postgres as the first hosted database path; treat Railway Postgres/pgvector and other managed Postgres + FTS deployments as production fallbacks if the VectorChord operations checklist is not ready.
- Build the first hosted replica on the winning substrate, with D1 + Vectorize acceptable only if "Cloudflare-only" becomes more important than one-substrate feature parity.
- Sync indexed corpus data into the hosted canonical store: users/workspaces, videos, channels, active transcript metadata, chunks, artifact pointers, vectors, jobs, and usage events.
- Add Postgres-backed `source_refresh_policies`, a Railway Cron global scheduler tick, and idempotent discovery/indexing jobs for user-enabled subscription auto-indexing.
- Use Railway workers as the first hosted ingest executor; record executor refs in Postgres and use application concurrency caps to bound cost and provider rate limits.
- Keep a Modal executor interface for future burst/backfill jobs, but defer implementation until Railway worker data proves it is needed.
- Keep a Fly Machines worker smoke path only if hoster demand appears; do not rely on Fly scheduled Machines for dynamic per-user schedules.
- Make laptop-off queries work from ChatGPT/Claude.
- Maintain product parity tests against local SQLite + LanceDB behavior, while allowing the hosted backend to use Postgres query syntax and different internal ranking.
- Track ranking drift explicitly; do not promise identical ordering until evals prove it. Run any Workers AI/Gemini embedding switch as an A/B benchmark against `voyage-4-lite`, not as a default simplification.

### Phase 5: Noob Product Polishing

- Replace provider setup copy with capability copy:
  - "Reliable fetching"
  - "Transcript repair"
  - "Smart search"
  - "Assistant connector"
- Show estimates before expensive jobs.
- Explain paused jobs as "out of credits", "source blocked", "needs reconnect", or "provider degraded", not raw provider errors.

## Section F: Open Questions

- Should hosted mode use one shared Yutome provider account per provider, or pool accounts by region/plan/risk?
- Should Webshare sub-users be mandatory for hosted attribution, or is activity-log attribution sufficient for MVP?
- Does VectorChord Suite pass the hosted product parity evals strongly enough to justify keeping it as the default despite custom database operations?
- Which managed or self-managed VectorChord-capable Postgres host passes the production checklist? Railway custom VectorChord Postgres is the default implementation path, but it still needs an explicit ops budget and runbook before paid production.
- If a search-first system wins retrieval quality, is the quality gain large enough to justify abandoning the one-substrate preference and adding Postgres/D1 as the control-plane database?
- For Turbopuffer specifically, do its $64/$256 monthly minimums and logical-byte query/write billing beat self-hosting once Yutome has many small tenants, and can client-side grouping/fusion preserve current Yutome retrieval behavior?
- After a Railway-first beta, which jobs need Modal burst execution, if any: backfills, large media fallback, embedding/index rebuilds, or none?
- Is there strong speed+cost+accuracy evidence to replace `voyage-4-lite` with Cloudflare Workers AI `bge-m3`/`qwen3-embedding-0.6b` or Gemini Embedding for hosted mode?
- Should transcript cleanup be synchronous for small jobs and Gemini Batch for backfills?
- Should subscriptions include monthly credits, or should credits always be separate prepaid top-ups?
- What auto-index cadence and per-run limits are safe defaults for subscription sources by plan?
- What privacy guarantees do we make around transcript text passing through Gemini, Webshare, and Yutome-managed storage?

## Section G: Agents As Paying Users

<!-- SUBAGENT-AGENT-BILLING-START -->
### Decision

Support agents as first-class **spending principals**, not as independent legal payers in V1. The normal funded account is still a human, team, company, or hoster workspace paid through Polar. Agents receive delegated wallets, scopes, budgets, and API credentials under that payer. This lets autonomous clients use Yutome without every agent having to complete card checkout, tax details, KYC, or customer-portal flows.

Do not make x402, Stripe machine payments, or direct stablecoin settlement the source of truth for usage. They can become optional funding rails later. The authoritative state remains Yutome's ledger:

```text
payer funds workspace -> agent receives delegated budget -> operation reserves credits -> provider call runs -> actual usage settles -> audit event records actor and payer
```

This matters because Gemini, Webshare, Voyage, and Cloudflare spend must still be controlled before execution. An agent payment protocol can fund a wallet, but it cannot replace Yutome's provider-cost reservation and settlement layer.

### What Polar Can Ship Now

Polar is suitable for:

- Human user checkout.
- Team or company billing via one Polar customer.
- Subscription plans and one-time credit top-ups.
- Customer portal, invoices, receipts, payment-method recovery, cancellations, and refunds.
- Optional customer-visible usage meters and prepaid credits.
- Webhook-driven reconciliation into Yutome.

Polar's Credits docs support prepaid usage and credits-only spending by not creating a metered price. Polar also states that it does not block usage when a customer exceeds their balance, so Yutome must enforce budgets itself. The Customer Portal is email/payment-method oriented and always available; it is appropriate for human/legal-entity payers, not for an autonomous agent wallet. Sources: [Polar credits](https://polar.sh/docs/features/usage-based-billing/credits), [Polar meters](https://polar.sh/docs/features/usage-based-billing/meters), [Polar customer portal](https://polar.sh/docs/features/customer-portal), [Polar webhooks](https://polar.sh/docs/integrate/webhooks/events).

V1 product model:

- `billing_account`: one Polar customer per human, team, company, or hoster workspace.
- `workspace`: Yutome resource boundary under a billing account.
- `human_user`: can manage billing, agents, sources, and caps according to role.
- `agent_account`: can spend delegated Yutome credits and call scoped APIs/MCP tools, but cannot change billing unless explicitly granted.
- `credit_wallet`: internal Yutome balance, optionally mirrored to Polar meter balances for customer visibility.

An agent "pays" in V1 by spending from a delegated wallet funded by a human/org payer. This is the right default because provider ToS, tax receipts, disputes, chargebacks, refunds, and privacy consents still need a responsible payer.

### First-Class Agent Principals

Add principals independent of humans:

```sql
principals(
  id,
  type, -- human_user | org | service_account | agent
  workspace_id,
  display_name,
  status,
  created_at,
  revoked_at
)

billing_accounts(
  id,
  polar_customer_id,
  legal_payer_principal_id,
  default_workspace_id,
  billing_status,
  created_at
)

agent_accounts(
  principal_id,
  owner_principal_id,
  external_agent_id,
  developer_name,
  contact_uri,
  public_key_jwk,
  default_wallet_id,
  created_at
)

agent_credentials(
  id,
  agent_principal_id,
  kind, -- api_key | oauth_client | signed_jwt_key
  hashed_secret_or_key_id,
  scopes,
  expires_at,
  last_used_at,
  revoked_at
)

credit_wallets(
  id,
  workspace_id,
  payer_billing_account_id,
  owner_principal_id,
  currency, -- yutome_ai_credit | fetch_mib | hosted_query
  balance,
  expires_at
)

spend_policies(
  id,
  wallet_id,
  principal_id,
  max_per_request,
  max_per_day,
  max_per_month,
  allowed_capabilities,
  allowed_sources,
  allowed_provider_routes,
  approval_required_above,
  status
)
```

Agent auth options:

- **API keys for MVP**: easy for local agents and cron jobs; store only hashes; show prefix, last used, scopes, and revocation state.
- **OAuth client credentials**: better for machine-to-machine hosted clients; issue short-lived access tokens with scopes such as `mcp:query`, `sources:read`, `sync:start`, `ai:repair`, `fetch:use`, and `wallet:spend`.
- **Signed requests later**: support `private_key_jwt`, DPoP-style proof, or wallet-signed requests when agent ecosystems settle.

Agent controls:

- Per-agent wallet allocation and monthly budget.
- Per-request maximum cost.
- Provider route allowlist: e.g. `voyage-4-lite` only, Gemini batch only, no video fallback, Webshare max GB per job.
- Tool scopes: read-only search agents should not be able to start ingest, spend Webshare bandwidth, or trigger Gemini fallback.
- Source scopes: restrict an agent to selected channels, projects, or corpora.
- Kill switch: revoke one agent without disabling the payer account.
- Audit trail: every reservation and provider event records `actor_principal_id`, `agent_principal_id`, `payer_billing_account_id`, `wallet_id`, `source_id`, `job_id`, scopes, and credential id.

### Billing Provider Reality Check

| Provider | Good for | Agent-payment fit |
|---|---|---|
| Polar | Human/org SaaS checkout, subscriptions, prepaid credits, MoR tax handling, portal, webhooks | Stripe-backed payment rail with Polar as MoR. Use as payer/receipt system for humans and organizations. It does not remove the need for Yutome agent principals, delegated wallets, or hard-cap enforcement. |
| Stripe Billing | Programmable cards, invoices, subscriptions, usage billing, stablecoin checkout | Stronger API surface than most, but unless using a specific MoR/marketplace setup, Yutome remains merchant/tax owner. Stablecoin acceptance is currently limited to eligible businesses. |
| Stripe machine payments | x402/MPP-style machine payments, as low as `0.01 USDC`, and Stripe settlement/reporting | Interesting later rail for machine-funded top-ups or paid endpoints. Still not a replacement for Yutome's ledger. Sources: [Stripe machine payments](https://docs.stripe.com/payments/machine), [Stripe x402](https://docs.stripe.com/payments/machine/x402), [Stripe MPP](https://docs.stripe.com/payments/machine/mpp). |
| Stripe stablecoin payments | Payment Links, Checkout, Elements, or PaymentIntents; USDC payments settle into Stripe balance in USD | Useful only if Yutome uses Stripe directly or alongside Polar. Current docs say only US businesses can accept stablecoin payments, while customers can pay globally. Source: [Stripe stablecoin payments](https://docs.stripe.com/payments/accept-stablecoin-payments). |
| Paddle | Mature MoR-style SaaS billing, subscriptions, transactions, customer portal links, global tax/payment coverage | Customer/subscription/invoice oriented. Good alternate human/org payer system, not native wallet/agent 402 spending. Source: [Paddle developer docs](https://developer.paddle.com/). |
| Lemon Squeezy | MoR checkout and subscription usage records | Usage billing is retrospective on subscription renewal, so it is a poor enforcement source for bursty provider spend unless Yutome keeps prepaid caps internally. Source: [Lemon Squeezy usage billing](https://docs.lemonsqueezy.com/help/products/usage-based-billing). |
| Dodo Payments | MoR, one-time payments, subscriptions, usage-based and credit-based billing for SaaS/AI products | Promising alternate to Polar, but still should feed Yutome's own ledger; do not make Dodo the runtime authorization layer. Source: [Dodo docs](https://docs.dodopayments.com/). |

### Emerging Agent Payment Rails

**x402** is the most relevant machine-native rail. Coinbase describes it as an HTTP 402 payment protocol for humans and AI agents to programmatically pay for APIs/content without accounts, sessions, or complex authentication. The server returns `402 Payment Required` with payment instructions; the client signs/pays; a facilitator verifies/settles; the server returns the resource. Coinbase's facilitator exposes `verify` and `settle` APIs, and the docs list a free tier of 1,000 facilitator transactions/month then `$0.001/transaction`. Sources: [Coinbase x402 overview](https://docs.cdp.coinbase.com/x402/docs/client-server-model), [Coinbase x402 facilitator](https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/x402-facilitator).

Cloudflare already documents x402 for Workers/Agents and MCP tools. Their docs call out paid HTTP content, paid MCP tools, client wrappers, and an `upto` scheme where the client authorizes a maximum and actual charge is determined at settlement. That is directionally useful for variable-cost provider calls, but V1 should avoid per-call on-chain settlement for expensive Yutome jobs because Gemini/Webshare usage can exceed estimates. Source: [Cloudflare x402](https://developers.cloudflare.com/agents/x402/).

Stripe is also moving into machine payments. Stripe's machine-payment docs say agents can pay for resources programmatically, sellers can charge API calls as low as `0.01 USDC`, and payments can settle into the Stripe balance. Stripe supports Base x402 with USDC and MPP on Solana/Tempo, plus card-network payments through shared payment tokens for eligible US legal entities. This is promising but still early/frontier enough that it should be an optional rail after Polar-backed hosted billing is stable. Sources: [Stripe machine payments](https://docs.stripe.com/payments/machine), [Stripe x402](https://docs.stripe.com/payments/machine/x402), [Stripe MPP](https://docs.stripe.com/payments/machine/mpp).

Coinbase stablecoin checkout and Circle/Circle Payments Network are relevant for later treasury or stablecoin checkout, not the fastest V1. Coinbase Payment Acceptance is positioned for payment platforms, PSPs, marketplaces, and enterprises with onboarding-gated API access; Coinbase Business availability is currently constrained in transition docs. Circle's payments pages emphasize USDC, institutional/payment-network access, and 24/7 settlement, but this is not a drop-in consumer SaaS checkout replacement. Sources: [Coinbase Payment Acceptance](https://docs.cdp.coinbase.com/payments/payment-acceptance/overview), [Coinbase Business transition](https://help.coinbase.com/en/transitioning-from-coinbase-commerce-to-coinbase-business), [Circle payments](https://www.circle.com/use-case/payments).

### Pragmatic Yutome Architecture

V1:

1. Use Polar for human/org subscription and top-up checkout.
2. Create agent accounts inside Yutome workspaces.
3. Let humans/org admins allocate prepaid credit wallets to agents.
4. Require every agent request to pass auth, scope, budget, and reservation checks before provider execution.
5. Return structured "insufficient credits" errors with a human-facing top-up URL when the payer needs to fund the workspace.

V1 request flow:

```text
agent request
  -> authenticate API key / OAuth client
  -> resolve workspace + payer + wallet
  -> check scopes and source permissions
  -> estimate Gemini/Webshare/Voyage/Cloudflare units
  -> reserve Yutome credits
  -> execute provider work
  -> record provider-native usage
  -> settle reservation
  -> emit audit + optional Polar usage event
```

Optional V1.5:

- Add `POST /billing/topup-intents` so an agent can request a checkout/top-up URL for its payer.
- Allow agents with `wallet:request_topup` to ask for more budget, but require human/org approval unless the payer pre-authorized automatic top-ups below a threshold.
- Add notification hooks: email, Slack, webhook, or MCP resource event when an agent is blocked by budget.

V2 machine-native funding:

- Add an x402 or Stripe machine-payment endpoint for **fixed credit packs**, not arbitrary provider execution.
- Example: `GET /pay/ai-credits/1000` returns `402 Payment Required`; after settlement, Yutome credits the agent wallet.
- For MCP, expose paid top-up tools separately from expensive operational tools: `yutome.billing.quote_topup`, `yutome.billing.fund_wallet`, `yutome.billing.balance`.
- Keep provider calls behind normal Yutome reservation even when the wallet was funded by x402.

Avoid in V2:

- Do not let an unauthenticated x402 payment directly trigger a Gemini/Webshare job with no Yutome account boundary.
- Do not expose provider API keys to agents.
- Do not rely on on-chain transaction success as proof that Yutome can safely run a variable-cost job.
- Do not mix Polar MoR invoices and direct crypto receipts in the same customer statement without explicit accounting/tax handling.

### Compliance And Product Constraints

- An autonomous agent is usually not a legal counterparty. Yutome still needs a human/org payer for ToS acceptance, privacy disclosures, tax details, refunds, abuse handling, and chargeback/dispute responsibility.
- Direct crypto/stablecoin rails can reduce card friction and enable machine-native top-ups, but they also make Yutome the merchant/payment operator for those payments unless a provider explicitly acts as MoR.
- x402 has excellent ergonomics for pay-per-API-call access, but it does not solve sales tax/VAT, account ownership, data-processing consent, refund policy, sanctions/geography policy, or provider ToS.
- Agent budgets must be deny-by-default. A prompt-injected or compromised agent should hit a small Yutome cap, not the hoster's Gemini/Webshare bill.
- For hosted Yutome, all provider spend remains prepaid/reserved before execution: Gemini tokens, Webshare sub-user bandwidth, Voyage embedding tokens, hosted search/storage work, Cloudflare connector usage, and any container costs.

### Implementation Plan

Phase 1: Polar-funded agents

- Add `principals`, `agent_accounts`, `agent_credentials`, `credit_wallets`, `spend_policies`, and `audit_events`.
- Extend `usage_reservations` and `usage_events` with actor/payer fields.
- Build agent API key creation, revocation, scopes, budget UI, and per-agent usage page.
- Add provider gateway enforcement so Gemini, Webshare, Voyage, hosted search/storage, and Cloudflare connector calls cannot run without a reservation.

Phase 2: Agent-facing MCP and API ergonomics

- Add MCP-visible billing resources: current wallet balance, last usage events, blocked reason, and top-up request link.
- Add typed errors: `insufficient_credits`, `cap_exceeded`, `scope_denied`, `source_denied`, `provider_route_denied`.
- Add webhooks for `agent.blocked`, `wallet.low_balance`, `reservation.settled`, and `agent.revoked`.

Phase 3: Optional machine funding rail

- Prototype x402 on Cloudflare for fixed top-up packs only.
- Store payment artifacts: quote id, transaction hash/payment intent id, wallet address, network, token, facilitator, fiat conversion, credited wallet id, and refund status.
- Gate production behind allowlisted agents and small maximum top-ups until accounting and compliance are settled.

Default recommendation: ship Polar + delegated agent wallets first. Add x402/Stripe machine payments only after agent demand is real and only as an additional way to fund the same Yutome ledger.
<!-- SUBAGENT-AGENT-BILLING-END -->
