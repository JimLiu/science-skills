---
name: remote-compute-ssh
description: Submit→wait_for_notification→collect-outputs workflow for the user's SSH/SLURM hosts. Load once you've decided to dispatch remote.
license: Apache-2.0
---

You've decided to run this on the user's SSH host. This skill covers the
orchestration layer — partitions, env activation, job scripts, file transfer,
recovery — not the science; what to run and why comes from the task and its
own skills. Each `c.submit_job()` puts an approval modal in front of the user
and, once approved, spends their allocation; a string of failed submits costs
their attention, their compute, and their trust. So the shape of a good run
is: read what's already known about this host, ask once for what isn't, land
the first submit, and write down what you learned about the host or compute
provider so the next session goes straight to the job.

## Workflow

Every `host.compute.*` call in this skill runs via the **`repl` tool**
(the control-plane kernel), not the `python` tool. Job submission opens the
user's approval modal and the SSH connection from the orchestrator's own
process; that has to happen outside the sandboxed data workspace, so
`host.compute` simply isn't attached in the `python` tool (the error there
redirects you: "host.compute is only available via the `repl` tool"). The
two kernels share your workspace directory but not memory, so the rhythm is:
prepare inputs in a `python` cell (write `./in.dat`, pickle what the job
needs), run `create → submit_job` in a `repl` cell and let the cell return
— the kernel never waits on the job. Then call the `wait_for_notification`
brain-tool to park until the daemon's poller posts the `compute_done`
notification, and return to the `python` tool to read the harvested
`hpc/<jobId>/` files. The `repl` tool is stdlib-only (`python -I -S`) —
keep pandas/numpy work in the `python` tool and pass data through files.

Start with the `compute_details({provider, mode:'read'})` tool, then bind
once: `c = host.compute.create(provider)`. The name is validated
immediately — a typo raises `host.compute.NotFound` listing the targets that
do exist, rather than surfacing three cells later. The doc's shape tells you
how much discovery is left: `### env:` blocks and gotchas mean prior
sessions did the legwork — trust it. A bare `## Resources` header means
first contact — spend one batched `c.call_command('id; module avail 2>&1 |
head -40; ls -la ~', intent=..., login_shell=True)` and one
`ask_about_compute` now, before any submit. The header's `scheduler:` line
is detection, not ground truth; `none` on a thin login node means a heavy
direct-exec job would crowd other users, so when the resources look thin and
the details doc has no prior note, ask first.

`c.call_command(cmd, intent=...)` returns a `CommandResult` — `r.stdout`,
`r.stderr`, `r.exit_code`, `r.ok` — and it is also a dict, so
`r["stdout"]` from an older transcript still works. A non-zero exit is a
value (`r.ok is False`), not an exception, until you opt in with
`r.raise_for_status()`, which raises `host.compute.CommandFailed` carrying
the stderr tail. The command is a shell string — pipes, `module load`,
redirects all work — so `shlex.quote()` anything you interpolate.

If the prose doc has a known-working activation, write it directly into your
`command` (e.g. `source <path>/activate && <tool> ...`). If it doesn't, find
one via `c.call_command()` (`module avail X`, `conda env list`, likely app
dirs) or ask. Install only once you've established the tool genuinely isn't
there — user-space (venv/conda under scratch), via `c.call_command()` for a
quick install or as its own `c.submit_job()` if it needs a build node.
Whichever route produced an activation, run the entrypoint once via
`c.call_command()` before building the real job on it.

Job workdirs live under the provider's `scratch_root`. You never set it
yourself: the connection probe detects one ($SCRATCH, ~/scratch, …) and on a
bare host creates `~/.claude-science-scratch` as the fallback — its current
value is the `scratch_root:` line in `compute_details`. If a submit errors
with "no scratch_root configured yet", the post-connect probe is usually
still finishing (common right after answering a password card): wait a few
seconds and retry, then check `compute_details`. There is no probe method on
the compute object — probing happens on connect.

