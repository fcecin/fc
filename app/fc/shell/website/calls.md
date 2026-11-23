# A website that calls other programs

A website is a CES program. It can call and pay other programs on any CES server, and other programs can call and pay it. `cesh man lua/servers` has the calls, with an example that runs, and `cesh man lua/calls` tells how a program takes a paid call.

## Take calls

Define `on_l2call(memo, info)` and return a text. `info.payer` is the key of the caller and `info.value` is what it paid, in raw units. The payments that visitors approve come to the same function, with the order as the memo, so give the calls of programs memos of their own, such as `price tea`.

## Make calls

1. `ces.ping(server)` gives the rpc port and the key of a server, and `ces.compute_client(host .. ":" .. rpc_port, key)` opens it.
2. A pid changes with each launch, and a source path stays. So find the other program by its path with `cc:instances(path)`, and take the newest instance.
3. `cc:call(pid, raw, memo)` pays `raw`, which can be 0, and returns the reply. Then `cc:close()`.
4. The key of your program pays `raw` and a small fee from its account on the other server. On its own server that is the balance of its file. On another server, give it credit first. `cesh --server <its server> compute stat <pid>` prints the program key, and `cesh cross <program key> <raw> <other server>` sends it credit from your server.

- A visitor waits at most 15 seconds, and a call can take 30. So make the calls in a task, such as `ces.every`. Keep the last answers, and show them on the page with their age.
- An empty reply is not an answer, and the program that took the call keeps the payment. When a call with a payment ends in `TIMEOUT`, `CONNECTION_LOST` or `INTERNAL`, the result is not sure, and a second call can pay twice.
- A task that fails with an error ends the program. So run the calls in `pcall`, as the pages.
