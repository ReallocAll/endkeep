# Worker shutdown and reload

The repository Worker finishes the current task before exiting after BDS
stops, including when BDS is forcefully terminated. The Worker checks the
controller process once per second; this does not run on the BDS tick.

Plugin unload leaves a ten-second handoff window. Reload can adopt the
same Worker and receive its existing task result. If reload takes longer,
the Worker drains its task and exits; the next instance recovers the
repository and receives any saved, unacknowledged results. If a detach
request cannot be delivered while BDS remains alive, five minutes without
control traffic also triggers draining.

A verified replacement controller can adopt a draining Worker while its
task is still active. Once it is idle and exiting, startup waits for the
instance lock to be released before starting a replacement.

The Worker holds an OS-backed instance lock until repository processing
has stopped. Existing repository transaction locks and atomic commits
remain in place. Startup retries temporary communication failures for
about five seconds. A live, unresponsive Worker is never killed or
replaced when its writer ownership cannot be disproved.

Workers using the previous control protocol must finish and exit before
this build can connect. Upgrade during a full maintenance shutdown.

Unacknowledged results are kept in a runtime journal separate from the
backup format. A persistent failure to write that journal retains the
Worker and its lock rather than silently discarding the result. Restore
write access to allow safe exit. Likewise, a task stuck indefinitely in
native code cannot be forcibly ended without compromising the safety
guarantee.

The maintained Amulet 3.0.8a1 Wheels require Linux x86-64 with glibc 2.34
or newer, or Windows x86-64. Python 3.12–3.15 remains supported.
