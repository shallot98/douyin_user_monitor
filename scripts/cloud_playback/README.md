# D2 playback repair on an independent compute host

This package runs historical media repair on the independent host `boh` and
publishes verified metadata on the project host `hdz`. Historical video bytes
must not be downloaded to, transcoded on, or uploaded through the project host.
The cloud worker uses Python 3.10+, the standard library, FFmpeg and FFprobe;
the project-side publisher uses the existing application environment.

Original D2 files are retained. Playback MP4s and covers use `_web_playback/`;
verified replacements for damaged originals use `_recovered_originals/`.
The existing `/kuake` mount continues to expose original bytes.

## Processing and publication

1. `build_queue.py` builds jobs from the fixed inventory and current records.
   It excludes disappeared records and avoids treating all H.264 files as
   browser-verified. Queue generation runs on the project host using metadata.
2. `run_staged.py` runs the pilot, optional original recovery, then the ordinary
   queue. The pilot also belongs to the ordinary queue and its receipt prevents
   duplicate processing. A failed pilot stops the pipeline; individual recovery
   failures are reported while the ordinary queue can continue.
3. `media_worker.py` reads native D2 URLs on boh, verifies source identity,
   prepares H.264/AAC/faststart video or a cover, and fully decodes the output.
   Suitable H.264 sources can be remuxed without video re-encoding.
4. `source_recovery.py` checks retained upstream bytes against their audited
   SHA-256 before uploading an independently named original. It verifies the
   D2 bytes, media metadata and complete decode before producing derivatives.
5. `d2_upload.py` sends completed files directly from boh to D2. Uploaded media
   must pass remote metadata checks and complete decode before a success receipt
   is written. Existing derivative targets are rejected rather than overwritten
   or silently adopted.
6. `sync_results.py` reads metadata receipts through ssh-skill, checks current
   D2 objects, refreshes the corresponding OpenList directories from parent to
   child, then merges references with `MonitorStorage.update_state` under the
   state lock. Shared directories are refreshed once per fetched receipt batch.
   It preserves download history and existing file indices.
   A website `Range: bytes=0-0` request must return HTTP 206 and the expected
   total size before the publication checkpoint advances.

The state merge precedes the website check. If the website check fails, verified
D2 references may already be present, but the checkpoint stays unchanged and
the error is recorded; the next poll retries idempotently. Cloud verification
and a one-byte website request do not replace real browser frame, audio, end
and seek acceptance.

Ordinary playback files and covers use a `TemporaryDirectory` on boh. They are
removed on successful completion or exception unwinding; a job waiting for
upload quota can retain its temporary files. Audited recovery candidates in
`special-sources/` and the earlier retained pilot copy have no automatic deletion
step. Local temporary cleanup does not remove cloud objects.

## Staged worker

The deployed root on boh is `/var/tmp/douyin-playback-repair-20260913`.
From that directory, the configured command is:

```bash
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
/opt/mamba/bin/python3 -m scripts.cloud_playback.run_staged \
  --credentials /var/tmp/douyin-playback-repair-20260913/d2.json \
  --pilot-queue /var/tmp/douyin-playback-repair-20260913/pilot-queue.json \
  --recovery-queue /var/tmp/douyin-playback-repair-20260913/source-recovery-queue.json \
  --recovery-source-root /var/tmp/douyin-playback-repair-20260913/special-sources \
  --queue /var/tmp/douyin-playback-repair-20260913/full-queue.json \
  --workspace /var/tmp/douyin-playback-repair-20260913/batch-run
```

Provision a worker package with empty `scripts/__init__.py`,
`douyin_user_monitor/__init__.py` and `douyin_user_monitor/monitor/__init__.py`,
plus `monitor/playback_media.py`, so importing the worker does not initialize
the application or crawler. Include the cloud modules and queues; credentials
and retained recovery media are provisioned separately.

The worker uses a file lock, individual receipts and an append-only result log.
`pipeline.json` reports the stage; `progress.json` counts only the current stage,
so the pilot's `total=1` is not the total deployed queue size. Restarting skips
recorded results. Failed jobs require explicit review and `run_queue`'s
`--retry-failed`; changed fingerprints and existing derivative objects remain
explicit errors.

Before processing a new job, `quota.py` queries actual D2 upload capacity and
waits 600 seconds between checks when exhausted. It also checks each file's
required byte count immediately before upload. It does not guess the quota
reset timezone or treat elapsed time as new capacity.

The worker runs under a separate Supervisor instance. Use ssh-skill for remote
operations, and always give its configuration when controlling this program:

```bash
/usr/bin/supervisorctl \
  -c /var/tmp/douyin-playback-repair-20260913/supervisor/repair.conf \
  status douyin-playback-repair
```

Do not restart the container or unrelated Supervisor processes. The local
`douyin-playback-repair-sync.timer` polls every 60 seconds using
`/etc/douyin-playback-repair/sync.json`; only metadata is transferred to hdz.
The deployed Supervisor program uses the system CA file shown above because
the Mamba CA bundle did not validate D2's upload host; TLS verification stays on.

## Standalone uploader

The `d2_upload` entrypoint only uploads a completed file; it does not run FFmpeg,
change website state, refresh tokens, or delete files. Only this entrypoint and
its signing/upload helpers require no media tools or application dependencies.

From the parent project directory on the compute host, run:

```bash
python3 -m scripts.cloud_playback.d2_upload \
  --credentials /private/d2.json \
  --file /worker/output.h264.mp4 \
  --path '/vps/抖音视频/_web_playback/USER/WORK.h264.mp4'
```

