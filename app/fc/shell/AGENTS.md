# AGENTS.md

How the site's assistant works in your shell. The site puts this file in your home, and keeps your changes to it.

## The shell

The shell is bash in your home on the site, which holds 16 MB and is deleted when you log out. The assistant types its commands at your prompt, so you see each command and what it prints, and the shell keeps its directory, variables and history between them. A line you typed but didn't run comes back at the next prompt. The shell is offline, and two commands reach out of it:

- `cesh` is the CES shell, signed with your key. A command goes to your server, or to the one in `--server`. `cesh man` lists its manual pages, and `cesh man <page>` reads one. `chat`, `bootstrap`, `mine`, `keys` and `man --serve` don't run here.
- `desk` drives the site's desktop. `desk help` lists the ops, and `desk man` is the manual.

Python 3 and the usual tools are here too. The site presses Ctrl+C on an assistant command after 60 seconds, so a command that waits for keys, such as a plain `cesh dial`, holds the shell until then. The assistant never exits the shell.

## Jobs

Run anything longer than 60 seconds, or a wait for a program, as a background job:

    job run --wake make -j4                   # wakes you when it ends
    job run --until 'listening' ./serve.sh    # wakes you at the first line that matches
    job run --wake sleep 300                  # a 5-minute timer

`job run` prints the job's number, and then you end your answer. When the job ends or matches, the site gives you its last lines and you go on. A job wakes you at most once, and only with `--wake` or `--until`. `job ls` lists the jobs, `job tail N` shows job N's output, and `job kill N` ends it. The jobs end with the shell.

## Credit, files and programs

The site's ledger holds only credit. Files and programs live on the network servers, so a person first moves credit to a network server, on the site's network page.

A CES file has a balance that pays for it. This puts a file with 1 credit in its balance:

    cesh file put - note.txt --deposit 100000000 <<END
    buy milk
    END

`cesh file put <local> <remote>` and `cesh file get <remote> <local>` copy files between this shell and the server. A bare remote name is in your zone (`/h/<your key>/<name>`).

A program is a Lua file. This one answers a call and every dial:

    function on_l2call(memo, info)      -- cesh compute call <pid> 0 --in "text"
      return "you said " .. memo
    end
    ces.conn.set_listener({
      hello = "hi\r\n",                  -- the first text of each dial
      on_data = function(conn, data) conn:write("got " .. data) end,
    })
    ces.run()                           -- the last line: the event loop

CES runs LuaJIT, which is Lua 5.1, so use `math.floor(a / b)` for `//` and the `bit` library for `& | ~ << >>`. A program with a syntax error doesn't start, and `cesh man lua` is the manual. Put the program with a deposit, which pays for every second it runs, and `cesh compute launch <name>.lua` prints its pid. `cesh compute stat <pid>` checks any pid, and `cesh compute ps` lists yours.

Give every program a few text commands in `on_l2call`, such as `help` and `state`, so that you and other programs can check it with `cesh compute call <pid> 0 --in "state"`, which pays 0 credits.

## Programs that people dial

A terminal program writes plain lines, and people dial it with `cesh dial <pid>`. From the assistant:

- `cesh dial <pid> --line "text"` sends one line and prints the reply.
- `printf 'a\nb\n' | timeout 5 cesh dial <pid> || true` sends the lines and prints what comes back for 5 seconds. Without `timeout` it waits until the program hangs up, and `|| true` keeps your next lines running after `timeout` exits with 124.

A window program draws with VS1: start it with `local vs1 = ces.load("vs1/vs1.lua")`, and `desk vs1` prints its docs. Its input is VS1 messages, so read it in `on_data` with `for e in vs1.events(conn, data) do ... end`. Every key gives a key down and a key up, and the text of `desk type` is one text event. The mouse gives a pointer event for each press, release and move to a new cell, with `e.action` ("down", "up" or "move"), `e.x`, `e.y` and `e.button`, so a click is `e.action == "down"`. Every viewer shares a screen from `vs1.new`, so make each viewer's screen and state in `on_open`, in a table by conn. `desk new program <name> <pid>` opens a window on a VS1 program.

## The person's view

`desk look <name>` and `desk grid <name>` show a window's text, `desk press`, `desk click <name> <x> <y>` and `desk type` send it keys, a click and text, and `desk tail shell` reads what your shell window printed. They show what the person sees, so use them to show or check something once, and drive a program through its own commands.
