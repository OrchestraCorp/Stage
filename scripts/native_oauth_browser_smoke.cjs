/* Isolated real-browser OIDC acceptance. The fixture and all credentials stay private. */
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
  for (const address of [config.issuer, config.redirect_uri, config.post_logout_redirect_uri]) {
    const url = new URL(address);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password) {
      throw new Error('loopback_fixture_required');
    }
  }
  const checks = [];
  const callbacks = new Map();
  const callbackAddress = new URL(config.redirect_uri);
  const logoutAddress = new URL(config.post_logout_redirect_uri);
  let logoutInitiation;
  function html(value) {
    return String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  }
  const listener = http.createServer((request, response) => {
    const incoming = new URL(request.url, config.redirect_uri);
    if (incoming.pathname === '/rp-logout' && request.method === 'GET' && logoutInitiation) {
      response.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; form-action " + config.issuer,
      });
      const controls = Object.entries(logoutInitiation).map(([key, value]) =>
        '<input type="hidden" name="' + html(key) + '" value="' + html(value) + '">').join('');
      response.end('<form method="post" action="' + html(config.issuer + '/oauth/logout') + '">' + controls +
        '<button type="submit">Start logout</button></form>');
      return;
    }
    if (![callbackAddress.pathname, logoutAddress.pathname].includes(incoming.pathname)) {
      response.writeHead(404); response.end(); return;
    }
    let body = '';
    request.on('data', chunk => { body += chunk; if (body.length > 8192) request.destroy(); });
    request.on('end', () => {
      const values = request.method === 'POST' ? new URLSearchParams(body) : incoming.searchParams;
      callbacks.set(values.get('state'), { values, method: request.method, referrer: request.headers.referer });
      response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' });
      response.end('<p>Registered callback received</p>');
    });
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
    function authorization(extra = {}) {
      const verifier = crypto.randomBytes(48).toString('base64url');
      const state = crypto.randomBytes(24).toString('base64url');
      const parameters = new URLSearchParams({
        client_id: config.client_id, redirect_uri: config.redirect_uri, response_type: 'code', scope: 'openid',
        state, nonce: crypto.randomBytes(24).toString('base64url'),
        code_challenge: crypto.createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256',
        ...extra,
      });
      const flow = { verifier, state, url: config.issuer + '/oauth/authorize?' + parameters };
      return flow;
    }
    async function received(flow, method = 'GET') {
      await page.waitForURL(url => url.href.startsWith(config.redirect_uri), { timeout: 10000 });
      const callback = callbacks.get(flow.state);
      if (!callback || callback.method !== method || callback.referrer || callback.values.get('iss') !== config.issuer || !callback.values.get('code')) {
        throw new Error('registered_callback_failed');
      }
      const result = await page.request.post(config.issuer + '/oauth/token', { form: {
        grant_type: 'authorization_code', client_id: config.client_id, redirect_uri: config.redirect_uri,
        code: callback.values.get('code'), code_verifier: flow.verifier,
      } });
      if (result.status() !== 200) throw new Error('token_exchange_failed');
      return await result.json();
    }
    function claims(token) { return JSON.parse(Buffer.from(token.split('.')[1], 'base64url')); }
    phase = 'fresh_totp_step';
    await new Promise(resolve => setTimeout(resolve, 30000 - Date.now() % 30000 + 200));
    phase = 'initial_password_totp_login';
    const initialFlow = authorization();
    await page.goto(initialFlow.url);
    await page.locator('[name=username]').fill(config.username);
    await page.locator('[name=password]').fill(config.password);
    await page.getByRole('button', { name: '继续', exact: true }).click();
    await page.locator('[name=code]').waitFor({ timeout: 10000 });
    const python = process.env.STAGE_SMOKE_PYTHON || path.resolve(__dirname, '../.venv/bin/python');
    const otp = execFileSync(python, ['-c',
      'import json,os,pyotp;from pathlib import Path;print(pyotp.TOTP(json.loads(Path(os.environ["STAGE_NATIVE_SMOKE_CREDENTIALS"]).read_text())["otp_secret"]).now())',
    ], { encoding: 'utf8', env: { ...process.env, STAGE_NATIVE_SMOKE_CREDENTIALS: file } }).trim();
    await page.locator('[name=code]').fill(otp);
    await page.getByRole('button', { name: '验证', exact: true }).click();
    const initial = await received(initialFlow);
    checks.push('password_totp_registered_query_callback_without_referrer');
    const cookie = (await page.context().cookies()).find(value => value.name === 'stage_sso');
    if (!cookie || !cookie.httpOnly || cookie.sameSite !== 'Lax') throw new Error('sso_cookie_boundary');
    checks.push('independent_http_only_sso_cookie');
    phase = 'silent_sso';
    const silentFlow = authorization({ prompt: 'none' });
    await page.goto(silentFlow.url);
    const silent = await received(silentFlow);
    if (claims(silent.id_token).auth_time !== claims(initial.id_token).auth_time) throw new Error('sso_auth_time_changed');
    checks.push('top_level_prompt_none_reuses_original_authentication_time');
    phase = 'prompt_login';
    await page.goto(authorization({ prompt: 'login' }).url);
    await page.locator('[name=password]').waitFor({ timeout: 10000 });
    checks.push('prompt_login_requires_new_credentials');
    phase = 'form_post_callback';
    const postedFlow = authorization({ prompt: 'none', response_mode: 'form_post' });
    await page.goto(postedFlow.url);
    await received(postedFlow, 'POST');
    checks.push('automatic_form_post_survives_csp_without_referrer');
    phase = 'explicit_consent';
    const consentFlow = authorization({ prompt: 'consent' });
    await page.goto(consentFlow.url);
    await page.getByRole('button', { name: '允许', exact: true }).click();
    await received(consentFlow);
    checks.push('explicit_consent_post_reaches_registered_callback');
    phase = 'logout_confirmation';
    const logoutState = crypto.randomBytes(24).toString('base64url');
    const logoutParameters = new URLSearchParams({
      id_token_hint: initial.id_token, post_logout_redirect_uri: config.post_logout_redirect_uri, state: logoutState,
    });
    await page.goto(config.issuer + '/oauth/logout?' + logoutParameters);
    await page.getByRole('button', { name: '退出登录', exact: true }).waitFor({ timeout: 10000 });
    const before = await page.request.get(config.issuer + '/oauth/userinfo', { headers: { Authorization: 'Bearer ' + initial.access_token } });
    if (before.status() !== 200) throw new Error('logout_get_changed_session');
    checks.push('logout_get_only_shows_confirmation');
    phase = 'cross_site_rp_page';
    logoutInitiation = Object.fromEntries(logoutParameters);
    await page.goto('http://localhost:' + callbackAddress.port + '/rp-logout');
    const relayResponse = page.waitForResponse(response =>
      response.url() === config.issuer + '/oauth/logout' && response.request().method() === 'POST');
    phase = 'cross_site_rp_form_submit';
    await page.getByRole('button', { name: 'Start logout', exact: true }).click();
    phase = 'cross_site_logout_relay_response';
    const relayed = await relayResponse;
    // The relay immediately navigates again; Chromium may discard its response
    // body before Playwright can request it. Verify the unique relay headers
    // and the final browser-bound confirmation instead of fetching that body.
    if (relayed.status() !== 200 || !relayed.headers()['content-security-policy'].includes("script-src 'nonce-") ||
        relayed.headers()['referrer-policy'] !== 'same-origin') throw new Error('cross_site_logout_relay_missing');
    phase = 'cross_site_logout_confirmation';
    await page.getByRole('button', { name: '退出登录', exact: true }).waitFor({ timeout: 10000 });
    phase = 'cross_site_logout_unchanged_session';
    const unchanged = await page.request.get(config.issuer + '/oauth/userinfo', { headers: { Authorization: 'Bearer ' + initial.access_token } });
    if (unchanged.status() !== 200) throw new Error('logout_post_initiation_changed_session');
    checks.push('cross_site_post_logout_relay_binds_samesite_cookie_before_confirmation');
    await page.getByRole('button', { name: '退出登录', exact: true }).click();
    await page.waitForURL(url => url.href.startsWith(config.post_logout_redirect_uri), { timeout: 10000 });
    const afterLogout = callbacks.get(logoutState);
    if (!afterLogout || afterLogout.referrer || afterLogout.method !== 'GET') throw new Error('logout_callback');
    checks.push('logout_confirmation_reaches_registered_return_without_referrer');
    const after = await page.request.get(config.issuer + '/oauth/userinfo', { headers: { Authorization: 'Bearer ' + initial.access_token } });
    if (after.status() !== 401) throw new Error('logout_access_still_valid');
    const refreshed = await page.request.post(config.issuer + '/oauth/token', { form: {
      grant_type: 'refresh_token', client_id: config.client_id, refresh_token: silent.refresh_token,
    } });
    if (refreshed.status() !== 400) throw new Error('logout_refresh_still_valid');
    checks.push('logout_invalidates_all_browser_token_families');
    phase = 'silent_after_logout';
    const rejectedFlow = authorization({ prompt: 'none' });
    await page.goto(rejectedFlow.url);
    await page.waitForURL(url => url.href.startsWith(config.redirect_uri), { timeout: 10000 });
    const rejected = callbacks.get(rejectedFlow.state);
    if (!rejected || rejected.values.get('error') !== 'login_required' || rejected.values.has('code')) {
      throw new Error('logout_sso_still_valid');
    }
    checks.push('logout_invalidates_browser_sso');
    console.log(JSON.stringify({ browser: 'Chromium', passed: checks.length, checks }));
  } finally {
    try { if (browser) await browser.close(); }
    finally { await new Promise(resolve => listener.close(resolve)); }
  }
})().catch(error => {
  console.error(JSON.stringify({ error: 'native_oauth_browser_smoke_failed', phase, type: error.name }));
  process.exitCode = 1;
});