Then `job = c.submit_job(...)` (see below). `inputs=['file']` (or
`{'src': 'file', 'dst': ...}`) stages the file for you — there's no separate
upload step to script, and once submitted there's nothing to verify with
`c.call_command('cat ...')`; the job reads `./<dst>` from its own
workdir. End the cell — `submit_job` returns as soon as the job is
dispatched (the card and input staging are the only things it waits for), and
the daemon's background poller polls the remote, transfers back the files you
named in `outputs` (everything, when you omit it) into your workspace under
`hpc/<jobId>/`, and posts a `compute_done` notification when they land.

Park on the `wait_for_notification` brain-tool until that notification
arrives. Its payload carries `{job_id, state, status, exit_code, notes,
output_files, output_file_count, ...}` — `state` is one of
`succeeded | failed | cancelled` on an ssh target (`timed_out` completes the
shared vocabulary but only container targets emit it; the same string
`job.state()` returns), and `output_files` is what transferred back — the
deliverables. Publish what you want with
`save_artifacts(payload['output_files'])` — that step is what gives them
provenance and surfaces them in the artifact panel. If you
need the full record (all `files`, `left_on_remote`, `stdout_tail`,
`notes`), re-enter a `repl` cell and call
`res = c.attach_job(job_id).result()` — a read of what the poller already
brought back, never a wait. Waiting is the daemon's job, not the kernel's:
called on a job that isn't terminal yet, `.result()` raises
`host.compute.JobPending` (whose `next_step` is the park protocol) rather
than blocking, because a `repl` cell that hangs on a remote job would tie
up your control-plane kernel for the job's whole runtime.
`open(res.files[i])` reads any transferred file directly. A file you didn't
name in `outputs` is still in the job's workdir on the host — chain it into
the next job via `inputs=[{'src': f"ssh://<target>{res['remote_workdir']}/<path>"}]`
(that pair is how you leave a file on the cluster; nothing reports it back,
because you already know where you wrote it). Between the notification and
`close()` you can still `c.download(f"{job.workdir}/<file>")` for a
one-off you decide you want locally after all.
`c.download('/any/absolute/host/path')` works for **any readable file on
the host**, not just job outputs — paths outside scratch/data_roots raise an
approval card the user clicks Allow on. When the user asks you to
fetch a host file, call `c.download()` with the path they gave; the approval
card is the authorization gate, so don't refuse on their behalf and don't
`cp` into scratch first to dodge it. Dotfiles / paths under a dot-directory
(`~/.ssh/*`, `.gitconfig`, `.env`, …) get a hardened per-file confirmation.
`c.close(intent='task done')` once you've confirmed. Close cleans up
around the files you chose to leave: a workdir whose files all transferred
back is removed entirely, but where you left files behind (a selective
`outputs` manifest's unselected files, over-cap leftovers) the host can't
know whether one of them is your next job's input, so it prunes the workdir
down to exactly those keepers and leaves them at their original paths (a
recoverable failed harvest is kept whole for the same reason). Those
surviving workdirs are named under `CloseReport.preserved_workdirs` — chain
from them, then remove them yourself with
`c.call_command('rm -rf <workdir>', intent=...)` once the chain is done,
since only you know when they've stopped being useful. Hand back the report
verbatim.

If your kernel was reaped mid-run and you no longer hold `c` or `job`,
don't retype the submit from scrollback and don't guess whether it went
through: `host.compute.ledger()` lists every job this conversation owns
(with its state), and `host.compute.create('<target>').attach_job(job_id)`
re-attaches by id — the host checks the id exists and is yours before
handing the Job back.

## What to record

The `compute_details()` tool is the only state that survives across sessions,
and three of your inputs are the user teaching you how their host works: an
`ask_about_compute` answer, an `ApprovalRedirected` reply from a declined
approval (they clicked Respond and typed what to do instead — the exception's
`.redirect` carries their words), or guidance relayed in the conversation.
When one arrives, treat it as a teach loop — read the durable fact, append it
via the `compute_details({mode:'append'})` tool with a `per user <date>` tag,
echo back what you understood in your next `intent` so the user sees the
teaching landed, then act on it.

Record an activation/partition/account combination you watched succeed too,
tagged with how you know: `verified <date>` if you ran the entrypoint and saw
exit 0, `per user` if from `ask_about_compute`, `untested` if inferred. A
single inline gotcha ("this tool needs `module load cuda/<ver>` here") is worth
keeping; per-job state and transient errors aren't.

When asking, ask once per gap and batch related questions ("Which partition and
account for GPU jobs, and how do I activate `<tool>`?"). Never ask what one
`c.call_command()` would tell you — `module avail` first, then ask for what
only the user knows: their account string, which env they prefer, whether you
may install.

The test for whether something belongs here is whether it is true of the host
or compute provider, or true of the work you ran on it. A preemption limit is
about the provider; a method choice or a result is about the project, and it
will sit in front of every future session on this machine — including
unrelated projects — long after it has stopped being true. The same goes for
what you learn about the user: that belongs in memory, where it is scoped and
correctable. When a session ends and nothing new about the provider came up,
the right amount to write is nothing.

## When the job fails

Read `res.exit_code`, `res.stdout_tail`, and the harvested log (`res.notes`
carries any host-authored remedy hint). An infrastructure failure (wrong
partition, env not activated, missing module, OOM, walltime) is yours to fix
— adjust `command`, record the fix, fresh `c.submit_job()`. On an ssh target
the run clock is a backstop, not a countdown: a job that overruns is stopped
about five minutes past `run_timeout_s` (slack counted from submit, so queue
time is part of it) and lands as `state == 'failed'` — `res.exit_code == 124`
(the remote `timeout` fired) or `res.error_kind == 'timeout_ceiling'` (the
host cancelled it) is what tells that shape apart from an ordinary crash, and
its partial outputs are already harvested. Resubmit with a larger
`run_timeout_s` only if the work was genuinely cut short. There is no
`state == 'timed_out'` and no `JobTimedOut` on ssh, so don't branch on them
here. A tool failure (the science tool ran but errored on
inputs) may be a bad flag or bad input data; one `c.call_command()` to inspect
the log usually says which. Infrastructure-fix retries are cheap on a short
smoke test and expensive on a long allocation, so after two failed submits on
the same job, ask before a third.

Failures raise typed exceptions — every leaf under `host.compute.Error`
carries `.kind`, `.retryable`, and a `.next_step` line, and still catches
under a plain `except RuntimeError`. Two are decisions, not errors to retry:
`ApprovalDenied` means the user said no (its message may carry their reason
— follow it or ask, never resubmit the same card), and an error whose
`.retry_after` is `'user_action'` means the host itself is unreachable (key
not loaded, VPN, host down) — call `ask_about_compute` with the error text
and wait rather than looping on your own.

## `c.submit_job()` on SSH

`command` is a job script. The host hoists scheduler directives from the top
into the dispatch wrapper, so write them as if you were handing the file to
`sbatch`/`qsub` yourself — one directive per line starting with the scheduler
prefix and a space. The host adds `--job-name`/`--output` bookkeeping (yours
can't override those); GPU/time/partition/account are yours. Don't write
`--array`/`--chdir`/`--wrap` — submit one job per task instead. PBS
(`#PBS -l ...`) and LSF (`#BSUB ...`) follow the same pattern with their
prefix; for `scheduler: none`, omit directives entirely.

The job runs under a login shell, so tools on the host's default
`module`/`conda` PATH are visible — but writing the activation into `command`
is still the reliable path (deterministic, and what gets recorded in the
details doc). There is no separate environment argument: env activation is
part of the command string. The script runs under `bash -eo pipefail`. If you
background subprocesses,
`wait` alone returns 0 regardless of their exit codes — capture each pid and
`wait $pid` (or `wait -n` in a loop) so a failing branch surfaces as a non-zero
`exit_code`. `job.cancel()` sends SIGTERM to the process group; a child that
ignores TERM or re-`setsid`s won't be reached, so don't daemonize inside the
script. cwd is a fresh per-job workdir under scratch — inputs stage flat
there as `./<dst>`. `dst` is a bare filename (no `/` — rejected at submit),
defaulting to the source basename. Only files under that workdir are eligible
to come back; if your tool takes an `--output-dir`, point it at `./out` or
`.`, not an absolute path under your home or scratch — anything outside the
workdir isn't transferred back (pull it afterward with `c.download('/abs/path')`;
see the Workflow section for how the approval gate works).
`outputs` globs match on the relative path too, so a subdir output like
`out/x.result` is reached by `'out/*.result'` or plain `'*.result'`
(basename match) — no flatten step needed; still, end the script with an
`ls -lh` of the expected files (`|| true`-guarded) so the log shows what's
there before the transfer. The `|| true` matters: under
`-eo pipefail` a missing optional output would otherwise fail the job.
`intent` is the approval-modal headline, the one
line the user reads to decide whether to let this run on their allocation:
name the tool, the target, and the scale; on a retry, say what's different.
`inputs` entries are `{src, dst}` — `src` picks the source by scheme: a
bare workspace-relative path or `artifact://<version_or_artifact_id>` is
staged from this machine (not a kernel-resolved `/sessions/...` path);
`ssh://<this-target>/<abs>` (under a `data_roots:` entry or scratch) is
symlinked, no transfer. Anything over ~100 MB that already lives on the host
should be an `ssh://` locator, not a staged path — staging is link-rate and
copies into the job workdir. `outputs` names the files that come back —
glob strings matched on basename or relative path, landing under
`hpc/<job_id>/` as `output_files`. The transfer back is the slow,
size-bearing step of a job (it rides the same sftp link), so it's the one
thing worth scoping: the files that arrive are your deliverables, and
`save_artifacts()` turns whichever you want into an artifact. For a small job
just omit `outputs` and everything comes back; on a big Nextflow-style run
name the results and let the terabyte of scratch stay put. Leaving a file
out is safe on ssh because the job's workdir persists under scratch until
`close()` — an unnamed file simply stays at `res['remote_workdir'] + '/<path>'`,
which is exactly how you park a large asset (a BAM, a trajectory) for the
next job to chain from: leave it out of `outputs`, then pass
`inputs=[{'src': f"ssh://<target>{res['remote_workdir']}/<path>"}]` to the
next `submit_job`. Nothing lists the unnamed file — you wrote it, so you
know where it is. `exclude=['work/**', '*.tmp']` subtracts globs from
whatever would come back (drop scratch out of a broad `outputs`, or trim the
everything-default); an excluded file appears in no list.
`transfer_limits={'max_file_mb': 500, 'max_total_mb': 2000}` is a size
budget for the same reason the transfer is worth bounding at all — a
multi-GB pull would stall your wake-up and fill the workspace, so an
over-budget file is left where it is and named in `left_on_remote`
(`reason:'over_cap'`, with an `ssh://<target>/<abs>` URI ready to feed a
later `inputs`); the total budget trims smallest-first so the most files
come back. That protection is on even when you set nothing: any single file
over ~100 MB stays behind by default (`reason:'threshold'`), and
`max_file_mb` is how you move that bar deliberately when you do want a big
file locally. `left_on_remote` is only ever the *involuntary* leftovers
(caps, threshold, `harvest_failed`), each a chainable `ssh://` URI, or a
target for `c.call_command(f'head -c 4096 {shlex.quote(path)}', intent=...)`
to peek; `c.download()` it only when you or the user actually need the bytes
locally — it's link-rate-slow and the file is already where the next job
needs it. `run_timeout_s` is the one run clock (integer seconds); omitted,
the host's default for this target applies and `job.notes` says so. On ssh
it is a backstop rather than a live countdown, so `job.deadline` /
`job.time_left_s()` read `None` here — the note carries the clock's shape
(stopped ~5 min past `run_timeout_s`, lands `failed` / `exit_code == 124`).

```python
# repl tool — host.compute isn't attached in the `python` tool
c = host.compute.create('<cluster>')            # bare name; 'ssh:<cluster>' works too
job = c.submit_job(
    intent='<tool> on <input> — 1 GPU, ~10 min',
    command='''#SBATCH --gres=gpu:1
#SBATCH --time=15
#SBATCH --partition=<partition>

module load <tool>/<ver>
<tool> ./in.dat --out ./out
cp ./out/*.result ./out/*.json ./ 2>/dev/null || true
ls -lh ./*.result ./*.json''',
    inputs=[
        'in.dat',                                          # workspace-relative (prepared in a `python` cell) → ./in.dat
        {'src': 'artifact://<version_or_artifact_id>', 'dst': 'ref.dat'},   # artifact-store file — dst REQUIRED
        # or chain a prior job's file: {'src': prev_files[0], 'dst': 'prev.out'}
        # or a file already on this cluster: {'src': 'ssh://<target>/lustre/ref.fa'} (symlinked, not copied)
    ],
    outputs=[
        '*.result',        # only these come back → output_files under hpc/<job_id>/
        '*.json',
        # traj.dcd is left out → it stays in the persistent workdir on the cluster;
        # chain it later via inputs=[{'src': f"ssh://<target>{res['remote_workdir']}/traj.dcd"}]
    ],
    exclude=['work/**'],              # scratch you never want back
    run_timeout_s=900,
)
# the Job repr just printed itself: id, notes (run clock included), outdir, and
# the re-attach line — the cell ends here, the kernel never waits on the job
```

Then call the `wait_for_notification` brain-tool. The `compute_done`
notification payload carries `{job_id, state, status, exit_code, notes,
output_files, output_file_count, ...}`; act on it directly:

```python
# after wait_for_notification returns the compute_done payload —
# output_files paths are workspace-relative under hpc/<jobId>/
save_artifacts(payload['output_files'])   # publish with provenance
```

If you need the fuller record (`files`, `left_on_remote`, `remote_workdir`,
`stdout_tail`, `notes`):

```python
# repl tool — read what the poller already brought back (never a wait)
res = c.attach_job(job_id).result()   # JobResult; res["output_files"] etc. still work
print(res.state, res.exit_code, res.stdout_tail)
c.close(intent='task done — clean up the workdirs')
```

`res.files` is the complete transferred list (uncapped); the same files
are on disk at `hpc/<job_id>/`.

Each `.submit_job()`/`.call_command()` that isn't Always-Allowed shows one
approval modal; max 10 — batch fan-out into one job script, or have the user
click Always-Allow if you're looping.

## When the user gives you a budget

A user who says "stay under twenty nodes" or "keep it to a hundred at a
time" is giving you a number that the prompt alone can't enforce. You'll
write it into the orchestrator's instructions, but the sub-agents you
delegate to start with fresh context — they never see that line, and
each one will reasonably try to use as much compute as its own task
seems to warrant. Across a wide fan-out that drifts well past whatever
the user had in mind, and the first sign is usually the cluster admin's email.

`host.compute.set_concurrency_limit(k)` exists so the user's number
becomes a property of the session rather than a sentence in a prompt.
Call it once before delegating; the daemon stores it against the session
root and counts every sub-agent's live job against the same `k`. A submit
that would put the session over the cap raises
`host.compute.ConcurrencyFull(live, limit)` in whichever frame made it,
and the right response depends on who holds the slots — a `compute_done`
is only ever delivered to the frame that submitted that job. So a frame
with its own running job ends the cell and parks on `wait_for_notification`
(one of its own jobs finishing frees a slot and wakes it), then resubmits.
A sub-agent refused because *siblings* hold the cap (`e.owns_live_jobs` is
False) will never receive their notifications; parking would sleep
forever, so it hands back to the orchestrator instead. The exception's
`next_step` says which case you're in. Read `c.concurrency`
(`Slots(live, limit)`) before a wide fan-out so you size the batch to the
free slots rather than discovering the cap by exception. Sub-agent code is
otherwise unchanged.

Choosing `k` has one constraint beyond the user's intent: each provider
also has its own ceiling, and that ceiling refuses rather than queues —
a session limit above it just stops being the binding constraint, and
submits past the host's own ceiling raise a `Busy` error instead of
`ConcurrencyFull`. `host.compute.status()` returns both your `k` and the
provider ceilings, so you can pick a value that actually parks. When
the user hasn't given a number, leaving the limit unset keeps today's
behavior; set one yourself only if a fan-out is wide enough to threaten
the host cap.

## Submitting several jobs

Submitting a batch and collecting each as it finishes uses the same
`wait_for_notification` mechanism, just called repeatedly. The poller
tracks every job you submitted, transfers each one's outputs back independently when the
remote reports it terminal, and posts one `compute_done` per job; each
`wait_for_notification` call returns whatever's queued (one or more) and
then blocks for the next.

```python
# repl tool — submit, then end the cell
c = host.compute.create("gpu-cluster")
free = (c.concurrency.limit - c.concurrency.live) if c.concurrency else 5
jobs = [
    c.submit_job(
        command=f"python fold.py --seed {s} --in input.fasta --out ranked.pdb",
        intent=f"AlphaFold seed {s}",
        inputs=["input.fasta"],
        outputs=["*.pdb"],
        run_timeout_s=3600,
    )
    for s in range(min(5, free))
]
print([(j.id, j.state()) for j in jobs])   # one live read each — fine once, not in a loop
```

Then loop the brain tool. Each call's `notifications` list may contain
more than one entry if two jobs finished while you were processing the
previous batch, so iterate it; the loop ends when the call returns
`{status:'error'}` because no compute jobs remain — that error is final,
not transient: stop calling `wait_for_notification` there and move on to
collecting and closing.

```text
wait_for_notification(timeout_seconds=1800)
→ {status:'received', notifications:[
     {notification_type:'compute_done',
      payload:{job_id:'…', intent:'AlphaFold seed 3', state:'succeeded',
               exit_code:0, output_files:['hpc/…/ranked.pdb']}}]}
# act on each payload (save_artifacts, or c.attach_job(jid).result()
# for stdout_tail / full files), then:
wait_for_notification(timeout_seconds=1800)
→ {status:'received', notifications:[ …seed 0…, …seed 4… ]}   # two arrived
# act on both, then:
wait_for_notification(timeout_seconds=1800)
→ … repeat until …
→ {status:'error',
   error:'Nothing can wake this wait: no delegated children running, no background cells, no unread notifications, no compute jobs in flight.'}
# done — every job has reported; don't call it again
```

When everything you care about is back, call `c.close(intent=...)`
once — it removes the fully-transferred jobs' workdirs and keeps any
workdir still holding files you left behind (listed in
`preserved_workdirs`). Don't put the `create()` in a `with` block —
`__exit__` calls `close()`, which would cancel the still-running jobs the
moment the submit cell ends.

## When the user asks you to set up the host

If the user explicitly asks for help getting a tool or environment running on
this host — *"can you set up boltz here"*, *"install the proteomics stack on
my cluster"*, *"get this box ready for GPU jobs"* — that's
environment-provisioning work, and the `compute-env-setup` skill is the
guide. It walks through the shape of the problem on whatever kind of host
this is (direct conda, Slurm modulefile or `.sif`, container-via-runner,
managed API), the declarative spec for what each env needs, where weights go,
and how to validate that the documented invocation actually works rather than
just that imports succeed. Read `compute_details` first to understand what's
already there and what kind of host you're on, then follow that skill. Treat
it as its own task with its own validation loop — don't fold provisioning
into a job submission.

## When it's unclear what's available on the host

Sometimes `compute_details(provider)` doesn't give clear guidance on which
environment has the package you need, or whether the tool is installed at
all — the doc might be sparse, stale, or just not mention the thing you're
after. Before assuming it's missing, it's fine to probe: send a handful of
quick remote commands (something like `which <tool>`, `conda env list`,
`module avail 2>&1 | grep -i <tool>`, `python3 -c 'import <pkg>'`,
`ls $SCRATCH/images/` — up to ~5 cheap checks) to see if it's already there
under a name the doc didn't capture. If a probe finds it, use it and append
what you learned about the provider to `compute_details` so the next agent
doesn't repeat the search.

If the probes come back empty or ambiguous, that's the point to bring the
user in rather than guess: *"I don't see `<tool>` set up on this host — I
checked conda envs, modules, and the usual paths. I can set it up here
(that's a separate step, a few minutes for a CPU env, longer for GPU +
weights), or if it's somewhere I didn't look, point me at it?"* Setting it
up is environment-provisioning work — see the `compute-env-setup` skill,
which covers building the stack on whatever shape this host is (direct conda,
Slurm modulefile or `.sif`, container-via-runner, managed API), wiring
weight caches, and validating the documented invocation actually works.

Don't improvise installs inline with a job submission; provisioning has its
own validation loop and a half-built env is harder to debug than starting
clean.
