# fc

The source of fc is at https://github.com/fcecin/fc, and the source of CES is at https://github.com/fcecin/ces. Both are in the public domain.

CES is a network of servers that already runs. Each server keeps a ledger of credit, stores files and runs programs in Lua. The servers settle credit with each other through reserves, and one key is one identity on all of them.

fc is a web page in front of that network. When a person signs in with an email address, the site makes them a CES key and gives them credit on its own ledger. The ledger keeps large reserves with the servers of the network, so this credit moves to any of them. On the same page, an assistant works for the person, with that key and that credit.

Credit comes from a grant or from mining, and no money enters. The answers of the assistant cost credit, and so do files and programs.

A program on a network server runs while its deposit lasts, and anyone can open a window on it. So people meet in programs, such as a game that many people play at once. The site gives each person a key, a shell, a desktop and an assistant. Everything else can be a program that the assistant writes on CES. Anyone can run another CES server and peer it with the network.

## What a person has

- A key. The site keeps it sealed, and only the site's bridge signs with it. The account page exports it, and the key works on every CES server without the site.
- Credit on the site's ledger, from the grant at the first sign-in and from the fc miner.
- A desktop, which the site keeps. Its windows show the chat, the shell, texts and programs.
- A shell. It is bash in a sandbox of its own on the site. Its home holds 16 MB, and logging out deletes it.
- The network page. It shows the person's server and its neighbors. There the person changes servers, sends credit and moves credit between neighbors.

## The assistant

The assistant is a language model. The site's ledger asks the model for each answer, and the person's credit on the ledger pays for it. The assistant reads the conversation, with a summary of the older part when the conversation gets long.

It works in the person's shell. It types each command at the person's prompt, so the person sees the command and what it prints. A command stops after 60 seconds, so a longer task runs as a job, which wakes the assistant when it ends. `~/AGENTS.md` tells it how the shell and the site work.

## What it can build

The shell has bash, Python 3 and the other tools of the site's machine, and no network. Two commands reach out of it:

- `cesh` runs CES commands with the person's key, for files, programs, dials, credit and names. `cesh man` is the manual. The key stays out of the shell.
- `desk` opens, moves and closes windows. It also reads a program window as the person sees it, and presses keys and clicks there. So the assistant tests a program the way a person uses it.

`job` runs a long command in the background, such as a build or a wait for a program.

A program is a Lua file on a network server. With the `ces` API, a program can:

- answer paid calls, and keep the credit that callers pay
- accept dials, as a terminal or as a window
- read and write files and a key-value store, which stay while their rent is paid
- send credit, also to keys on other servers
- use the files and compute of other CES servers as its own key, and start programs from its own files
- sign and verify messages, and run tasks, timers and channels.

A window program draws with VS1, a screen of character cells with Unicode text in 24-bit color. Each viewer sends keys, clicks and text, and each viewer can have a screen of their own. So one window program can be a game for many players, or an editor that people share.

## Limits

- The shell home holds 16 MB, and logging out deletes it. The shell has no network, and a command stops after 60 seconds.
- A program is one Lua file in LuaJIT, which is Lua 5.1. It has no `io`, `os` or `require`, and its owner cannot read a log of it. `ces.on_error` catches its errors.
- Only the owner of a file can start it as a program.
- A window shows character cells only.
- The site's ledger holds only credit. Files and programs need credit on a network server first.
- The assistant knows the current conversation and what it reads in the shell. It can be wrong.
