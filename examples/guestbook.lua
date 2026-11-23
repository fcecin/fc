-- Publish this file as an fc website. Signatures stay for this instance's lifetime.
local names, incoming = {}, {}

local function reply(conn, status, headers, body)
  conn:write("HTTP/1.1 " .. status .. "\r\n" .. headers ..
    "Content-Length: " .. #body .. "\r\nConnection: close\r\n\r\n" .. body)
  conn:close()
end

local function handle(conn, method, target)
  local path = target:match("^[^?]*")
  if method == "POST" and path == "/sign" then
    local key = conn.pubkey:gsub(".", function(byte)
      return string.format("%02x", byte:byte())
    end)
    names[key] = true
    return reply(conn, "303 See Other", "Location: /\r\n", "")
  end
  if method == "GET" and path == "/style.css" then
    return reply(conn, "200 OK", "Content-Type: text/css\r\n",
      "body { max-width: 48rem; margin: 2rem auto; padding: 1rem; font: 1.1rem system-ui } code { overflow-wrap: anywhere }")
  end
  if method ~= "GET" or path ~= "/" then
    return reply(conn, "404 Not Found", "Content-Type: text/plain\r\n", "Page unavailable.")
  end
  local entries = {}
  for key in pairs(names) do entries[#entries + 1] = "<li><code>" .. key .. "</code></li>" end
  table.sort(entries)
  local page = '<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">' ..
    '<title>Guestbook</title><link rel="stylesheet" href="style.css"><h1>Guestbook</h1>' ..
    '<form method="post" action="sign"><button>Sign the guestbook</button></form><ul>' ..
    table.concat(entries) .. '</ul>'
  reply(conn, "200 OK", "Content-Type: text/html; charset=utf-8\r\n", page)
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
