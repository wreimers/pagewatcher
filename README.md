# pagewatcher

Pagewatcher monitors the meaningful text of an HTML page and sends an Apple Push
Notification service (APNs) alert when that text changes materially. It supports
CSS-based content selection, noise removal, conditional HTTP requests, cumulative
change detection, and durable SQLite state.

The first successful check establishes a baseline and does not send a notification.

## Requirements

- Python 3.12 or newer
- An Apple Developer account
- An APNs-enabled companion app installed on the receiving device
- The app's bundle ID and current APNs device token
- An APNs token-signing key (`.p8`), its key ID, and your Apple team ID

Pagewatcher sends notifications to an existing app/device registration. It does not
create an iOS or macOS app or obtain a device token for you.

## Installation

Create and activate a virtual environment, then install the project and development
dependencies:

```sh
python3.12 -m venv venv
source venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the test suite:

```sh
pytest
```

## APNs setup

1. Enable push notifications for your companion app and register the app with APNs.
2. Send the device token produced by the app to the machine running Pagewatcher.
3. Create an APNs signing key in the Apple Developer portal and download its `.p8`
   file. Apple only allows this file to be downloaded once.
4. Note the key ID, team ID, and app bundle ID.
5. Use the sandbox endpoint for development builds and the production endpoint for
   distributed builds. A device token must match the selected environment.

Keep the signing key private and outside the repository. Restrict its permissions,
for example with `chmod 600 AuthKey_EXAMPLE.p8`.

Apple's documentation covers [registering an app with APNs](https://developer.apple.com/documentation/usernotifications/registering-your-app-with-apns)
and [token-based provider authentication](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns).

## Configuration

Pagewatcher reads configuration from environment variables.

Required variables:

| Variable | Description |
| --- | --- |
| `PAGEWATCHER_URL` | Absolute HTTP or HTTPS URL to monitor |
| `PAGEWATCHER_APNS_TEAM_ID` | Apple Developer team ID |
| `PAGEWATCHER_APNS_KEY_ID` | APNs signing-key ID |
| `PAGEWATCHER_APNS_BUNDLE_ID` | Bundle ID used as the APNs topic |
| `PAGEWATCHER_APNS_DEVICE_TOKEN` | Device token issued to the companion app |
| `PAGEWATCHER_APNS_PRIVATE_KEY_PATH` | Path to the APNs `.p8` signing key |

Optional variables:

| Variable | Default | Description |
| --- | ---: | --- |
| `PAGEWATCHER_DATABASE_PATH` | `pagewatcher.db` | SQLite state-file path |
| `PAGEWATCHER_POLL_INTERVAL_SECONDS` | `300` | Delay between checks in continuous mode |
| `PAGEWATCHER_REQUEST_TIMEOUT_SECONDS` | `20` | HTTP request timeout |
| `PAGEWATCHER_MAX_RESPONSE_BYTES` | `2000000` | Maximum downloaded response size |
| `PAGEWATCHER_INCLUDE_SELECTORS` | empty | Comma-separated CSS selectors to monitor |
| `PAGEWATCHER_IGNORE_SELECTORS` | `script,style,noscript,template` | Comma-separated CSS selectors to remove |
| `PAGEWATCHER_SIMILARITY_THRESHOLD` | `0.98` | Relative materiality threshold from 0 to 1 |
| `PAGEWATCHER_MINIMUM_CHANGED_CHARACTERS` | `20` | Absolute materiality threshold |
| `PAGEWATCHER_APNS_USE_SANDBOX` | `true` | Use APNs sandbox rather than production |

Boolean values accept `true`, `false`, `yes`, `no`, `on`, `off`, `1`, or `0`.
Selector values are split at commas, so use each comma-separated entry as an
independent selector.

Example development configuration:

```sh
export PAGEWATCHER_URL="https://example.com/products/widget"
export PAGEWATCHER_INCLUDE_SELECTORS="main,#availability"
export PAGEWATCHER_IGNORE_SELECTORS=".timestamp,.advertisement"

