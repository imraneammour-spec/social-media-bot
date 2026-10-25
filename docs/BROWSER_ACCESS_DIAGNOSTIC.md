# Browser access diagnosis — 24 October 2026 (UTC)

**Superseded on 25 October:** authenticated Chrome and OCI Cloud Shell now work
in this conversation. Oracle activation, home region and actual capacity reports
were inspected successfully. Do not repeat registration or the troubleshooting
below as a current prerequisite. No registry/authentication changes were made by
the agent. See `FREE_HOSTING_ASSESSMENT.md`; OCI capacity, not browser access, is
the remaining Oracle blocker. The observations below describe the earlier state.

Read-only checks from the bundled Browser plugin confirmed:

- Google Chrome is running.
- ChatGPT for Chrome is installed and enabled in the selected Default profile.
- Browser/Chrome plugins are enabled in the local Codex configuration.
- The native host manifest file exists, but the expected Windows registration
  `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.openai.codexextension`
  is absent. The plugin's native-host diagnostic therefore exits 1.
- Separately, the browser tool rejects inventory and direct browser access with
  `Local browser use is disabled in Work settings`, before Oracle can be inspected.
  A fresh turn and a browser-session reset did not change that result.

The missing native-host registration is a confirmed transport setup defect;
it has not been established as the cause of the separate Work access rejection.
Opening/pinning the Chrome extension did not repair the missing registration.
No browser cookies, saved passwords or authentication sessions were read.

The bundled `docs/chrome-troubleshooting.md` says:
> Do not install or repair the native host yourself.

It directs the user to reinstall the **Browser plugin from the ChatGPT plugin
UI** when the native-host manifest/registration is invalid. This means the plugin
inside the desktop application's Plugins interface. It is separate from pinning
the browser extension. No registry or permission setting was changed by the agent.

If registration becomes valid but Work still rejects access, preserve this tool
result for application support; do not repeatedly toggle Chrome or ask for new
"continue" messages as if that were a verified fix. Do not bypass the access gate
with browser profile/cookie extraction, arbitrary CDP, or UI scripting substitutes.
No Oracle account state, capacity or cost can be inferred from this failure.
