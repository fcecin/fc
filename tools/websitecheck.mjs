// Browser checks of fc websites in Chromium, Firefox and WebKit.
//
// The checks open the websites of tools/websitecheck_setup.py as a new
// signed-in visitor in each browser. They buy the booklet of the example shop
// and come back to the shop. Then they try each attack of the hostile
// website, and they read what reached the victim website and a server outside
// (OUT). An attack passes when nothing reached either one, and no payment was
// approved.
//
//   node tools/websitecheck.mjs --site http://fc.localhost:8780 --links tmp/wc-links.json \
//     [--browsers chromium,firefox,webkit] [--out-port 8797] [--sessions FILE]
//
// A local site must enter an email at once (FC_LOGIN_WITHOUT_CODE). For a
// site that sends codes, --sessions names a JSON file with a session key of a
// test visitor for each browser, such as {"chromium": "..."}. Playwright comes
// from ~/wyn/playwright (PLAYWRIGHT_BROWSERS_PATH is set from there).
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import os from 'node:os'

const arg = (name, dflt) => {
  const i = process.argv.indexOf('--' + name)
  return i > 0 ? process.argv[i + 1] : dflt
}
const SITE = arg('site', 'http://fc.localhost:8780')
const LINKS = JSON.parse(fs.readFileSync(arg('links', 'tmp/wc-links.json'), 'utf8'))
const BROWSERS = arg('browsers', 'chromium,firefox,webkit').split(',')
const OUT_PORT = Number(arg('out-port', '8797'))
// --out-https: the outside server speaks HTTPS with a certificate of its own,
// for a check of an HTTPS site, where a browser does not load HTTP files.
const OUT_HTTPS = process.argv.includes('--out-https')
const SESSIONS = arg('sessions') ? JSON.parse(fs.readFileSync(arg('sessions'), 'utf8')) : null
const OUT = `${OUT_HTTPS ? 'https' : 'http'}://127.0.0.1:${OUT_PORT}`
const PW = os.homedir() + '/wyn/playwright'
process.env.PLAYWRIGHT_BROWSERS_PATH ||= PW + '/browsers'
const pw = await import(PW + '/node_modules/playwright/index.mjs')
const ENGINES = {
  chromium: [pw.chromium, { executablePath: '/usr/bin/google-chrome' }],
  firefox: [pw.firefox, {}],
  webkit: [pw.webkit, {}],
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
// A frame that the browser blocked has no document, and Playwright waits for
// one with no end. So each look into a frame stops after `ms`.
const within = (ms, promise, fallback) =>
  Promise.race([promise.catch(() => fallback), sleep(ms).then(() => fallback)])

// The server outside: it keeps each request and each connection.
const outside = { requests: [], connections: 0 }
function outsideServer(handler) {
  if (!OUT_HTTPS) return http.createServer(handler)
  fs.mkdirSync('tmp/wc-tls', { recursive: true })
  if (!fs.existsSync('tmp/wc-tls/cert.pem')) {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30', '-subj', '/CN=127.0.0.1',
                             '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', 'tmp/wc-tls/key.pem',
                             '-out', 'tmp/wc-tls/cert.pem'], { stdio: 'ignore' })
  }
  return https.createServer({ key: fs.readFileSync('tmp/wc-tls/key.pem'), cert: fs.readFileSync('tmp/wc-tls/cert.pem') },
                            handler)
}
const server = outsideServer((req, res) => {
  const url = new URL(req.url, OUT)
  if (url.pathname === '/frame') {
    res.writeHead(200, { 'Content-Type': 'text/html' })
    return res.end(`<!doctype html><link rel="icon" href="data:,"><iframe id="f" width="600" height="400" ` +
                   `src="${url.searchParams.get('src')}"></iframe>`)
  }
  outside.requests.push(`${req.method} ${url.pathname}`)
  res.writeHead(200, { 'Content-Type': 'text/plain' })
  res.end('outside')
})
server.on('connection', () => { outside.connections++ })
await new Promise((r) => server.listen(OUT_PORT, '127.0.0.1', r))

const H = LINKS.hostile           // ends with "/"
const results = []
let failed = 0
let running = ''                  // "<browser> <check>", for a crash report

function save() {
  fs.writeFileSync('tmp/websitecheck-results.json', JSON.stringify(results, null, 1))
}

