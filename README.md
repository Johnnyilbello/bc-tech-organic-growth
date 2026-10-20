# Organic growth runner

Generic Meta Graph API operations with an authenticated encrypted state. The source is published. The daily production runner is not activated; its first live read-only test is still pending. A separate verification workflow tests synthetic data without Meta secrets.

Public source includes the engine, a planner without its private topic catalog, tests, the workflow and this document. Account configuration, calendar, captions, metrics, reports and operation receipts are restored only from AES-256-GCM encrypted state. No token, private catalog or operational database belongs in source control.

Meta Page credentials and the 256-bit state key belong exclusively in GitHub Actions Secrets. The user token and Windows vault are never uploaded. Every Meta mutation requires an acknowledged remote intent; compare-and-swap, leases and receipts preserve uncertain results without blind retries.

The default mode is read-only. Publishing requires a separate activation flag and a verified transition to one authoritative publisher. Missing queue evidence, expired coverage, changed assets, wrong identity or unknown results stop publishing.

Standard public GitHub-hosted Ubuntu runners are free according to GitHub documentation. No larger runners, artifact storage or cache is configured. Jobs time out after five minutes. Scheduled jobs can be delayed or dropped; missed publication windows must be reviewed rather than retried blindly.

Full reports remain encrypted; public logs and job summaries contain only sanitized technical status. Notification delivery and a live cloud run still require verification. This repository does not claim an operational acquisition system until integrations and results are checked.

Source: https://docs.github.com/en/billing/concepts/product-billing/github-actions
