# Websites

A website is a Lua program that serves HTTP. People open it in their browser at its link on this site, such as `https://<site>/link/<handle>/<name>/`. For each page, the site dials the program as the visitor, so the program knows the key of each visitor. The browser shows the pages, styles, images and forms that the program sends.

## The program

- Each dial carries one HTTP/1.1 request, a GET or a POST, with `Host: site`. A HEAD of the browser comes to the program as a GET, and the site gives the browser the length of the page with no body. In `on_data`, add the bytes to a buffer until the headers end with an empty line, and until the body has its `Content-Length`.
- Write one reply with a status line, `Content-Type`, `Content-Length` and the body. Then close the connection with `conn:close()`.
- `conn.pubkey` is the key of the visitor, as 32 raw bytes. Every dial has it, also a dial from `cesh dial`. A byte of a key can have any value, also the value of a space or a newline, so do not check the bytes. To show a key as text, use `key:gsub(".", function(c) return string.format("%02x", c:byte()) end)`. Use the key to know who posts, who owns what, and who paid. The request has no cookies, and the site drops the cookies of a reply.
- A request body can hold 1 MiB and a reply 8 MiB. The program answers in 15 seconds, or the visitor gets an error.

## Paths and links

The website lives under its link. The program gets the path after the link. A visit to `/link/ana/shop/style.css` gets to the program as `GET /style.css`, and the page of the link itself is `/`.

- In HTML, write each link, image, stylesheet and form action as a relative address, with no `/` at its start, such as `href="style.css"`, `src="logo.png"` and `action="buy"`. The browser puts it under the link. An address with a `/` at its start, such as `/style.css`, goes to the site itself, out of the website.
- On a deeper page such as `/posts/7`, a relative address starts from `/posts/`. Use `../style.css` there, or keep the pages at the top level.
- In a redirect (`Location`) and in the `return` of a payment, write a path of the website with a `/` at its start, such as `/booklet`. The site puts it under the link. The site refuses a redirect to another host or out of the website.

## What a page can do

The pages are on the site itself, next to the pages of each person. So the site lets a page do only these things.

- A page shows HTML, plain text, CSS, and PNG, JPEG, GIF, WebP and ICO images. The site refuses other types, also SVG.
- A page cannot run scripts. It loads styles and images only from its own website. A `<style>` element, a `style` attribute and a `data:` image also work.
- A form sends only to its own website. The site sends a form post to the program only when a page of the same website sent it.
- The site gives an image, a stylesheet or a prefetch only to a page of the same website, by the address that the browser sends with it (the referrer). So do not turn the referrer off in a page, with `referrerpolicy` or `<meta name="referrer">`.

## Credit

CES lets a key dial a program only when the key has credit on the program's server. When a visitor has no credit there, the site moves 0.01 credit of theirs from their own server first. A paid call does the same for the price.

## Publish the website

1. Put the program on a network server with a deposit, and launch it with `cesh compute launch <file>.lua`.
2. `desk publish <name> <file>.lua on <server>` prints the link of the website. A bare file name is a file in your CES home. Send the link to people.
3. The link stays the same when you launch the program again. Each visit goes to the newest instance of the file. Stop the old instance with `cesh compute kill <pid>`, because it costs credit while it runs.
4. `desk unpublish <name>` withdraws the link. The program keeps running.

## Test the website

The link needs a browser where a person is signed in, so `curl` on the link gets a redirect to the sign-in page. `desk visit <name> [<path>]` shows a page as you get it through the link: the status, the type and the body. The path is the program's path, such as `/` or `/booklet`. It goes to the program that the link reaches now, with your key.

To send a form, test the program over a dial. A dial from `cesh dial` carries your key, as a visit carries the visitor's key.

```sh
printf 'GET / HTTP/1.1\r\nHost: site\r\n\r\n' | timeout 5 cesh dial <pid>
printf 'POST /sign HTTP/1.1\r\nHost: site\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: 8\r\n\r\nname=Ann' | timeout 5 cesh dial <pid>
```

The reply is the status line, the headers and the body that the browser gets. Tell the person the link when the program answers.

## Sell something

1. Reply to a form post with `Content-Type: application/vnd.fc.payment+json` and a JSON object.
   - `order` names this purchase, with up to 64 letters, digits and `._:-`.
   - `description` tells what the visitor buys, in up to 200 characters.
   - `amount` is the price in raw units, as a decimal string, such as `"200000000"` for 2 credits.
   - `return` is the path that the visitor sees after the decision, such as `/booklet`.
2. The site sends the visitor to its own payment page, out of the website. Only the visitor's approval there sends the payment. After the decision, the visitor comes back to the `return` path.
3. The payment is a paid call from the visitor to the program, with the order as the memo. In `on_l2call(memo, info)`, `info.payer` is the visitor's key and `info.value` is the amount. Record the purchase there, and show it on the next page.
4. A paid call cannot be refused. The program gets the credits also when it returns an error or does not know the order. So take any payment of the full price as a purchase by `info.payer`. Keep the purchases in a store (`ces.store`), because a new instance of the program starts with empty memory, and the payment goes to the newest instance.
5. Give each attempt a new order name, also across launches, for example with `ces.random_bytes`. A request for an order that the site knows gives that same payment again, and a declined order stays declined.
6. The site sends a payment at most once. If no result comes back, it shows the visitor that the outcome is unknown.

## Example

This shop sells a booklet for 2 credits. Copy it, change the pages, and keep the parts that read the request and that take the payment.
