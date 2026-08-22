# Runbook: ALMANYA24 private YouTube test environment

This runbook covers a **separate Google Cloud project** and a **separate private
ALMANYA24 YouTube channel** for btcedu test publishing.

It is operator guidance only:

- do not create or expose secrets here
- do not commit credential JSON files
- do not use production OAuth credentials for this environment
- do not ask btcedu to create external accounts

## 1) Ownership model

- **Google Cloud project:** dedicated test project, owned by the operator.
- **Channel:** Brand Account-owned ALMANYA24 test channel, separate from production.
- **Owners:** one primary owner plus an optional backup owner/test user.
- **Audience:** only the exact test accounts needed for the run.

Use a Brand Account for the channel. That keeps channel ownership separate from a
personal Google account and makes promotion/transfer much cleaner.

## 2) Google Cloud setup

1. Create a **separate Google Cloud project** for the test channel.
2. Enable **YouTube Data API v3** in that project.
3. Configure the OAuth consent screen:
   - **User type:** External unless the project truly lives inside one Google Workspace.
   - **Publishing status:** Testing while the test channel is in use.
   - **Test users:** add only the operator and any explicitly approved testers.
   - **App details:** fill in app name, support email, and contact email.
4. Add the scopes btcedu currently needs:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube`
   - `https://www.googleapis.com/auth/youtube.force-ssl`

Keep `youtube.force-ssl` from the start; caption upload depends on it. Changing
the scope set later invalidates existing refresh tokens and requires reauth.

> Google’s testing mode expires test-user authorizations after 7 days, including
> the refresh token. Plan to rotate or reauth accordingly.

## 3) Brand Account and channel identity

1. Create or move the test channel under a **Brand Account**.
2. Keep the test channel name/handle distinct from production.
3. Make the operator a **primary owner** and keep the ownership list small.
4. Complete YouTube channel verification / phone verification if custom
   thumbnails or longer uploads are needed.

`btcedu youtube-auth` prints the authenticated channel ID. Store that public ID
in the matching target configuration. Every real upload then calls
`channels.list(mine=true)` and fails before `videos.insert` if the OAuth token
belongs to another channel. Do not print the credential JSON itself.

## 4) Separate paths and systemd secret handling

Use dedicated paths for the test project:

```env
YOUTUBE_DEFAULT_TARGET=test
YOUTUBE_TEST_CLIENT_SECRETS_PATH=data/youtube/test/client_secret.json
YOUTUBE_TEST_CREDENTIALS_PATH=data/youtube/test/credentials.json
YOUTUBE_TEST_CHANNEL_ID=UC...
YOUTUBE_TEST_DEFAULT_PRIVACY=private
YOUTUBE_PRODUCTION_CLIENT_SECRETS_PATH=data/youtube/production/client_secret.json
YOUTUBE_PRODUCTION_CREDENTIALS_PATH=data/youtube/production/credentials.json
YOUTUBE_PRODUCTION_CHANNEL_ID=UC...
YOUTUBE_PRODUCTION_DEFAULT_PRIVACY=unlisted
YOUTUBE_CATEGORY_ID=27
YOUTUBE_DEFAULT_LANGUAGE=tr
```

Recommended handling:

- keep both JSON files outside version control
- set file mode to `0600`; btcedu enforces this when reading, refreshing or
  creating either OAuth file and rejects unsafe/non-regular paths
- keep path overrides in a dedicated env file such as `.env.youtube-test`
- never put credential contents into systemd unit files
- only reference file paths from systemd `EnvironmentFile=`

If the test env should be used by a timer-driven publish run, add the test env
file via a systemd drop-in for `btcedu-run.service` (and `btcedu-web.service`
if the dashboard should read the same settings).

## 5) Authenticate

1. Install the YouTube extras if needed:

   ```bash
   pip install -e ".[youtube]"
   ```

2. If you are working over SSH, forward the local callback port used by the
   auth flow:

   ```bash
   ssh -L 8085:localhost:8085 pi@HOST
   ```

3. Run the auth flow:

   ```bash
   btcedu youtube-auth --target test
   ```

4. Check the token state:

   ```bash
   btcedu youtube-status --target test
   ```

Expected operator checks:

- credentials file exists at the test path
- `valid` is true
- `can_refresh` is true
- the authenticated channel is the ALMANYA24 test channel

If the consent screen is still in Testing, only listed test users can authorize.

## 6) Default private upload

The test environment should publish as **private** by default.

Recommended checks:

- the test target sets `YOUTUBE_TEST_DEFAULT_PRIVACY=private`
- ad hoc uploads can still override privacy explicitly:

  ```bash
  btcedu publish-review --episode-id EPISODE_ID --target test
  btcedu review approve REVIEW_ID
  btcedu publish --episode-id EPISODE_ID --target test
  ```

Do not switch the test channel to unlisted/public unless you are explicitly
testing that behavior.

## 7) Post-upload validation

Check these after every test upload:

| Check | What to confirm | Notes |
| --- | --- | --- |
| Playlist | Video is in the intended test playlist, if you use one | btcedu does not auto-manage playlists yet |
| Thumbnail | Custom thumbnail is visible in Studio / watch page | upload failures are logged as non-critical |
| Captions | Caption track exists and is selectable | requires `youtube.force-ssl` |
| Chapters | Timestamp block appears in the description | YouTube silently drops marks unless the first is `0:00`, there are at least 3 marks, and every section is at least 10s |

