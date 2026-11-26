# Postenza Customer Onboarding Design

## Purpose

Postenza will add a short, persisted onboarding flow for newly registered customers before they enter the tenant application. The flow records product intent, acquisition source, and an informational plan selection without adding billing, entitlements, account limits, or feature gates.

## Scope

This change is limited to customer onboarding, the authentication and tenant-entry gates needed to enforce it, and read-only Admin reporting of onboarding answers. It does not redesign the customer application, alter Instagram provider behavior, add payment processing, or continue unrelated cleanup work.

## Persistence and legacy compatibility

Create one additive `user_onboarding_profiles` table with:

- `user_id`: UUID primary key and foreign key to `users.id` with cascade deletion.
- `use_case`: nullable bounded string.
- `main_goal`: nullable bounded string.
- `onboarding_source`: nullable bounded string.
- `selected_plan`: nullable bounded string.
- `completed_at`: nullable timezone-aware timestamp.
- `created_at` and `updated_at`: non-null timezone-aware timestamps.

The application accepts only documented allowlist values for the four answer fields. It stores no free-form onboarding response.

The migration creates the new table and does not insert historical rows. For a user that existed before this feature, absence of a profile is the backward-compatibility signal that onboarding is complete. Such users retain their existing login, application, Instagram connection, internal Prompt, and Admin behavior. Their plan and acquisition source remain unknown rather than being fabricated.

New browser registrations have a stronger invariant: the `users` row and an empty `user_onboarding_profiles` row are inserted in the same database transaction. The registration is successful only after both rows flush successfully. Any profile insertion or transaction failure rolls back the new user, returns the existing safe registration-service failure, and does not authenticate or leave a partially initialized user. Application code must not create a newly registered user without its incomplete profile.

The migration downgrade checks for any onboarding profile row and aborts if data exists. It drops the table only when empty, matching the repository's data-preserving downgrade policy.

## Server-controlled state machine

The existing `/onboarding` endpoint supports GET and POST. The authenticated session UUID is the only user identity accepted. The server reads that user's profile and determines the first incomplete step; a submitted step must match that expected step.

1. `profile`: collect `use_case` and `main_goal` together.
2. `source`: collect the required `onboarding_source`.
3. `plan`: collect the required informational `selected_plan` and set `completed_at` atomically.
4. `instagram`: reuse the current Instagram connection/status presentation after questionnaire completion.

Each valid intermediate POST persists only its own allowlisted fields and redirects to `/onboarding`, where the next step is derived again from persisted state. Invalid or out-of-order submissions do not mutate data. They render the expected step with an accessible localized inline error and retain safe values submitted for that step.

When the plan step completes, the server redirects a user who already owns a ready connected Instagram account to `/app`. Otherwise it redirects to `/onboarding`, which now renders the existing Instagram connection/status step. Successful Instagram OAuth callbacks use the same post-onboarding destination decision: incomplete users return to the questionnaire, while completed or legacy-compatible users follow existing connection-based routing.

## Gating

`post_login_destination()` checks onboarding completeness before checking Instagram connection state. The tenant dashboard context applies the same persisted check before resolving or rendering tenant accounts. An incomplete newly registered user therefore cannot enter `/app` by forging a URL, account reference, form field, query parameter, or client-side state.

The internal Admin route remains governed exclusively by `current_user_has_legacy_workspace_access()` and is not routed through the customer onboarding gate. Legacy internal owners without a profile retain access. Existing Prompt capability and legacy workspace boundaries are unchanged.

Instagram OAuth state, token exchange, credentials, account ownership, and disconnect behavior remain unchanged. Connection initiation is presented only after questionnaire completion. Callback routing changes only the post-success destination; provider and persistence semantics are untouched.

## Allowlists

Use cases:

- `personal_growth`: Grow my own Instagram account
- `business_account`: Manage a business account
- `agency_clients`: Manage client accounts / agency
- `content_publishing`: Content publishing
- `comment_dm_automation`: Comment/DM automation
- `other`: Other

Main goals:

- `save_time`: Save time
- `generate_leads`: Generate leads
- `increase_engagement`: Increase engagement
- `consistent_posting`: Stay consistent with posting
- `multiple_accounts`: Manage multiple accounts
- `other`: Other

Acquisition sources:

- `google`: Google
- `instagram`: Instagram
- `tiktok`: TikTok
- `youtube`: YouTube
- `friend_colleague`: Friend or colleague
- `github`: GitHub
- `ai_tool`: AI tool / ChatGPT
- `other`: Other

Plans:

- `free`: core image publishing and automation positioning
- `pro`: growing workflows and expanded automation positioning
- `agency`: multi-client and multi-account positioning

Every plan is labelled “Pricing coming soon.” The onboarding page states that selection records the user's preference and does not change current access. No selected plan affects features, account capacity, usage, billing, or entitlements in this phase.

## Presentation and localization

The flow extends the existing Postenza auth/onboarding shell. It uses a visible four-step progress indicator, large native radio controls presented as selectable cards, one primary submit action per questionnaire step, keyboard-visible focus, responsive layouts, and the existing locale selector. All new customer-facing text has EN, FR, and AR catalog entries with placeholder parity; Arabic renders through the shell's RTL behavior.

The fourth step preserves the existing account status, reconnect, disconnect, connect, and sign-out forms. Existing CSRF and opaque account-action references remain in use.

## Admin reporting

The Admin dashboard stays GET-only and read-only. Its snapshot query adds:

- plan distribution for `free`, `pro`, and `agency`;
- onboarding-source distribution for each acquisition-source allowlist value;
- an `Unknown / not provided` count for users without a profile or without the relevant answer.

Counts cover registered users consistently, so each distribution totals the registered-user count. Historical users are not assigned synthetic answers. The Admin loads aggregates only and does not expose onboarding records across customer routes.

## Error handling and security

- Every onboarding POST requires the existing session-bound CSRF token.
- Every value is validated server-side against a fixed allowlist.
- Required source and plan values cannot be empty.
- Unexpected database failures return the existing safe authentication-service unavailable response.
- Registration transaction failures expose neither database details nor partial user state.
- Onboarding forms contain no user ID, tenant ID, account ID, role flag, plan entitlement, or trusted completion flag.
- Onboarding data is accessed only for the authenticated session user; Admin aggregates remain behind the established internal-owner capability.
- No provider credentials, tokens, password hashes, webhook payloads, session data, or secret values are queried into onboarding presentation models.

## Verification

Test-first coverage will prove:

- registration atomically creates both user and incomplete profile, and rolls both back on profile failure;
- newly registered and incomplete users are directed to onboarding and blocked from tenant pages;
- profile absence preserves access semantics for legacy users;
- expected-step enforcement, CSRF, required fields, all allowlists, no-mutation failures, and safe value preservation;
- persisted completion and correct connected/disconnected destinations;
- existing Instagram connection and OAuth callback behavior;
- internal Admin access and internal Prompt capability remain unchanged;
- real Admin plan/source distributions, including unknown historical users;
- tenant isolation and absence of billing or entitlement behavior;
- EN/FR/AR catalog and placeholder parity, Arabic RTL, Jinja parsing, JavaScript syntax, Python compilation, and Git whitespace checks;
- the broad existing auth, OAuth, onboarding-status, tenant-dashboard, product, and Admin regression suites.

## Explicit exclusions

No payment provider, checkout, price, subscription, entitlement, usage limit, account limit, plan-based feature branch, onboarding free text, Admin write action, Railway change, environment-variable change, production migration execution, deployment, commit, push, or unrelated cleanup is part of this implementation.
