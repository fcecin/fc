-- Publish this file as an fc website that sells a booklet for 2 credits.
-- A click on Buy asks fc for the payment. fc shows the request to the visitor
-- in its own bar, and an approval sends a paid call with the order as the
-- memo. A paid call cannot be refused, so any payment of the price is a
-- purchase. The buyers stay in a store, so a new instance keeps them.
local PRICE = 200000000   -- raw units: 2 credits
local incoming = {}

local function hex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local path = "/h/" .. hex(ces.owner_pubkey()) .. "/booklet.kv"
local buyers = ces.store(path)
if not ces.file_stat(path) then buyers:create() end

local function bought(key) return buyers:get(hex(key)) ~= nil end

local function reply(conn, status, headers, body)
  conn:write("HTTP/1.1 " .. status .. "\r\n" .. headers ..
    "Content-Length: " .. #body .. "\r\nConnection: close\r\n\r\n" .. body)
  conn:close()
end

local function page(conn, title, body)
  reply(conn, "200 OK", "Content-Type: text/html; charset=utf-8\r\n",
    '<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">' ..
    "<title>" .. title .. '</title><link rel="stylesheet" href="/style.css"><h1>' .. title .. "</h1>" .. body)
end

local BUY = '<form method="post" action="/buy"><button>Buy the booklet for 2 credits</button></form>'

local function handle(conn, method, target)
  local path = target:match("^[^?]*")
  if method == "POST" and path == "/buy" then
    if bought(conn.pubkey) then return reply(conn, "303 See Other", "Location: /booklet\r\n", "") end
    -- A new order name for each attempt, also across launches.
    local id = "booklet-" .. hex(ces.random_bytes(8))
    return reply(conn, "200 OK", "Content-Type: application/vnd.fc.payment+json\r\n",
      string.format('{"order":"%s","description":"The club booklet","amount":"%d","return":"/booklet"}',
        id, PRICE))
  end
  if method == "GET" and path == "/style.css" then
    return reply(conn, "200 OK", "Content-Type: text/css\r\n",
      "body { max-width: 40rem; margin: 2rem auto; padding: 1rem; font: 1.1rem system-ui }")
  end
  if method == "GET" and path == "/booklet" then
    if bought(conn.pubkey) then
      return page(conn, "The club booklet", "<p>Meetings are on Thursdays at seven. Bring a friend.</p>")
    end
    return page(conn, "The club booklet", "<p>You have not bought the booklet yet.</p>" .. BUY)
  end
  if method == "GET" and path == "/" then
    return page(conn, "The club shop", "<p>The club booklet tells you when we meet.</p>" .. BUY ..
      '<p><a href="/booklet">Read the booklet</a></p>')
  end
  reply(conn, "404 Not Found", "Content-Type: text/plain\r\n", "Page unavailable.")
end

-- fc sends the approved payment as a paid call, with the order as the memo.
-- The program keeps the credits whatever it replies, so a payment of the
-- price is a purchase by the payer, also for an order that this instance did
-- not make. The key pays its own rent in the store: 0.001 credit.
function on_l2call(memo, info)
  if info.value < PRICE then return "the booklet costs " .. PRICE .. " raw" end
  local ok, err = buyers:put(hex(info.payer), "1", 100000)
  if not ok then return "store error " .. tostring(err) end
  return "the booklet is yours"
end

ces.conn.set_listener({
  on_open = function(conn) incoming[conn.id] = "" end,
  on_data = function(conn, bytes)
    local buffer = incoming[conn.id]
    if not buffer then return end
    buffer = buffer .. bytes
    if #buffer > 16384 then
      incoming[conn.id] = nil
      return reply(conn, "413 Content Too Large", "", "")
    end
    local head, body = buffer:match("^(.-)\r\n\r\n(.*)$")
    local length = head and tonumber(head:lower():match("\r\ncontent%-length:%s*(%d+)")) or 0
    if not head or #body < length then
      incoming[conn.id] = buffer
      return
    end
    incoming[conn.id] = nil
    local method, target = head:match("^(%u+) (/[^%s]*) HTTP/1%.1")
    if not target then return reply(conn, "400 Bad Request", "", "") end
    handle(conn, method, target)
  end,
  on_close = function(conn) incoming[conn.id] = nil end,
})
ces.run()