Use an independently named playback file or cover; do not select the original
archive path as the destination. Paths are native D2 paths without the OpenList
`/d2` mount prefix. Meaningful spaces and Unicode characters are preserved.

Provision the credentials JSON separately with private file permissions, such as
mode `0600`. Required string fields are `host`, `client_id`, and `client_secret`.
`access_token` is optional and defaults to an empty string, matching the verified
personal API authentication. `host` contains a hostname only, such as
`openapi.2dland.cn`, not an `https://` URL. The utility never reads or writes the
production mount database. It ignores refresh tokens and never refreshes them.

The injectable Python interface is:

```python
from scripts.cloud_playback.d2_client import Credentials, D2Client, HttpTransport
from scripts.cloud_playback.d2_upload import upload_file

client = D2Client(Credentials.from_mapping(private_config), transport=HttpTransport())
result = upload_file(client, completed_local_path, destination_native_path)
```

`client.post(endpoint, payload)` also supports explicit native metadata requests.
For a read-only lookup, use `/v6/userfile/get` with `{"path": native_path}`.
For a direct source URL, use `/v6/userfile/get_direct_download_address` with the
same payload; its response is private and must not be logged.

## Protocol and error handling

- `create_upload_task` receives `path` and decimal-string `size`.
- Blocks follow the server's `block_size`, CID codec and multihash settings.
  CID version is 1. Omitted or zero codec/hash fields use the documented defaults
  `0x55` and `0x12`. Supported hashes are SHA-1, SHA-256 and SHA-512; other values
  fail explicitly instead of substituting a different hash.
- Each CID is checked with GET; only a JSON boolean `true` skips its upload.
  Missing blocks use POST, followed by POST of the ordered complete CID list.
- Task URLs preserve the provider's literal colon separators. Encoding them as
  `%3A` causes D2 to look up a different upload session and return HTTP 404.
- Finalization must acknowledge the exact path, size and a nonempty content
  identity. The uploader then always reads `/v6/userfile/get` to obtain the file
  identity and verifies the same path, size and content identity. D2's upload
  response can omit the file identity. Source changes before finalization cause
  an error.
- A `created=true` response triggers a metadata check and then an explicit error:
  same path and size do not prove that the existing object matches the source.
  The caller must verify that object's contents separately.
- HTTP or protocol errors stop the upload. There are no implicit retries, token
  refreshes, or success substitutions. HTTP error bodies and exception URLs are
  omitted from diagnostics because they can contain credentials or bearer URLs.
- Connections bypass environment proxy settings so full media is sent directly
  from the compute host. Redirects are surfaced rather than forwarding signed
  headers or task bearer URLs to another address.

The tool does not create parent directories. The official OpenList D2 driver uses
`POST /v6/userfile/create` with `{"path": parent_path, "name": child_name}` for
mkdir. The coordinator must explicitly create and verify each missing directory;
automatic recursive parent creation by the upload endpoint has not been verified.

On success, stdout contains JSON with `status`, `identity`, `path`, `size`,
`content_identity`, `blocks_total`, `blocks_uploaded`, and `blocks_reused`.
On failure, stderr contains sanitized JSON and the process exits nonzero.
**Uploaded metadata is not playback acceptance.** The caller must inspect the
D2 file's codec, duration, frames, audio and seeking before changing website state.

## Deployment evidence

On 2026-09-13, 866 video path mappings and 17 cover mappings were restored.
The deployed queue has 12,410 unique works, including 10 original recoveries;
it also contains startup-speed and cover work, not only unplayable videos.
On 2026-09-14, direct uploading and website publication resumed after fixes to
quota zero-value parsing, the worker CA configuration, upload task URL encoding,
finalization metadata lookup and OpenList directory refresh. The worker is
processing the ordinary queue; it waits for provider-reported quota when needed.

The pilot now passes actual website playback checks from D2, including frames,
audio decoding, full completion, seeking and reopening. Headless-browser dropped
frames remain recorded; multi-device smoothness is unverified. All 10 original
recoveries passed cloud byte and decode checks. An explicit HTTPS offline-import attempt
returned HTTP 400/code 3 from `add`, without a task ID. Its temporary origin was
stopped. `--origin-config` is an explicit alternative and is not used by the
deployed staged worker.

See the [upload recovery evidence](../../docs/plantree/plans/browser-playback-repair/evidence/upload-recovery.md)
and [current handoff](../../docs/plantree/plans/browser-playback-repair/implementation-status.md)
for exact deployment hashes, source checks, process status, tests and remaining
limitations. This recovery's focused checks passed 70 tests and 64 subtests.
The earlier 296-test run is retained as historical evidence; neither count
establishes full-queue browser acceptance.

## Protocol sources and tests

Protocol sources inspected on 2026-09-13:

- [D2 SDK signer](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/signer/signer.go)
- [D2 signature constants](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/signer/constants.go)
- [D2 request and response models](https://github.com/halalcloud/golang-sdk-lite/blob/main/halalcloud/services/userfile/model.go)
- [OpenList D2 block upload](https://github.com/OpenListTeam/OpenList/blob/main/drivers/halalcloud_open/halalcloud_upload.go)
- [OpenList D2 mkdir](https://github.com/OpenListTeam/OpenList/blob/main/drivers/halalcloud_open/driver_curd_impl.go)

The signer was compared against the helper that already passed real, read-only
D2 metadata requests. Focused tests use synthetic files and responses; they do
not submit real upload tasks. Run them from the project root with:

```bash
timeout 60s .venv/bin/python -m pytest -q \
  tests/test_d2_cloud_upload.py tests/test_d2_cloud_signing.py
```
