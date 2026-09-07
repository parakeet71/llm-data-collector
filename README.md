# LLM data collector

Record your own Claude Code and OpenCode usage locally, with optional automatic
uploads to a private Hugging Face dataset for router research. This is a passive recorder: it does not select models,
retry requests, change prompts, or generate extra model calls.

**Initial release: offline integration-tested on Linux; a real macOS/subscription
smoke test is still required.** No subscription account has been tested by the
maintainer. If a client rejects the local endpoint, stop and report the client
version/error category. Do not switch authentication or billing to work around it.

## Install on macOS

You need Python 3.11 or newer, plus your already-working Claude Code and OpenCode
installations. If needed, install Python with `brew install python`.

Download this repository using GitHub's **Code → Download ZIP**, unzip it, and open
Terminal in that folder. Then:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m router_collector --help
```

When updating an existing installation, download the new code and run
`python -m pip install --upgrade .` again inside its activated virtual environment.
Existing recordings remain usable.

Keep the downloaded folder: the launcher and session helpers are in `scripts/`.
The commands below assume its virtual environment is activated. Substitute the
actual full path to the downloaded folder where shown.

## Record Claude Code

First verify that ordinary `claude` works with your existing subscription.
Then, **from the project you want to work on**, run:

```sh
python /full/path/to/llm-data-collector/scripts/launch.py claude
```

The launcher prints a **run ID**, starts a loopback proxy, and opens Claude Code
with temporary endpoint and hook settings. Keep the run ID for feedback. Your
existing authentication is used; no key is requested, copied, or substituted.
Claude's normal settings still apply. Hook behavior can be constrained by managed
settings; check `/hooks` and the collector's `events/` directory during the smoke
test. Do not use this launcher for `/login`; log in normally before recording.

Claude hooks capture prompts, tool evidence, stop events, session IDs and
subagent events. At normal session/subagent termination, an available transcript
under `~/.claude/projects/` is also copied. A process crash can prevent the final
snapshot; hook events and already-completed traffic records still remain.

Pass ordinary client arguments after `--`:

```sh
python /full/path/to/llm-data-collector/scripts/launch.py claude -- --continue
```

The launcher reserves `--settings` for recording. It does not edit shell startup
files or persistent Claude settings. Stop recording by exiting the launched
client. Run ordinary `claude` for unrecorded work.

## Record OpenCode with the GLM Coding Plan

Authenticate normally with `opencode auth login`, choosing **Z.AI Coding Plan**.
Keep using the official plan's existing credential. Then, from your project:

```sh
python /full/path/to/llm-data-collector/scripts/launch.py opencode
```

Select a **zai-coding-plan** model with `/models`. Only that provider is redirected;
other providers and direct MCP/network calls are not captured. The launcher
merges a temporary `baseURL` override into `OPENCODE_CONFIG_CONTENT` without
changing saved configuration. Provider-level options in particular client
versions may behave differently: verify that `status` shows captured requests.

**After finishing, export the OpenCode session too.** This preserves final local
tool results and session structure that might never be sent in another request:

```sh
opencode session list
umask 077
opencode export SESSION_ID > "$HOME/datasets/router-collector/opencode-session.json"
python /full/path/to/llm-data-collector/scripts/session_capture.py import-session \
  "$HOME/datasets/router-collector/opencode-session.json" \
  --client opencode --session-id SESSION_ID --run-id RUN_ID
```

Replace `SESSION_ID` and `RUN_ID` with the real identifiers. Keep one export per
session, or import before overwriting the temporary export. The importer does
not read OpenCode's credential database or copy its whole application directory.
Session exports can include earlier history if you resumed an existing session;
filter by timestamps later. A run ID links a launch, not necessarily one task.

## First-run check

1. Start with a small task you actually need; it uses your normal allowance.
2. Confirm the selected provider/account is still the intended one.
3. Check that streamed output and tool execution behave normally.
4. Exit, then run `python -m router_collector status`.
5. Confirm at least one completed record and the expected session evidence exist.

If requests fail, exit and use the client normally. There is no automatic fallback
to paid API billing, a different model, or a different provider. The collector
cannot recover hidden quota information that the provider never sends.

## Record an outcome

A completed API response, successful tool execution, and client exit are **not**
labels that the task succeeded. Add explicit feedback after a task:

```sh
python -m router_collector outcome --run-id RUN_ID --task-id TASK_ID \
  --result accepted --note 'Tests passed and I accepted the change'
