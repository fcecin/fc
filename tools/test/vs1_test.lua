-- Tests of VS1 in the CES sandbox, which tools/test/vs1.test.mjs runs with
-- VS1 as its local vs1, under `ljcheck -s`. A failed check raises an error.
-- What each viewer got goes to stdout as records, [u8 viewer][u32 size][bytes],
-- and the Node test reads them with the monitor (fc/monitor.js).

local function conn(id)
  return {id = id, write = function(self, s)
    self.writes = (self.writes or 0) + 1
    host.write(string.char(id) .. string.char(#s % 256, math.floor(#s / 256) % 256,
      math.floor(#s / 65536) % 256, math.floor(#s / 16777216) % 256) .. s)
    return true
  end}
end

local function eq(a, b, what)
  if a ~= b then error(what .. ": " .. tostring(a) .. " ~= " .. tostring(b), 2) end
end

local c1, c2 = conn(1), conn(2)
local a = vs1.new(6, 3)

-- 1. The first frame: the mode, then the non-blank cells.
a:write(2, 1, "ab")
eq(a.x, 4, "the cursor after a write")
a:ship(c1)
eq(c1.writes, 1, "one write for a frame")

-- 2. With no change, nothing goes.
a:ship(c1)
eq(c1.writes, 1, "no write with no change")

-- 3. One cell, by its code.
a:put(6, 3, 0x2588)
a:ship(c1)

-- 4. A write stops at the right edge, and the cursor goes on.
a:write(5, 2, "xyz")
eq(a.x, 8, "the cursor after a clipped write")
eq(a.cells[2][7], nil, "no cell past the right edge")
a:write("!")                       -- at the cursor, off screen, so nothing
a:ship(c1)

-- 5. UTF-8, and colors.
a:color(0xffcc00, 0x101020)
a:write(1, 3, "\195\169\226\150\136")  -- e with an acute accent, a full block
a:color(0xaaaaaa, 0x000000)
a:title("box")
a:ship(c1)
a:ship(c1)
eq(c1.writes, 4, "the title goes once")

-- 6. Another screen on the same viewer, then back, sends the mode again.
local b = vs1.new(3, 1)
b:write(1, 1, "B")
b:ship(c1)
a:ship(c1)

-- 7. A second viewer gets the whole screen at once.
a:ship(c2)

-- 8. Boxes.
a:fill(1, 1, 2, 2, "#")
a:blit(3, 2, {"ab", "cd"})
a:ship(c1)

-- 9. The person's events, in two parts, with the person's times.
local function le32(n) return string.char(n % 256, math.floor(n / 256) % 256, math.floor(n / 65536) % 256, math.floor(n / 16777216) % 256) end
local function key(action, mods, time, name)
  local body = string.char(action, mods) .. le32(time) .. name
  return string.char(129, #body, 0, 0, 0) .. body
end
local all = key(1, 1, 1000, "ArrowLeft") .. string.char(130, 10, 0, 0, 0, 3, 0, 3, 0, 4, 0) .. le32(1001)
  .. string.char(131, 5, 0, 0, 0, 0) .. le32(1002) .. key(2, 0, 4294967295, " ")
local got = {}
for e in vs1.events(c1, all:sub(1, 9)) do got[#got + 1] = e end
eq(#got, 0, "half an event waits")
for e in vs1.events(c1, all:sub(10)) do got[#got + 1] = e end
eq(#got, 4, "the events")
eq(got[1].type .. " " .. got[1].action .. " " .. got[1].key .. " " .. got[1].time, "key down ArrowLeft 1000", "a key")
eq(got[1].shift, true, "shift")
eq(got[1].ctrl, false, "ctrl")
eq(got[2].type .. " " .. got[2].action .. " " .. got[2].x .. " " .. got[2].y .. " " .. got[2].time, "pointer move 4 5 1001", "the pointer, from 1")
eq(got[3].type .. " " .. tostring(got[3].on) .. " " .. got[3].time, "focus false 1002", "focus")
eq(got[4].action .. " [" .. got[4].key .. "] " .. got[4].time, "up [ ] 4294967295", "the space key, at the last ms of the clock")
for _ in vs1.events(c1, string.char(129, 255, 255, 0, 0, 1)) do error("a giant event") end
eq(c1.vs1_in, "", "a giant event drops the bytes")

-- 10. A viewer that leaves takes its state with it.
a:forget(c2)
eq(a.seen[2], nil, "forget")

-- 11. vs1.keys makes steps, with a test clock in place of CES's clock
-- (ces.spawn and ces.sleep).
local now, sleepers = 0, {}
local function wake(co, ...)
  local ok, at = coroutine.resume(co, ...)
  if not ok then error(at, 0) end
  if coroutine.status(co) ~= "dead" then sleepers[#sleepers + 1] = {co = co, at = at} end
end
ces = {
  spawn = function(fn) wake(coroutine.create(fn)) end,
  sleep = function(ms) return coroutine.yield(now + ms) end,
}
local function advance(ms)
  local target = now + ms
  while true do
    table.sort(sleepers, function(p, q) return p.at < q.at end)
    local s = sleepers[1]
    if not s or s.at > target then break end
    table.remove(sleepers, 1)
    now = s.at
    wake(s.co)
  end
  now = target
end
local steps = {}
local keys = vs1.keys(170, 50, function(c, k) steps[#steps + 1] = c.id .. k end)
local function ev(action, k, time) return {type = "key", action = action, key = k, time = time} end
local c3 = {id = 3}

-- A tap is one step, even when its down and up come together.
keys:feed(c3, ev("down", "a", 1000))
keys:feed(c3, ev("up", "a", 1040))
eq(#steps, 1, "a tap is one step")
eq(steps[1], "3a", "the step of the key of the viewer")
advance(1000)
eq(#steps, 1, "nothing after a tap")
eq(#sleepers, 0, "nothing runs while no key is down")

-- A hold makes one step at once, then one every 50 ms after 170 ms, until the up.
steps = {}
keys:feed(c3, ev("down", "b", 5000))
eq(#steps, 1, "a step at the key down")
advance(169)
eq(#steps, 1, "no step before the delay")
advance(1)
eq(#steps, 2, "a step at the delay")
advance(100)
eq(#steps, 4, "a step each 50 ms")
keys:feed(c3, ev("up", "b", 5280))
eq(#steps, 4, "a hold of 280 ms earns 1 + ceil(110 / 50) = 4 steps")
advance(500)
eq(#steps, 4, "the key up stops the steps")

-- When the network brings a hold in one piece, the steps follow the person's
-- clock.
steps = {}
keys:feed(c3, ev("down", "c", 9000))
keys:feed(c3, ev("up", "c", 9300))
eq(#steps, 4, "a hold of 300 ms is 1 + ceil(130 / 50) = 4 steps")
advance(1000)
eq(#steps, 4, "and no more")

-- When a window loses focus, its held keys go up.
steps = {}
keys:feed(c3, ev("down", "d", 20000))
keys:feed(c3, {type = "focus", on = false, time = 20230})
eq(#steps, 3, "a hold of 230 ms is 1 + ceil(60 / 50) = 3 steps")
advance(1000)
eq(#steps, 3, "and it stops")

-- A viewer that leaves stops its steps.
steps = {}
keys:feed(c3, ev("down", "e", 30000))
keys:forget(c3)
advance(1000)
eq(#steps, 1, "forget stops the steps")

-- A keyboard repeat, and a second down of a held key, make no step.
steps = {}
keys:feed(c3, ev("down", "f", 40000))
keys:feed(c3, ev("repeat", "f", 40050))
keys:feed(c3, ev("down", "f", 40060))
keys:feed(c3, ev("up", "f", 40100))
eq(#steps, 1, "only the first down steps")
advance(1000)
eq(#steps, 1, "and nothing after it")

-- A key held before the window got focus comes as repeats with no down, so
-- it makes no step.
keys:feed(c3, ev("repeat", "g", 50000))
keys:feed(c3, ev("up", "g", 50400))
advance(1000)
eq(#steps, 1, "a repeat with no down makes no step")
eq(#sleepers, 0, "nothing runs at the end")
