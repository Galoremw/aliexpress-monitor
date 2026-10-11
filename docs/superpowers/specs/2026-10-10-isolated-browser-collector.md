# Isolated Browser Collector

## Goal

Run this computer's browser collection queue in a dedicated Chrome user profile so collection tabs and focus changes cannot alter the user's everyday Chrome profile.

## Requirements

- The local everyday Chrome profile is not a browser-collection worker, including when the dashboard queues or resumes a run.
- The scheduled collector starts a dedicated Chrome profile at `%LOCALAPPDATA%\AliExpressMonitor\BrowserCollectorProfile` with the project extension and local dashboard.
- The dedicated profile is explicitly identified by `browser_collection=1&browser_collection_profile=isolated`.
- A LAN worker using `192.168.66.107:3000/?browser_collection=1` remains supported.
- Do not terminate Chrome processes or change, restart, or reload the live queue/services as part of this implementation.
- Initial sign-in and any platform verification in the dedicated profile remain user-handled.
