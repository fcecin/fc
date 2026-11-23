# Websites

A website is a Lua program that serves HTTP. People open it in their browser at its link on this site, such as `https://<site>/link/<handle>/<name>/`. For each page, the site dials the program as the visitor, so the program knows the key of each visitor. The browser shows the pages, styles, images and forms that the program sends.

To make a website, do these steps.

1. Read `desk website program`, and the example that `desk website guestbook.lua` prints.
2. Write the program. `desk website guestbook.lua > <file>.lua` gives you a copy of the example to change.
3. Put the program on a network server, launch it and publish it. `desk website publish` tells how.
4. Run `desk visit <name>`. When it shows the page, give the person the link.

## Pages

- `desk website program` tells about the request, the reply, the links, and what a page can do.
- `desk website publish` tells how to publish a website, test it and change it.
- `desk website shop` tells how a website sells something.
- `desk website calls` tells how a website calls and pays other programs, and how they call it.
- `desk website guestbook.lua` prints a small website. Each visitor can sign it.
- `desk website booklet.lua` prints a website that sells a booklet.
