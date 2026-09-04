# Response-triggered Instagram Follow-ups

## Goal

Make general `direct_message` follow-ups eligible only after the recipient replies to the initial comment private reply, while retaining legacy `prompt_request` behavior.

## Design

An account-scoped direct-interaction row is created only after the initial private reply succeeds. It records the account, recipient, source comment, initial-reply timestamp, and (once claimed) the inbound reply that consumed it. The inbound message path can therefore match only an unconsumed interaction owned by the routed account.

The existing follow-up job remains the only durable delayed-work mechanism. Response-triggered jobs receive a nullable provider-window expiry equal to 24 hours after the qualifying reply's provider timestamp, preserved through durable ingress. Missing/invalid/future timestamps and replies predating the initial private DM do not qualify. Legacy jobs retain a null expiry. The worker fails an expired response-triggered job without making a Graph request, with a second deadline check after credential resolution immediately before dispatch.

## Constraints

- Do not schedule general direct-message follow-up work from a comment.
- Preserve existing account-scoped active-job and seven-day sent-job dedupe.
- Preserve prompt extraction, prompt requests, pending access, prompt follow-ups, OAuth credentials, publishing, Cron, and leases.
- Log only fixed allowlisted status/reason labels.
- The original implementation was local-only. The subsequent approved rollout authorizes commit/push and staging code/migration only; production, Railway variables, automation configuration and live Instagram interactions remain out of scope.

## Final-review verification

The migration is exercised against populated legacy state, including safe empty-state downgrade/re-upgrade and both incompatible-data downgrade guards. The 24-hour limit follows [Meta's private-reply documentation](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-23eacf45-3728-4e41-bcc7-6d164959327c); processing latency does not extend it.