Chapter marks are built from the rendered timeline, not from narration length.
If marks disappear, inspect `render/render_manifest.json` and `chapters.json`.

## 8) Official quota verification procedure

Use the current Google Cloud Console procedure:

1. Open the **Google Cloud Console** and select the test project.
2. Go to either:
   - **APIs & Services → Dashboard → YouTube Data API v3 → Quotas**
   - **IAM & Admin → Quotas & System Limits** and filter **Service = YouTube Data API v3**
3. Inspect:
   - current usage
   - current usage percentage
   - quota charts / usage over time
4. If you need more quota, submit the **YouTube API Services – Audit and Quota
   Extension** form. There is no self-serve paid quota bucket.

Current official baseline, verified 2026-08-22:

- `videos.insert`: 1 call in its dedicated default 100-calls/day bucket
- `channels.list`: 1 general quota unit
- `thumbnails.set`: 50 general quota units
- `captions.insert`: 400 general quota units

The publisher records the expected and attempted breakdown in the PublishJob
metadata snapshot and publish provenance. It does not claim to read live quota
usage from YouTube.

If quota is tight, keep retries low and avoid repeat uploads until the quota
page shows headroom again.

## 9) Troubleshooting

### `quotaExceeded`

Meaning: daily quota exhausted.

Action:

- check the quota page for the test project
- wait for the Pacific-time quota reset, or request quota extension
- do **not** re-authenticate; this is a quota problem, not a token problem

### Publish job remains `uploading`

Meaning: the process stopped after the remote upload began, so YouTube may
already contain the video even though no video ID was committed locally.

Action:

- inspect the selected test/production channel before doing anything else
- reconcile the existing attempt only after its remote outcome is known
- do not use `--force`; btcedu deliberately blocks automatic re-upload while
  the target has an indeterminate `uploading` PublishJob

### `invalid_grant`

Meaning: the refresh token is dead.

Common causes:

- testing-mode authorization expired after 7 days
- the user revoked consent
- the token was minted for a different client/project
- scopes changed after the token was created
- the credentials file belongs to another environment

Action:

- re-run `btcedu youtube-auth --target test` with the correct project/channel
- if scopes changed, discard the old credentials file and mint a new one
- verify the channel identity again before publishing

### `can_refresh=false`

Meaning: the credentials file has no usable refresh token.

Action: re-authenticate.

### Caption upload failed

Most likely causes:

- missing `youtube.force-ssl`
- stale token created before the scope was added

Action: rotate the token with the full scope set.

### Thumbnail upload failed

Most likely causes:

- channel verification not completed
- image/path issue

Action: verify the channel and retry the publish.

## 10) Token rotation

Rotate tokens when:

- the scope list changes
- the client project changes
- Google revokes or expires the refresh token
- you move between test and production environments

Rotation flow:

1. Prepare the new client secret file path.
2. Run `btcedu youtube-auth --target test` against the intended project/channel.
3. Replace the credentials file at the test path.
4. Run `btcedu youtube-status --target test`.
5. Perform one private canary upload.

Do not hand-edit the credential JSON.

## 11) Promotion to production

Promotion means switching from the test project/channel to the production
project/channel and publishing the OAuth app only when ready.

Before promotion:

- production Brand Account/channel exists and is separate
- production Google Cloud project exists and has YouTube Data API v3 enabled
- production OAuth consent screen is ready for **In production**
- any required verification work is complete
- production client/credential paths are separate from the test paths

Promotion steps:

1. Keep the ALMANYA24 test channel private and intact.
2. Create or activate the production project and consent screen.
3. Publish the app only after the required verification is complete.
4. Configure the production target's separate paths and public channel ID.
5. Run `btcedu youtube-auth --target production`.
6. Create a production-bound publish review and approve it:

   ```bash
   btcedu publish-review --episode-id EPISODE_ID --target production --privacy private
   btcedu review approve REVIEW_ID
   ```

7. Run one private canary with
   `btcedu publish --episode-id EPISODE_ID --target production --privacy private`
   before changing any public-facing defaults.

Successful uploads are idempotent per target. A test upload remains separate
and does not mark the episode published; a committed production PublishJob is
reconciled after restart instead of uploading a duplicate.

## 12) Blocking actions

If the repo-side documentation and path separation are in place but the external
Google/YouTube work is still pending, the remaining operator actions are:

- create the separate Google Cloud project
- create/confirm the private ALMANYA24 Brand Account/channel ownership
- add the required OAuth test users and consent-screen state
- perform the first private upload to verify the live path

Until those are complete, keep the todo blocked.

## References

- YouTube API quota and compliance: https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits
- OAuth app verification: https://support.google.com/cloud/answer/13463073?hl=en
- OAuth app audience / testing / publishing status: https://support.google.com/cloud/answer/15549945?hl=en
- Google Cloud quota viewing and management: https://docs.cloud.google.com/docs/quotas/view-manage
- Current method costs: https://developers.google.com/youtube/v3/determine_quota_cost
