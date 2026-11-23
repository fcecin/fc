# A website that sells something

A website gets credits when a visitor approves a payment on the site's own payment page.

1. Reply to a form post with `Content-Type: application/vnd.fc.payment+json` and a JSON object.
   - `order` names this purchase, with up to 64 letters, digits and `._:-`.
   - `description` tells what the visitor buys, in up to 200 characters.
   - `amount` is the price in raw units, as a decimal string, such as `"200000000"` for 2 credits.
   - `return` is the path that the visitor sees after the decision, with a `/` at its start, such as `/booklet`. The site puts it under the link.
2. The site sends the visitor to its own payment page, out of the website. Only the visitor's approval there sends the payment. After the decision, the visitor comes back to the `return` path.
3. The payment is a paid call from the visitor to the program, with the order as the memo. In `on_l2call(memo, info)`, `info.payer` is the visitor's key and `info.value` is the amount. Record the purchase there, and show it on the next page.
4. A paid call cannot be refused. The program gets the credits also when it returns an error or does not know the order. So take any payment of the full price as a purchase by `info.payer`. Keep the purchases in a store (`ces.store`), because a new instance of the program starts with empty memory, and the payment goes to the newest instance.
5. Give each attempt a new order name, also across launches, for example with `ces.random_bytes`. A request for an order that the site knows gives that same payment again, and a declined order stays declined.
6. The site sends a payment at most once. If no result comes back, it shows the visitor that the outcome is unknown.
7. The visitor pays from their balance on the program's server. When it is too low, the payment page shows the credit that the site must move there from the visitor's own server. The site moves it after the approval, and then sends the payment.

## Example

`desk website booklet.lua` prints a shop that sells a booklet for 2 credits. Copy it, change the pages, and keep the parts that read the request and that take the payment.