// Playwright can fail inside its own event handlers (seen with WebKit), and
// then no check can go on in that browser. Report it as a failure of the
// check that ran, and stop.
process.on('uncaughtException', (e) => {
  results.push({ browser: running.split(' ')[0], check: running, ok: false, detail: 'crash: ' + String(e).split('\n')[0] })
  console.log(`FAIL ${running}: the check script crashed: ${String(e).split('\n')[0]}`)
  save()
  process.exit(1)
})

async function signIn(page, email) {
  await page.goto(SITE + '/')
  await page.fill('input[name=email]', email)
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/enter') && r.request().method() === 'POST'),
    page.$eval('input[name=email]', (el) => el.form.requestSubmit()),
  ])
  await page.waitForLoadState('load')
}

async function victimLog(page) {
  const r = await page.goto(LINKS.victim + 'log')
  const text = await r.text()
  return text ? text.split('\n') : []
}

async function engine(name) {
  const [type, opts] = ENGINES[name]
  const browser = await type.launch(opts)
  // The certificate of the outside server is its own. The site keeps its own
  // certificate checks.
  const ctx = await browser.newContext({ ignoreHTTPSErrors: OUT_HTTPS })
  const popups = []
  ctx.on('page', (p) => popups.push(p))
  const page = await ctx.newPage()
  if (SESSIONS) {
    const site = new URL(SITE)
    await ctx.addCookies([{ name: 'sessionid', value: SESSIONS[name], domain: site.hostname, path: '/',
                            secure: site.protocol === 'https:', httpOnly: true, sameSite: 'Lax' }])
  } else {
    await signIn(page, `wc-${name}-${Date.now()}@example.test`)
  }
  const victimBefore = (await victimLog(page)).length
  const outBefore = outside.requests.length
  let approveAt = null
  let reviewUrl = null

  // The visitor's payments, as fc keeps them (each one is a section of
  // /payments, and a paid one says "Paid."). A check expects no new payment,
  // unless it says so.
  async function receipts() {
    const html = await (await ctx.request.get(SITE + '/payments')).text()
    return { entries: (html.match(/<h2>/g) || []).length - 1,   // the first heading is the page's
             paid: (html.match(/>Paid\./g) || []).length }
  }

  // The paid calls that reached the hostile program, as it counts them.
  async function hostileCalls() {
    return Number(await (await ctx.request.get(H + 'calls')).text())
  }

  async function check(label, fn, expect = { entries: 0, paid: 0 }) {
    running = `${name} ${label}`
    const victim0 = (await victimLog(page)).length
    const out0 = outside.requests.length
    const pay0 = await receipts()
    const calls0 = await hostileCalls()
    let ok = false
    let detail = ''
    try {
      [ok, detail] = await fn()
    } catch (e) {
      detail = 'error: ' + String(e).split('\n')[0]
    }
    const victim = (await victimLog(page)).slice(victim0)
    const out = outside.requests.slice(out0)
    const pay = await receipts()
    const calls = await hostileCalls()
    if (calls !== calls0) { ok = false; detail += ` the hostile program got ${calls - calls0} paid calls` }
    if (victim.length) { ok = false; detail += ` victim got [${victim.join(', ')}]` }
    if (out.length) { ok = false; detail += ` outside got [${out.join(', ')}]` }
    if (pay.entries - pay0.entries !== expect.entries || pay.paid - pay0.paid !== expect.paid) {
      ok = false
      detail += ` payments: ${pay.entries - pay0.entries} new, ${pay.paid - pay0.paid} paid`
    }
    results.push({ browser: name, check: label, ok, detail: detail.trim() })
    if (!ok) failed++
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${label}${detail ? ': ' + detail.trim() : ''}`)
  }

  const open = async (path) => page.goto(H + path)
  const click = async (selector = '#go') => {
    await page.click(selector, { noWaitAfter: true, timeout: 3000 })
    await sleep(1500)
  }
  const stays = (path) => page.url() === H + path

  await check('a page of the shop gets its own stylesheet', async () => {
    const r = await page.goto(LINKS.shop)
    const width = await page.evaluate(() => getComputedStyle(document.body).maxWidth)
    return [r.status() === 200 && width === '640px', `status ${r.status()}, max-width ${width}`]
  })

  await check('the shop sells the booklet through fc\'s review page', async () => {
    await page.goto(LINKS.shop)
    await Promise.all([page.waitForURL(/\/payments\/review\?id=\d+$/), page.click('button', { timeout: 5000 })])
    const box = await page.locator('button.primary').boundingBox()
    approveAt = { x: Math.round(box.x + box.width / 2), y: Math.round(box.y + box.height / 2) }
    await sleep(1200)
    await Promise.all([page.waitForURL(LINKS.shop + 'booklet', { timeout: 90000 }), page.click('button.primary')])
    const text = await page.locator('body').textContent()
    return [text.includes('Meetings are on Thursdays'), page.url()]
  }, { entries: 1, paid: 1 })

  await check('an image of the website loads', async () => {
    await open('img')
    await sleep(800)
    const width = await page.evaluate(() => document.getElementById('px').naturalWidth)
    return [width === 1, `natural width ${width}`]
  })

  await check('a form cannot post to another website', async () => {
    await open('form')
    await click()
    return [stays('form'), page.url()]
  })

  await check('a button formaction cannot post to another website', async () => {
    await open('formaction')
    await click()
    return [stays('formaction'), page.url()]
  })

  await check('a form cannot post to fc\'s own endpoints', async () => {
    let status = null
    const watch = (r) => { if (r.url().endsWith('/api/send')) status = r.status() }
    page.on('response', watch)
    await open('form-fc')
    await click()
    page.off('response', watch)
    return [status === null || status === 403, `url ${page.url()}, /api/send status ${status}`]
  })

  await check('a form cannot post outside', async () => {
    await open('form-out')
    await click()
    return [stays('form-out'), page.url()]
  })

  await check('a meta refresh does not move the page to another website', async () => {
    await open('refresh-site')
    await sleep(2000)
    return [stays('refresh-site'), page.url()]
  })

  await check('a meta refresh does not move the page outside', async () => {
    await open('refresh-out')
    await sleep(2000)
    return [stays('refresh-out'), page.url()]
  })

  await check('a base element changes no address', async () => {
    await open('base')
    await sleep(500)
    await click()
    return [page.url() === H + 'base-click', page.url()]
  })

  await check('a base target opens no other window', async () => {
    const before = popups.length
    await open('base-target')
    await click()
    return [popups.length === before, `${popups.length - before} new windows, url ${page.url()}`]
  })

  await check('styles load only from the website', async () => {
    await open('css')
    await sleep(1500)
    return [true, '']
  })

  await check('resource hints and images load only from the website', async () => {
    const before = outside.connections
    await open('hints')
    await sleep(2500)
    const made = outside.connections - before
    return [made === 0, `${made} connections to the outside server (a preconnect makes one with no request)`]
  })

  await check('a ping goes nowhere', async () => {
    await open('ping')
    await click()
    return [true, page.url()]
  })

  await check('a page embeds no frame, object or fc page', async () => {
    const requests = []
    const watch = (r) => { if (r.url().includes('/account/')) requests.push(r.url()) }
    page.on('request', watch)
    await open('embed')
    await sleep(1500)
    page.off('request', watch)
    return [requests.length === 0, requests.join(' ')]
  })

  await check('a page runs no script', async () => {
    await open('script')
    await sleep(1500)
    return [stays('script'), page.url()]
  })

  await check('a javascript: link runs nothing', async () => {
    await open('jslink')
    await click()
    return [stays('jslink'), page.url()]
  })

  await check('a fake approval page approves nothing', async () => {
    await open('fakepay')
    await click()
    const text = await page.locator('body').textContent()
    return [text.includes('posted'), page.url()]
  })

  await check('a double click from a website does not approve its payment', async () => {
    if (!approveAt) return [false, 'no position of the approve button']
    await open(`dbl?x=${approveAt.x}&y=${approveAt.y}`)
    await Promise.all([page.waitForURL(/\/payments\/review\?id=\d+$/), page.mouse.click(approveAt.x, approveAt.y)])
    await page.mouse.click(approveAt.x, approveAt.y)
    await sleep(300)
    const review = page.url()
    const pending = await page.locator('button.primary').count()
    await sleep(1200)
    const visible = await page.locator('button.primary').isVisible()
    reviewUrl = review
    await Promise.all([page.waitForURL(/\/link\//), page.click('text=decline')])
    return [pending === 1 && visible && review.includes('/payments/review'),
            `still pending: ${pending === 1}, approve visible after 1.2 s: ${visible}`]
  }, { entries: 1, paid: 0 })

  await check('an SVG page is refused', async () => {
    const r = await open('svg')
    return [r.status() === 502, `status ${r.status()}`]
  })

  await check('HTML sent as an image is not a page', async () => {
    const r = await open('fake.png')
    await sleep(800)
    let pwn = 0
    try { pwn = await page.locator('#pwn').count() } catch (e) { pwn = 0 }
    return [pwn === 0 && r.headers()['content-type'] === 'image/png', `content-type ${r.headers()['content-type']}`]
  })

  await check('the cookies and Refresh of a website are dropped', async () => {
    await open('setcookie')
    await sleep(1500)
    const cookies = await ctx.cookies(SITE)
    const evil = cookies.filter((c) => c.value === 'evil').map((c) => c.name)
    const r = await page.goto(LINKS.shop)
    return [evil.length === 0 && page.url() === LINKS.shop && r.status() === 200,
            `cookies with the website's value: [${evil}], still signed in: ${page.url() === LINKS.shop}`]
  })

  await check('a redirect cannot leave the website', async () => {
    const outward = (await open('redirect-out')).status()
    await open('redirect-up')
    const up = page.url()
    const encoded = (await open('redirect-enc')).status()
    return [outward === 502 && up.startsWith(H) && encoded === 502,
            `to another host ${outward}, up ${up}, encoded dots ${encoded}`]
  })

  // A frame on fc's own origin carries the session, so the page in it is the
  // real page. Playwright gives the parent page at a path that fc does not
  // have. Each frame check opens a new tab: WebKit with Playwright crashed
  // when the same tab went from fc to another origin.
  async function framed(src, selector) {
    const tab = await ctx.newPage()
    try {
      // The positive control: a page that allows frames shows in the same
      // kind of frame, and the check sees into it.
      await tab.route(SITE + '/__frameable', (route) => route.fulfill({
        contentType: 'text/html', body: '<!doctype html><p id="ok">frameable</p><header>h</header>' }))
      await tab.route(SITE + '/__control', (route) => route.fulfill({
        contentType: 'text/html', body: `<!doctype html><iframe width="600" height="400" src="/__frameable"></iframe>` }))
      await tab.goto(SITE + '/__control')
      await sleep(800)
      const control = tab.frames().find((f) => f !== tab.mainFrame())
      const seen = control ? await within(3000, control.locator(selector).count(), -1) : -1
      if (seen !== 1) return [false, `the check cannot see into a frame here (control: ${seen})`]
      await tab.route(SITE + '/__frame', (route) => route.fulfill({
        contentType: 'text/html', body: `<!doctype html><iframe width="600" height="400" src="${src}"></iframe>` }))
      const target = tab.waitForResponse((r) => r.url() === new URL(src, SITE).href, { timeout: 10000 })
      await tab.goto(SITE + '/__frame')
      const status = (await target).status()
      await sleep(1500)
      const frame = tab.frames().find((f) => f !== tab.mainFrame())
      // -1: the frame has no document, as when the browser refused the page.
      const shown = frame ? await within(3000, frame.locator(selector).count(), -1) : -1
      return [status === 200 && shown <= 0, `the framed page gave ${status}, elements shown: ${shown} (control: 1)`]
    } finally {
      await tab.close()
    }
  }

  await check('a page of a website cannot be framed', async () => framed(H + 'ok', '#ok'))
  await check('fc\'s payment review cannot be framed', async () =>
    reviewUrl ? framed(reviewUrl, 'header') : [false, 'no review page to frame'])

  const victim = (await victimLog(page)).slice(victimBefore)
  const out = outside.requests.slice(outBefore)
  console.log(`${name}: the victim got ${victim.length} requests, the outside server ${out.length}`)
  await browser.close()
}

for (const name of BROWSERS) {
  try {
    await engine(name)
  } catch (e) {
    failed++
    console.log(`FAIL ${name}: ${String(e).split('\n')[0]}`)
  }
}
server.close()
save()
console.log(`${results.length - results.filter((r) => !r.ok).length} of ${results.length} checks passed`)
process.exit(failed ? 1 : 0)
