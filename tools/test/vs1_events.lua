-- The events in host.args[1] (hex), one line each, as VS1 reads them.
-- tools/test/vs1.test.mjs feeds it what the monitor (fc/monitor.js) writes,
-- to check that the two agree.
local hex, bytes = host.args[1] or "", {}
for i = 1, #hex, 2 do bytes[#bytes + 1] = string.char(tonumber(hex:sub(i, i + 1), 16)) end
local c = {id = 1}
for e in vs1.events(c, table.concat(bytes)) do
  local mods = (e.shift and "S" or "") .. (e.ctrl and "C" or "") .. (e.alt and "A" or "") .. (e.meta and "M" or "")
  if e.type == "key" then host.write("key " .. e.action .. " [" .. e.key .. "] " .. mods .. " " .. e.time .. "\n")
  elseif e.type == "text" then host.write("text [" .. e.text .. "]\n")
  elseif e.type == "pointer" then
    host.write("pointer " .. e.action .. " " .. e.button .. " " .. e.x .. " " .. e.y .. " " .. e.time .. "\n")
  else host.write("focus " .. tostring(e.on) .. " " .. e.time .. "\n") end
end
