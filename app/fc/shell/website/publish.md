# Publish a website

You need credit on a network server. `~/AGENTS.md` tells how to move some there. These commands publish `board.lua` as the website `board`, with 10 credits in the balance of the program.

```sh
S=<server>
cesh --server $S file put board.lua board.lua --deposit 1000000000
cesh --server $S compute launch board.lua       # prints the pid, as "Instance"
desk publish board board.lua on $S              # prints the link
desk visit board                                # shows the page
```

When `desk visit` shows the page, give the person the link. A bare name in `desk publish` is a file in your CES home.

`desk unpublish <name>` withdraws the link. The program keeps running.

When no instance of the file runs, for example after a restart of its server, the next visit starts one, and the balance of the file pays for it. So to stop a website, withdraw the link first, and then stop the program.

## Change the website

The link stays the same. Each visit goes to the newest instance of the file. An instance costs credit while it runs, so stop the older one.

```sh
cesh --server $S compute ps                     # the pid of the instance that runs now
cesh --server $S file put board.lua board.lua
cesh --server $S compute launch board.lua
cesh --server $S compute kill <the older pid>
desk visit board                                # the last step
```

The newest instance has the highest pid. Do not stop it, because it serves the link.

## Test the website

The link needs a browser where a person is signed in, so `curl` on the link gets a redirect to the sign-in page. `desk visit <name> [<path>]` shows a page as you get it through the link, with the status, the type and the body. The path is the program's path, such as `/` or `/posts`. It goes to the program that the link reaches now, with your key.

To send a form, test the program over a dial. A dial from `cesh dial` carries your key, as a visit carries the visitor's key.

```sh
B='name=Ann&text=hello'
printf 'POST /sign HTTP/1.1\r\nHost: site\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n%s' ${#B} "$B" | timeout 5 cesh --server $S dial <pid>
```

The reply is the status line, the headers and the body that the browser gets.

## Credit of the visitors

CES lets a key dial a program only when the key has credit on the program's server. When a visitor has no credit there, the site moves 0.01 credit of theirs from their own server first.
