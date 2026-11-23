# The program of a website

- Each dial carries one HTTP/1.1 request, a GET or a POST, with `Host: site`. A HEAD of the browser comes to the program as a GET, and the site gives the browser the length of the page with no body. In `on_data`, add the bytes to a buffer until the headers end with an empty line, and until the body has its `Content-Length`.
- Write one reply with a status line, `Content-Type`, `Content-Length` and the body. Then close the connection with `conn:close()`.
- `conn.pubkey` is the key of the visitor, as 32 raw bytes. Every dial has it, also a dial from `cesh dial`. A byte of a key can have any value, also the value of a space or a newline, so do not check the bytes. To show a key as text, use `key:gsub(".", function(c) return string.format("%02x", c:byte()) end)`. Use the key to know who posts, who owns what, and who paid. The request has no cookies, and the site drops the cookies of a reply.
- A request body can hold 1 MiB and a reply 8 MiB. The program answers in 15 seconds, or the visitor gets an error.

## Paths and links

The website lives under its link. The program gets the path after the link. A visit to `/link/ana/shop/style.css` gets to the program as `GET /style.css`, and the page of the link itself is `/`.

- In HTML, write each link, image, stylesheet and form action as a relative address, with no `/` at its start, such as `href="style.css"`, `src="logo.png"` and `action="buy"`. The browser puts it under the link. An address with a `/` at its start, such as `/style.css`, goes to the site itself, out of the website.
- On a deeper page such as `/posts/7`, a relative address starts from `/posts/`. Use `../style.css` there, or keep the pages at the top level.
- In a redirect (`Location`), write a path of the website with a `/` at its start, such as `/posts`. The site puts it under the link. The site refuses a redirect to another host or out of the website.

## What a page can do

The pages are on the site itself, next to the pages of each person. So the site lets a page do only these things.

- A page shows HTML, plain text, CSS, and PNG, JPEG, GIF, WebP and ICO images. The site refuses other types, also SVG.
- A page cannot run scripts. It loads styles and images only from its own website. A `<style>` element, a `style` attribute and a `data:` image also work.
- A form sends only to its own website. The site sends a form post to the program only when a page of the same website sent it.
- The site gives an image, a stylesheet or a prefetch only to a page of the same website, by the address that the browser sends with it (the referrer). So do not turn the referrer off in a page, with `referrerpolicy` or `<meta name="referrer">`.

## Example

`desk website guestbook.lua` prints a whole website. Copy it, change the pages, and keep the parts that read the request and that write the reply.