```

Use a Claude prompt ID from hook events, an OpenCode message/task identifier, or a
clear unique identifier of your own. Other results: `rejected`, `abandoned`,
`unknown`. Record escalation, fresh versus continued work, verification command,
and remaining issues in the note where relevant. These are human reports, not
independently verified labels. Leave unknown outcomes unknown.

## Send the recordings privately

Everything defaults to `~/datasets/router-collector/`, outside this repository.
For a custom directory, pass `--data-dir /absolute/path` to the launcher and put
that option **before** the subcommand for `python -m router_collector`.

Exit recorded sessions and finish importing their exports, then:

```sh
python -m router_collector status
python -m router_collector export "$HOME/Desktop/router-recordings.zip"
```

The archive includes completed traffic records, events, imported transcripts,
feedback, and a SHA-256 manifest. In-flight/crash-leftover `active/` directories
are excluded; keep them locally for diagnosis. Existing export destinations are
never overwritten. This is a full snapshot, so later exports can repeat records;
deduplicate by record UUID/checksum on the receiving machine.

**The ZIP is not encrypted.** Transfer it directly using your agreed private
channel. Never attach recordings to GitHub issues or push them to this repository.
Only collector source code belongs on GitHub. Automatic uploads are off unless
you enable the private Hugging Face integration below.

## Automatic private Hugging Face uploads

This replaces manual transfers with incremental uploads. Enable it only for
recordings you have agreed to share with the dataset owner. Uploads include
**existing completed recordings** in the selected data directory, not only new
sessions. Use a separate `--data-dir` if you want to start fresh.

One-time setup on the recording Mac, inside the downloaded collector folder:

```sh
source .venv/bin/activate
python -m pip install --upgrade '.[sync]'
hf auth login
python -m router_collector sync-setup --repo OWNER/PRIVATE_DATASET
python -m router_collector sync
```

Replace `OWNER/PRIVATE_DATASET` with the agreed dataset ID. The dataset must
already exist and be **private**. Use a dedicated fine-grained token with write
access to that dataset only; enter it at the local `hf auth login` prompt.
No token goes in the collector configuration, GitHub, or chat. Login credentials
remain managed by Hugging Face's local login store (or `HF_TOKEN`). The uploader
refuses a public dataset and does not create repositories or change visibility.

After setup, launch Claude Code or OpenCode using the normal collector launcher.
A separate uploader checks for new recordings every five minutes, draining any
backlog in batches. Connection failures retry while the launcher is running;
model requests and local recording continue independently. On exit, it attempts
one final batch for up to ten seconds; leftovers retry on the next launch.
The configuration persists for that data directory.

To keep uploading after the client exits (including manually imported OpenCode
sessions and later outcome notes), leave this running in another terminal:

```sh
python -m router_collector sync --watch
```

It runs only while that process is alive; this does not install a macOS startup
service. To upload one batch immediately, run `python -m router_collector sync`.
The result reports uploaded files/bytes and files still pending. A per-directory
lock prevents simultaneous workers from uploading the same batch.

Pause configured automatic uploads with:

```sh
python -m router_collector sync-disable
```

Workers notice before their next batch; a batch already in progress may finish.
Use `sync-setup --repo OWNER/PRIVATE_DATASET` to re-enable, then restart the launcher
or `sync --watch`. For a one-off destination override, `sync --repo OWNER/DATASET`
explicitly uploads one batch even when automatic uploads are disabled.

**Storage behavior:** existing compressed files are streamed directly without
building a ZIP, copying the dataset, or enabling the SDK's Xet cache. A small
SQLite ledger under `.sync/` tracks successful uploads; unchanged files are
skipped and failed batches remain pending. Only finalized, allowlisted collector
files are uploaded. Active captures, temporary files, configuration, credentials,
and upload state are excluded. Batches keep each traffic record together and
normally contain at most 90 files / 64 MiB; one larger record/file is allowed.
Session events and their transcript attachments may arrive in separate batches.

Uploads do **not** delete local recordings or reset the 5 GiB storage limit.
After verifying remote copies, you can move local recordings off the Mac and
restart collection. Keep `.sync/` to retain upload history. Deleting it makes
files eligible for upload again. The ledger assumes confirmed remote files stay
present: it does not repair files manually deleted from the Hub. A crash between
remote success and the local ledger update can cause a harmless retry to the
same remote paths. Repository privacy is checked before each nonempty batch;
keep the dataset private afterwards too.

On the receiving machine, log in with read access and download whenever needed:

```sh
hf download OWNER/PRIVATE_DATASET --repo-type dataset \
  --local-dir "$HOME/datasets/router-usage"
