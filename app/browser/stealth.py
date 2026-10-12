"""Anti-detection (stealth) patches that shrink the automation fingerprint.

Humanization (paced typing, curved cursor, lazy scroll) makes *behaviour* look
human, but it does nothing about the *fingerprint* a site reads from JS and the
launch flags — `navigator.webdriver === true`, the "Chrome is being controlled by
automated test software" banner, the `--enable-automation` switch, a missing
`window.chrome`, and so on. Hardened identity providers (Google, etc.) gate on
exactly these, so behaviour alone never gets through.

This module supplies two things the controller applies at launch:

* :func:`launchArgs` / :func:`ignoreDefaultArgs` — Chromium flags that remove the
  automation switches.
* :data:`STEALTH_INIT_JS` — an init script (run before any page script) that
  patches the most common JS tells.

None of this is a guarantee: aggressive IdPs may still block automated contexts.
It is best paired with the real-Chrome channel (``ABC_BROWSER_CHANNEL=chrome``), a
visible window (``login_session``), and a persistent profile. See the README's
"Logging in" note. Toggle with ``ABC_STEALTH`` (default on).
"""

from __future__ import annotations

# Chromium launch flags that drop the obvious automation markers.
_STEALTH_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-infobars",
)

# Default args Chromium adds that we want gone (the automation switch + the
# "controlled by automated software" infobar that rides on it).
_IGNORE_DEFAULT_ARGS: tuple[str, ...] = ("--enable-automation",)


def launchArgs() -> list[str]:
    """Return the stealth launch flags to merge into the context args."""
    return list(_STEALTH_ARGS)


def ignoreDefaultArgs() -> list[str]:
    """Return the default Chromium args to suppress."""
    return list(_IGNORE_DEFAULT_ARGS)


# Runs in every page/frame before site scripts. Patches the common JS tells:
# navigator.webdriver, a plausible window.chrome, non-empty plugins/languages,
# and the permissions-query quirk headless Chrome exposes for notifications.
STEALTH_INIT_JS: str = r"""
(() => {
  try {
    // The single biggest tell: navigator.webdriver === true under automation.
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}

  try {
    // Real Chrome exposes window.chrome; headless/automation often does not.
    if (!window.chrome) {
      window.chrome = { runtime: {} };
    }
  } catch (e) {}

  try {
    // languages is empty in some headless configs.
    if (!navigator.languages || navigator.languages.length === 0) {
      Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    }
  } catch (e) {}

  try {
    // A zero-length plugins array is a classic automation signal.
    if (navigator.plugins && navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    }
  } catch (e) {}

  try {
    // Headless Chrome returns 'denied' for Notification permission via query
    // while Notification.permission says 'default' — normalise the mismatch.
    const original = window.navigator.permissions && window.navigator.permissions.query;
    if (original) {
      window.navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : original(params);
    }
  } catch (e) {}
})();
"""
