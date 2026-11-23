-- A hostile website for the browser checks of tools/websitecheck.mjs. Each
-- page tries one way to act as the visitor out of this website: at the
-- victim website (V), at fc itself, or at a server outside (OUT). The check
-- puts the owner of the victim in @OWNER@ and the outside server in @OUT@.
local V = "/link/@OWNER@/wc-victim"
local OUT = "@OUT@"
local incoming = {}
-- Each paid call that reaches this program. No check may approve one.
local calls = 0
function on_l2call(memo, info)
  calls = calls + 1
  return "a hostile program took it"
end

local function reply(conn, status, headers, body)
  conn:write("HTTP/1.1 " .. status .. "\r\n" .. headers ..
    "Content-Length: " .. #body .. "\r\nConnection: close\r\n\r\n" .. body)
  conn:close()
end

local function html(body)
  return '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="style.css"><body>' .. body
end

local GO = ' style="position:absolute;left:20px;top:20px;width:200px;height:60px"'
local PAGES = {
  ["/ok"] = '<p id="ok">ok</p>',
  ["/form"] = '<form method="post" action="' .. V .. '/form-post"><input name="a" value="1">' ..
    '<button id="go"' .. GO .. '>go</button></form>',
  ["/formaction"] = '<form method="post" action="ok"><button id="go"' .. GO .. ' formaction="' .. V ..
    '/formaction-post">go</button></form>',
  ["/form-fc"] = '<form method="post" action="/api/send"><input name="to" value="evil">' ..
    '<input name="amount" value="1"><button id="go"' .. GO .. '>go</button></form>',
  ["/form-out"] = '<form method="post" action="' .. OUT .. '/form-out"><button id="go"' .. GO .. '>go</button></form>',
  ["/refresh-site"] = '<meta http-equiv="refresh" content="0;url=' .. V .. '/refresh"><p id="ok">here</p>',
  ["/refresh-out"] = '<meta http-equiv="refresh" content="0;url=' .. OUT .. '/refresh"><p id="ok">here</p>',
  ["/base"] = '<base href="' .. OUT .. '/"><img src="base.png"><a id="go"' .. GO .. ' href="base-click">x</a>',
  ["/base-target"] = '<base target="_blank"><a id="go"' .. GO .. ' href="ok">x</a>',
  ["/css"] = '<style>@import url(' .. V .. '/import.css); body { background: url(' .. V .. '/bg.png) }</style>' ..
    '<link rel="stylesheet" href="' .. V .. '/link.css"><link rel="stylesheet" href="' .. OUT .. '/out.css">' ..
    '<p id="ok" style="background: url(' .. OUT .. '/inline.png)">css</p>',
  ["/hints"] = '<link rel="prefetch" href="' .. V .. '/prefetch"><link rel="preload" as="image" href="' .. V ..
    '/preload.png"><link rel="preconnect" href="' .. OUT .. '"><link rel="dns-prefetch" href="' .. OUT ..
    '"><link rel="prerender" href="' .. V .. '/prerender"><link rel="icon" href="' .. V .. '/icon.png">' ..
    '<link rel="modulepreload" href="' .. V .. '/module.js"><img src="' .. V .. '/img.png"><img srcset="' .. V ..
    '/srcset.png 1x"><img src="' .. OUT .. '/out.png"><video poster="' .. V .. '/poster.png"></video>' ..
    '<p id="ok">hints</p>',
  ["/ping"] = '<a id="go"' .. GO .. ' href="ok" ping="' .. V .. '/ping">x</a>',
  ["/embed"] = '<iframe src="/account/"></iframe><iframe src="' .. V .. '/iframe"></iframe><object data="' .. V ..
    '/object"></object><embed src="' .. V .. '/embed"><p id="ok">embed</p>',
  ["/script"] = '<script>location="' .. V .. '/script"</script><img src="x" onerror="location=\'' .. V ..
    '/onerror\'"><svg onload="location=\'' .. V .. '/svgonload\'"></svg><p id="ok">script</p>',
  ["/jslink"] = '<a id="go"' .. GO .. ' href="javascript:location=\'' .. V .. '/jslink\'">x</a>',
  ["/fakepay"] = '<h2>ana/shop asks for a payment</h2><p>Only this page of fc can approve a payment.</p>' ..
    '<form method="post" action="fake-approve"><button id="go"' .. GO .. '>approve</button></form>',
  ["/"] = '<p id="ok">the hostile website</p>',
  -- The positive control: an image of this website loads.
  ["/img"] = '<img id="px" src="px.png" width="10" height="10"><p id="ok">img</p>',
}
-- A PNG of 1 x 1 pixel.
local PNG = "\137\80\78\71\13\10\26\10\0\0\0\13\73\72\68\82\0\0\0\1\0\0\0\1\8\6\0\0\0\31\21\196\137" ..
  "\0\0\0\11\73\68\65\84\120\156\99\96\0\2\0\0\5\0\1\122\94\171\63\0\0\0\0\73\69\78\68\174\66\96\130"

local function handle(conn, method, target)
  local path, query = target:match("^([^?]*)%??(.*)$")
  if method == "GET" and path == "/calls" then
    return reply(conn, "200 OK", "Content-Type: text/plain\r\n", tostring(calls))
  end
  if method == "GET" and path == "/style.css" then
    return reply(conn, "200 OK", "Content-Type: text/css\r\n", "body { color: rgb(1, 2, 3) }")
  end
  if method == "GET" and PAGES[path] then
    return reply(conn, "200 OK", "Content-Type: text/html; charset=utf-8\r\n", html(PAGES[path]))
  end
  if method == "GET" and path == "/dbl" then
    -- A buy button where fc's approve button opens (x, y from the query).
    local x, y = query:match("x=(%d+)&y=(%d+)")
    return reply(conn, "200 OK", "Content-Type: text/html; charset=utf-8\r\n", html(
      '<form method="post" action="buy"><button id="go" style="position:absolute;left:' .. (x - 40) ..
      'px;top:' .. (y - 20) .. 'px;width:80px;height:40px">buy</button></form>'))
  end
  if method == "POST" and path == "/buy" then
    local order = "dbl-" .. ces.random_bytes(6):gsub(".", function(c) return string.format("%02x", c:byte()) end)
    return reply(conn, "200 OK", "Content-Type: application/vnd.fc.payment+json\r\n",
      '{"order":"' .. order .. '","description":"A hostile order","amount":"1000000","return":"/ok"}')
  end
  if method == "POST" then
    return reply(conn, "200 OK", "Content-Type: text/html; charset=utf-8\r\n", html('<p id="posted">posted</p>'))
  end
  if path == "/px.png" then
    return reply(conn, "200 OK", "Content-Type: image/png\r\n", PNG)
  end
  if path == "/svg" then
    return reply(conn, "200 OK", "Content-Type: image/svg+xml\r\n",
      '<svg xmlns="http://www.w3.org/2000/svg"><script>location="' .. V .. '/svg"</script></svg>')
  end
  if path == "/fake.png" then
    return reply(conn, "200 OK", "Content-Type: image/png\r\n",
      '<h1 id="pwn">html</h1><script>location="' .. V .. '/fakepng"</script>')
  end
  if path == "/setcookie" then
    return reply(conn, "200 OK", "Content-Type: text/html\r\nSet-Cookie: sessionid=evil; Path=/\r\n" ..
      "Set-Cookie: csrftoken=evil; Path=/\r\nRefresh: 0;url=" .. V .. "/refresh-header\r\n", html('<p id="ok">c</p>'))
  end
  if path == "/redirect-out" then return reply(conn, "302 Found", "Location: //" .. OUT:gsub("^https?://", "") .. "/\r\n", "") end
  if path == "/redirect-up" then return reply(conn, "302 Found", "Location: ../../../account/\r\n", "") end
  if path == "/redirect-enc" then return reply(conn, "302 Found", "Location: /%2e%2e/%2e%2e/%2e%2e/account/\r\n", "") end
  reply(conn, "404 Not Found", "Content-Type: text/plain\r\n", "no page")
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
