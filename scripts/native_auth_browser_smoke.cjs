/* Optional real Chromium smoke. Uses a private loopback fixture, never prints secrets.
 * STAGE_PLAYWRIGHT_MODULE may select an installed Playwright module; no browser
 * dependency is added to the production Python service.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const http = require('http');
const { execFileSync } = require('child_process');
let phase = 'fixture';

(async () => {
  const file = path.resolve(process.argv[2] || '');
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  let config;
  try {
    const info = fs.fstatSync(descriptor);
    if (!info.isFile() || (info.mode & 0o077) || info.size > 32768 || info.uid !== process.getuid()) {
      throw new Error('private_fixture_required');
    }
    config = JSON.parse(fs.readFileSync(descriptor, 'utf8'));
  } finally { fs.closeSync(descriptor); }
  for (const address of [config.issuer, config.redirect_uri]) {
    const url = new URL(address);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password) {
      throw new Error('loopback_fixture_required');
    }
  }
  // Each smoke gets a new time step, including after an earlier failed callback.
  phase = 'fresh_totp_step';
  await new Promise(resolve => setTimeout(resolve, 30000 - Date.now() % 30000 + 200));
  const verifier = crypto.randomBytes(48).toString('base64url');
  const state = crypto.randomBytes(24).toString('base64url');
  const parameters = new URLSearchParams({
    client_id: config.client_id, redirect_uri: config.redirect_uri, response_type: 'code', scope: 'openid',
    state, nonce: crypto.randomBytes(24).toString('base64url'),
    code_challenge: crypto.createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256',
  });
  let callback;
  let callbackReferrer;
  const callbackAddress = new URL(config.redirect_uri);
  const listener = http.createServer((request, response) => {
    const incoming = new URL(request.url, config.redirect_uri);
    if (request.method !== 'GET' || incoming.pathname !== callbackAddress.pathname || incoming.searchParams.get('state') !== state) {
      response.writeHead(404); response.end(); return;
    }
    callback = incoming;
    callbackReferrer = request.headers.referer;
    response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' });
    response.end('<p>Browser callback verified</p>');
  });
  await new Promise((resolve, reject) => {
    listener.once('error', reject);
    listener.listen(Number(callbackAddress.port), '127.0.0.1', resolve);
  });
  let browser;
  try {
  const { chromium } = require(process.env.STAGE_PLAYWRIGHT_MODULE || 'playwright');
  browser = await chromium.launch({
    headless: true, executablePath: process.env.STAGE_CHROMIUM_EXECUTABLE || undefined,
    args: process.getuid() === 0 ? ['--no-sandbox'] : [],
  });
    const page = await browser.newPage();
    phase = 'password_form';
    await page.goto(config.issuer + '/oauth/authorize?' + parameters);
    await page.locator('[name=username]').fill(config.username);
    await page.locator('[name=password]').fill(config.password);
    await page.getByRole('button', { name: '继续', exact: true }).click();
    await page.locator('[name=code]').waitFor({ timeout: 10000 });
    phase = 'totp_and_browser_redirect';
    const python = process.env.STAGE_SMOKE_PYTHON || path.resolve(__dirname, '../.venv/bin/python');
    const otp = execFileSync(python, ['-c',
      'import json,os,pyotp;from pathlib import Path;print(pyotp.TOTP(json.loads(Path(os.environ["STAGE_NATIVE_SMOKE_CREDENTIALS"]).read_text())["otp_secret"]).now())',
    ], { encoding: 'utf8', env: { ...process.env, STAGE_NATIVE_SMOKE_CREDENTIALS: file } }).trim();
    await page.locator('[name=code]').fill(otp);
    await page.getByRole('button', { name: '验证', exact: true }).click();
    await page.waitForURL(url => url.href.startsWith(config.redirect_uri), { timeout: 10000 });
    if (!callback || callback.searchParams.get('state') !== state || callbackReferrer) throw new Error('callback');
    config.last_totp_step = Math.floor(Date.now() / 30000);
    fs.writeFileSync(file, JSON.stringify(config, null, 2) + '\n');
    phase = 'pkce_exchange';
    const exchanged = await page.request.post(config.issuer + '/oauth/token', { form: {
      grant_type: 'authorization_code', client_id: config.client_id, redirect_uri: config.redirect_uri,
      code: callback.searchParams.get('code'), code_verifier: verifier,
    } });
    if (exchanged.status() !== 200) throw new Error('exchange');
    const tokens = await exchanged.json();
    phase = 'business_identity';
    const me = await page.request.get(config.issuer + '/api/v1/identity/me', {
      params: { profile_ref: config.profile_ref, profile_version: String(config.profile_version) },
      headers: { Authorization: 'Bearer ' + tokens.access_token },
    });
    if (me.status() !== 200 || (await me.json()).principal_ref !== config.principal_ref) throw new Error('identity');
    phase = 'revoke';
    const revoke = await page.request.post(config.issuer + '/oauth/revoke', { form: {
      client_id: config.client_id, token: tokens.refresh_token,
    } });
    if (revoke.status() !== 200) throw new Error('revoke');
    console.log(JSON.stringify({ browser: 'Chromium', passed: 6, checks: [
      'password_form', 'totp_form', 'registered_browser_callback_without_referrer',
      'pkce_exchange', 'same_person_business_identity', 'session_revoked',
    ] }));
  } finally {
    try { if (browser) await browser.close(); }
    finally { await new Promise(resolve => listener.close(resolve)); }
  }
})().catch(error => {
  console.error(JSON.stringify({ error: 'native_browser_smoke_failed', phase, type: error.name }));
  process.exitCode = 1;
});
