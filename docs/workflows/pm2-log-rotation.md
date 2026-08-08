# PM2 Log Rotation

## Authority

This workflow is the single source of truth for enabling PM2 log rotation in an
`llm-collab` workspace. Other setup and adapter documentation links here instead
of copying the procedure.

One `pm2-logrotate` instance is expected to cover both ecosystem-defined logs
under `Logs/watchers/` and ad-hoc apps using PM2's `~/.pm2/logs/` default. That
expectation comes from the module's
[pinned upstream implementation](https://github.com/keymetrics/pm2-logrotate/blob/15f30dc0deb27d77b8095ee105af90a2f53d96b9/app.js#L184-L220),
which inspects each active app's `pm_out_log_path` and `pm_err_log_path`. Source
inspection is not host evidence; the procedure is incomplete until the final
two-path check passes.

## Procedure

### 1. Preserve existing evidence before installation

Complete the operator-owned disposition of existing logs before enabling
rotation. Installing `pm2-logrotate` starts the module and rotation immediately.
Archive any history needed for diagnosis, especially unique crash-loop evidence,
outside the live PM2 paths before running the install command. Do not blindly
delete or truncate existing logs. Inspect orphaned files left by deleted PM2
entries separately because the module's target list covers active app paths only.

### 2. Install, configure, and start

```bash
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 10M
pm2 set pm2-logrotate:retain 7
pm2 set pm2-logrotate:compress true
pm2 set pm2-logrotate:rotateInterval '0 0 * * *'
bin/llm-collab pm2_watchers.py start --all
```

### 3. Verify both stores

Wait beyond pm2-logrotate's default 30-second worker interval, then run its
[read-only target-list action](https://github.com/keymetrics/pm2-logrotate/blob/15f30dc0deb27d77b8095ee105af90a2f53d96b9/app.js#L251-L258):

```bash
sleep 35
pm2 trigger pm2-logrotate 'list watched logs'
```

PASS only if the returned map contains at least one active log under
`Logs/watchers/` and at least one under `~/.pm2/logs/`. If either path class is
missing, treat rotation coverage as failed and do not claim both stores are
protected; diagnose the module/path mismatch or replace the mechanism.

## Retention policy

Keep the current file plus seven gzip-compressed generations per PM2 log, with a
10 MiB size trigger and daily rotation. Ordinary logs retain about one week of
history. A noisy process may rotate sooner, deliberately bounding a crash loop
instead of guaranteeing seven days at any write rate.
