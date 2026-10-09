# Coach notes, morning brief and the phone page

Three optional background jobs. On macOS each is a per-user `launchd` agent in
`~/Library/LaunchAgents/com.fitdash.*.plist`, installed and removed with one command, with logs in
`~/.fitbit-mcp/*.log`. On Linux, run the same commands from cron or a systemd timer.

## Coach notes

The Today box shows a **COACH** note under the verdict, written in the style of a WHOOP coach:

![Today box with a coach note](demo/today.svg)

```sh
fitdash coach --install                         # check every 30 min for a due note
fitdash coach run                               # write one now if due
fitdash coach run --kind evening --force        # write one now regardless
fitdash coach run --kind morning --dry-run      # print what would be sent and the reply; save nothing
fitdash coach set note "Deload week."           # your own note
fitdash coach show
```

**When a note is due**

| Note | When |
| --- | --- |
| morning | 05:00–12:00, once last night's sleep has synced (or from 10:30), once a day |
| activity | a workout of 10+ min that ended in the last 4 h, unless a later note already covered it |
| evening | from 21:00, or 90 min before tonight's asleep-by time if that's earlier, but not before 20:00. Until 04:00 it still counts as the previous day. |

**How it's written.** `coach.py` builds a summary of the numbers: Recovery and its drivers,
last night's sleep, strain vs target, today's and this week's workouts, the last 7 days of every
key metric, training load, lifting volume and progress, body weight and nutrition, the journal,
your personal recovery patterns, notes already written today, and `coaching/profile.md`. It sends
that to the `claude` CLI in print mode **with no tools, no MCP servers and no saved session**:

```sh
claude -p --tools "" --strict-mcp-config --no-session-persistence --system-prompt "<coach rules>"
```

Claude only returns text: at most ~75 words, leading with what matters, with 1–2 concrete actions.
It won't suggest running or jumping during an impact spike, and points you to a clinician for
anything worrying. The note goes to `coaching/notes.json`. If the CLI is missing or fails, the box
shows a rule-based note marked "auto".

The summary goes to Anthropic each time, like anything you ask Claude. Leave out sections in
`coach.context()` if you'd rather not share them.

## Morning brief

```sh
fitdash brief --install          # daily, 30 min after wake_anchor (or "brief_time" in stats.json)
fitdash brief --install 07:45
fitdash brief                    # now: sync, write the morning coach note if due, notify
fitdash brief --print            # print instead of notifying
fitdash brief --status | --uninstall
```

The notification shows *Recovery · Sleep score · hours asleep* as the title. The body is the
morning coach note when there is one; otherwise it's the strain target, the next split day and
its freshness, tonight's asleep-by time, and muscles under 10 sets. A Mac that was asleep at the
scheduled time runs the brief when it wakes. macOS may ask you to allow notifications from Script
Editor the first time.

## Phone page

```sh
fitdash web --install            # refresh every 30 min + serve on 127.0.0.1:8787 (this Mac only)
fitdash --html                   # write ~/.fitbit-mcp/web/index.html now
fitdash web --status | --uninstall
```

<img src="demo/phone.png" width="320" alt="fitdash on a phone" align="right">

The page is one self-contained HTML file with the dashboard at phone width: dark theme, inline
colors, no scripts and no external requests. Every symbol is pinned to one character cell, so
phones without a braille font still line up. The server binds to **127.0.0.1 only**, so nothing on
your Wi-Fi can reach it.

To open it on your phone, use [Tailscale](https://tailscale.com) (free for personal use):

1. Install Tailscale on the computer and the phone, signed in to the same account.
2. `tailscale serve --bg 8787`. The first time, it gives you a link to enable Serve on your tailnet.
3. Open `https://<computer-name>.<tailnet>.ts.net` on the phone and choose *Add to Home Screen*.

`tailscale serve` shares the page with your own devices only. If you want a public page,
`tailscale funnel 8787` publishes it, but then anyone with the link can see your data.

The page is read-only. To log lifts or the journal from your phone, SSH in (Remote Login on macOS,
with the Tailscale name as the host) and run `fitdash` there. A narrow phone terminal works best
with single sections, e.g. `fitdash --section today`.

<br clear="right">

## Removing everything

```sh
fitdash coach --uninstall && fitdash brief --uninstall && fitdash web --uninstall
tailscale serve reset            # if you set it up
```

Your data in `~/.fitbit-mcp` is left alone. Delete that folder to remove it.