export PAGEWATCHER_APNS_TEAM_ID="YOUR_TEAM_ID"
export PAGEWATCHER_APNS_KEY_ID="YOUR_KEY_ID"
export PAGEWATCHER_APNS_BUNDLE_ID="com.example.PagewatcherReceiver"
export PAGEWATCHER_APNS_DEVICE_TOKEN="YOUR_DEVICE_TOKEN"
export PAGEWATCHER_APNS_PRIVATE_KEY_PATH="/secure/path/AuthKey_EXAMPLE.p8"
export PAGEWATCHER_APNS_USE_SANDBOX="true"
```

Validate the configuration without fetching the page or sending a notification:

```sh
pagewatcher validate
```

## Usage

Perform one check:

```sh
pagewatcher check
```

Run continuously using the configured polling interval:

```sh
pagewatcher watch
```

Test APNs without fetching the monitored page:

```sh
pagewatcher test-notification
```

The same commands work through the module entry point:

```sh
python -m pagewatcher check
```

Exit status `0` indicates success, `1` indicates an operational error, `2` indicates
invalid configuration, and `130` indicates an interrupted continuous watcher.

## What counts as a material change

Pagewatcher removes comments and ignored elements, limits the page to any inclusion
selectors, extracts text, decodes HTML entities, and normalizes whitespace. Changes
to markup that do not alter the resulting text are ignored.

For changed text, Pagewatcher calculates both a similarity score and an absolute
changed-character count. A change is material if either:

- similarity is lower than `PAGEWATCHER_SIMILARITY_THRESHOLD`; or
- changed characters reach `PAGEWATCHER_MINIMUM_CHANGED_CHARACTERS`.

An immaterial observation updates HTTP validators but does not replace the comparison
baseline. Small changes therefore accumulate until their combined difference becomes
material. A successful notification advances the baseline. If APNs fails, the old
baseline and validators remain in place so the same change can be fetched and retried.

The notification includes the monitored URL and up to 240 characters of normalized
page text. The companion app is responsible for handling the custom top-level `url`
field if tapping the notification should open the page.

## Choosing selectors

Selectors are the best defense against timestamps, rotating promotions, navigation,
and other irrelevant changes. For example:

```sh
export PAGEWATCHER_INCLUDE_SELECTORS="main.product"
export PAGEWATCHER_IGNORE_SELECTORS=".last-updated,.recommendations"
```

If an inclusion selector matches nothing, the check fails instead of treating the
missing section as empty. This avoids replacing a useful baseline after a selector
becomes invalid, but it also means selector disappearance is reported as an error
rather than a content-change notification.

## Persistence and failure behavior

State is stored by URL in SQLite. It includes the comparison snapshot and hash, ETag,
Last-Modified value, last-check time, and last successfully notified hash. Database
directories are created automatically.

`pagewatcher watch` retries network timeouts, retryable HTTP responses, APNs rate
limits, expired provider tokens, and APNs server failures after the normal polling
interval. Configuration errors, invalid selectors, oversized pages, invalid APNs
requests, and other permanent failures stop the process with a nonzero status.

APNs collapse identifiers are derived from the snapshot hash. Retries for the same
snapshot therefore use the same identifier, reducing duplicate visible alerts if a
request was accepted but its response was lost.

## Running unattended

For a long-running process, configure your service manager to run:

```sh
/absolute/path/to/pagewatcher/venv/bin/pagewatcher watch
```

Provide the required environment variables through the service manager's protected
environment or secrets facility, set the working directory explicitly, and arrange
for automatic restart after unexpected process failures.

Alternatively, schedule `pagewatcher check` with cron, launchd, or a systemd timer.
SQLite persistence makes independent invocations safe, provided only one invocation
uses a given database at a time.

## Scope and limitations

- Pagewatcher downloads server-returned HTML; it does not execute JavaScript. For a
  client-rendered page, monitor a stable server endpoint or API instead.
- One process is configured for one URL and one device token. Separate processes and
  database files can monitor additional URLs or devices.
- APNs acceptance does not guarantee delivery. Device connectivity, notification
  permissions, focus settings, and APNs delivery policy still apply.
- Treat monitored page content as sensitive if it may appear on the receiving
  device's lock screen.
