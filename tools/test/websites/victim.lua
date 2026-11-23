-- A website for the browser checks of tools/websitecheck.mjs. It keeps each
-- request that reaches it, and GET /log shows them. A check that a page of
-- another website cannot act as its visitor reads this log.
local incoming, seen = {}, {}

local function reply(conn, status, headers, body)
  conn:write("HTTP/1.1 " .. status .. "\r\n" .. headers ..
    "Content-Length: " .. #body .. "\r\nConnection: close\r\n\r\n" .. body)
  conn:close()
end

local function handle(conn, method, target)
  if method == "GET" and target == "/log" then
    return reply(conn, "200 OK", "Content-Type: text/plain\r\n", table.concat(seen, "\n"))
  end
  seen[#seen + 1] = method .. " " .. target
  if target:match("%.png$") then
    return reply(conn, "200 OK", "Content-Type: image/png\r\n", "not really a png")
  end
  if target:match("%.css$") then
    return reply(conn, "200 OK", "Content-Type: text/css\r\n", "body { background: red }")
  end
  reply(conn, "200 OK", "Content-Type: text/html; charset=utf-8\r\n", "<p>victim got " .. method .. "</p>")
end

ces.conn.set_listener({
  on_open = function(conn) incoming[conn.id] = "" end,
  on_data = function(conn, bytes)
    local buffer = incoming[conn.id]
    if not buffer then return end
    buffer = buffer .. bytes
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