```

This transfers original raw-content recordings, not anonymized training rows.
Review/filter them on the receiving machine. Private means access-controlled by
Hugging Face; the recordings are not additionally end-to-end encrypted.

## What is captured

- Original request/response body bytes, including streaming SSE payloads,
  model/settings, messages, tools, and usage fields where present.
- Request start/end time, response status, byte counts, hashes, partial flags,
  safe content/request-ID headers and reported rate-limit headers.
- Client launch/version/project location and run ID; Claude lifecycle evidence;
  explicit OpenCode session exports and user outcome annotations.

Only model requests routed through the configured endpoint are captured. This
is not a whole-device network sniffer and installs no certificate. Browser login,
telemetry, separate MCP connections, hidden provider reasoning and unreported
subscription usage are not captured. No repository-wide scan or automatic git
snapshot is performed; code/diffs are present when in traffic or session exports.

Authentication headers and cookies are forwarded only in memory and excluded
from metadata; query values and unknown URL paths are not stored. Structured
credential fields are redacted in hook events. **Raw bodies/transcripts are
intentionally unfiltered:** secrets pasted into a prompt or printed by a tool can
remain, as can private code. Review/filter these on the receiving machine before
training or sharing. Do not record projects you do not have permission to share.
The loopback listener is for a trusted single-user machine; local processes may
connect. Capture files/directories use owner-only permissions. New recordings are compressed before being written; no raw staging copy is made.
Existing recordings are not rewritten or deleted automatically.

## SSD and storage protection

Protection is enabled by default:

- Request/response bodies, hook events and imported transcripts are gzip-compressed
  at level 1 **before** disk writes, using a 256 KiB compressed-output buffer.
  There is no per-token flush or raw temporary copy. Streaming to the client
  continues normally; the collector buffers its own disk output only.
- Identical transcript imports reuse a file named by its content hash. Changed
  transcripts are retained in full so later filtering remains possible.
- Collection pauses at approximately **5 GiB** in the data directory, or when
  less than **1 GiB** of free disk space remains. Requests keep passing through;
  the proxy prints a warning and `/health` reports its recording state. A record
  that hits the limit is discarded rather than exported as a complete capture.
- `python -m router_collector status` shows storage usage and configured limits.
  After moving exported recordings off the Mac and freeing space, restart the
  launcher to resume. Existing records are never automatically deleted.

Limits are safeguards, not a filesystem quota: concurrent helpers/collectors and
other applications can change disk usage between checks. Buffered data can be
lost if the process crashes. The cap limits retained data, not lifetime SSD
writes; repeatedly exporting or deleting and recollecting still writes data.

Override limits in the terminal used for both the launcher and session imports
(values are bytes; these examples retain the defaults):

```sh
export ROUTER_COLLECTOR_MAX_BYTES=5368709120
export ROUTER_COLLECTOR_MIN_FREE_BYTES=1073741824
```

New traffic files end in `.bin.gz`, hook events in `.json.gz`, and transcripts
in `.json.gz` or `.jsonl.gz`. Decompress once to recover the original bytes.
Traffic metadata describes each stored body and retains its uncompressed hash.
The export manifest hashes the actual archived files. Exports support both the
old uncompressed format and the new compressed format; gzip files are not
compressed a second time by ZIP. Exporting still writes an additional archive,
and client-owned logs and manual OpenCode exports are outside these protections.

## API usage and manual proxy mode

The same Anthropic proxy supports API-key clients using their existing headers.
The GLM Coding Plan and ordinary GLM API are distinct endpoints:

```sh
python -m router_collector serve --provider anthropic --port 8787 --run-id MY_RUN
# Or choose one:
python -m router_collector serve --provider glm --port 8787 --run-id MY_RUN
python -m router_collector serve --provider glm-api --port 8787 --run-id MY_RUN
```

Use `http://127.0.0.1:8787` as the corresponding client base URL (no extra `/v1`
for GLM). The provider is fixed per proxy process; it cannot forward to arbitrary
hosts. Requests are never retried and redirects are not followed. Streaming
preserves body bytes; HTTP hop-by-hop framing is necessarily regenerated.

Subscription usage and API cost must be analyzed separately. Token usage, cache
usage and quota headers are evidence; this collector does not invent dollar
prices or infer undocumented allowance multipliers.

## Development

```sh
python -m pip install '.[sync]'
python -m unittest discover -s tests -v
```

Tests use local fake upstreams, never real credentials or paid model calls.

Documentation checked September 7, 2026:

- [Claude Code gateway configuration](https://code.claude.com/docs/en/llm-gateway)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [OpenCode provider configuration](https://opencode.ai/docs/providers/)
- [OpenCode CLI and session export](https://opencode.ai/docs/cli/)
- [Official GLM Coding Plan setup](https://docs.z.ai/devpack/tool/opencode)
- [Z.AI endpoint distinction](https://docs.z.ai/devpack/quick-start)
