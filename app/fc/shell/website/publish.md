# Publish a website

1. Put the program on a network server with a deposit, and launch it with `cesh compute launch <file>.lua`.
2. `desk publish <name> <file>.lua on <server>` prints the link of the website. A bare file name is a file in your CES home.
3. Run `desk visit <name>`. When it shows the page, give the person the link.

`desk unpublish <name>` withdraws the link. The program keeps running.

When no instance of the file runs, for example after a restart of its server, the next visit starts one, and the balance of the file pays for it. So to stop a website, withdraw the link first, and then stop the program.

## Change the website

1. Put the changed file, and launch it again. The link stays the same, and each visit goes to the newest instance of the file.
2. Stop each older instance with `cesh compute kill <pid>`, because an instance costs credit while it runs. `cesh compute ps` lists your instances, and the newest has the highest pid. Do not stop the newest, because it serves the link.
3. Run `desk visit <name>` as the last step, after you stop the older instances.

## Test the website

The link needs a browser where a person is signed in, so `curl` on the link gets a redirect to the sign-in page. `desk visit <name> [<path>]` shows a page as you get it through the link, with the status, the type and the body. The path is the program's path, such as `/` or `/posts`. It goes to the program that the link reaches now, with your key.

To send a form, test the program over a dial. A dial from `cesh dial` carries your key, as a visit carries the visitor's key.

```sh
printf 'GET / HTTP/1.1\r\nHost: site\r\n\r\n' | timeout 5 cesh dial <pid>
printf 'POST /sign HTTP/1.1\r\nHost: site\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 8\r\n\r\nname=Ann' | timeout 5 cesh dial <pid>
```

The reply is the status line, the headers and the body that the browser gets.

## Credit of the visitors

CES lets a key dial a program only when the key has credit on the program's server. When a visitor has no credit there, the site moves 0.01 credit of theirs from their own server first.
