# LLM data collector

Record your own Claude Code and OpenCode usage locally, then send a private export
for router research. This is a passive recorder: it does not select models,
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
Only collector source code belongs on GitHub. Nothing is uploaded automatically.

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
connect. Capture files/directories use owner-only permissions. Disk usage grows
with traffic; no silent truncation/rotation is performed.

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
