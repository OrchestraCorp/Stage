/* Opt-in Chromium acceptance for an isolated native-account rebind.
 * Reads and updates only an explicitly marked private test fixture. No credentials,
 * page bodies, callbacks, or browser logs are printed. Total budget: 115 seconds.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const http = require('http');
const { execFileSync } = require('child_process');
let phase = 'fixture';
const checks = [];
const deadline = Date.now() + 115000;

function remaining() {
  const milliseconds = deadline - Date.now();
  if (milliseconds <= 0) throw new Error('time_budget_exhausted');
  return milliseconds;
}
function privateFile(descriptor) {
  const info = fs.fstatSync(descriptor);
  if (!info.isFile() || (info.mode & 0o077) || info.size > 32768 || info.uid !== process.getuid()) {
    throw new Error('private_fixture_required');
  }
}

(async () => {
  const file = path.resolve(process.argv[2] || '');
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  let config;
  try { privateFile(descriptor); config = JSON.parse(fs.readFileSync(descriptor, 'utf8')); }
  finally { fs.closeSync(descriptor); }
  if (config.purpose !== 'isolated-native-recovery-browser-smoke' || !config.otp_secret) {
    throw new Error('isolated_bound_fixture_required');
  }
  for (const address of [config.issuer, config.redirect_uri]) {
    const url = new URL(address);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password) {
      throw new Error('loopback_fixture_required');
    }
  }
  const save = () => {
    const output = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW);
    try {
      privateFile(output);
      const contents = Buffer.from(JSON.stringify(config, null, 2) + '\n');
      fs.writeFileSync(output, contents);
      fs.ftruncateSync(output, contents.length);
      fs.fsyncSync(output);
    } finally { fs.closeSync(output); }
  };
  const python = process.env.STAGE_SMOKE_PYTHON || path.resolve(__dirname, '../.venv/bin/python');
  const otp = secret => execFileSync(python, ['-c',
    'import pyotp,sys;print(pyotp.TOTP(sys.stdin.read()).now())',
  ], { encoding: 'utf8', input: secret, timeout: Math.min(5000, remaining()), stdio: ['pipe', 'pipe', 'pipe'] }).trim();
  const freshStep = async () => {
    const delay = Math.min(30000, 30000 - Date.now() % 30000 + 100);
    if (delay + 1000 >= remaining()) throw new Error('time_budget_exhausted');
    await new Promise(resolve => setTimeout(resolve, delay));
  };
  let callback;
  let expectedState;
  let callbackReferrer;
  const callbackAddress = new URL(config.redirect_uri);
  const listener = http.createServer((request, response) => {
    const incoming = new URL(request.url, config.redirect_uri);
    if (request.method !== 'GET' || incoming.pathname !== callbackAddress.pathname ||
        incoming.searchParams.get('state') !== expectedState) {
      response.writeHead(404); response.end(); return;
    }
    callback = incoming;
    callbackReferrer = request.headers.referer;
    response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' });
    response.end('<p>Isolated browser callback verified</p>');
  });
  await new Promise((resolve, reject) => {
    listener.once('error', reject);
    listener.listen(Number(callbackAddress.port), '127.0.0.1', resolve);
  });
  let browser;
  let timer;
  try {
    const { chromium } = require(process.env.STAGE_PLAYWRIGHT_MODULE || 'playwright');
    browser = await chromium.launch({
      headless: true, executablePath: process.env.STAGE_CHROMIUM_EXECUTABLE || undefined,
      timeout: Math.min(10000, remaining()), args: process.getuid() === 0 ? ['--no-sandbox'] : [],
    });
    // Closing the browser interrupts pending waits; finally closes its listener too.
    timer = setTimeout(() => { browser.close().catch(() => {}); }, remaining());
    const context = async () => {
      const value = await browser.newContext();
      value.setDefaultTimeout(Math.min(10000, remaining()));
      value.setDefaultNavigationTimeout(Math.min(10000, remaining()));
      return value;
    };
    const password = async browserContext => {
      remaining();
      const verifier = crypto.randomBytes(48).toString('base64url');
      expectedState = crypto.randomBytes(24).toString('base64url');
      callback = undefined;
      callbackReferrer = undefined;
      const query = new URLSearchParams({
        client_id: config.client_id, redirect_uri: config.redirect_uri, response_type: 'code', scope: 'openid',
        state: expectedState, nonce: crypto.randomBytes(24).toString('base64url'),
        code_challenge: crypto.createHash('sha256').update(verifier).digest('base64url'), code_challenge_method: 'S256',
      });
      const page = await browserContext.newPage();
      await page.goto(config.issuer + '/oauth/authorize?' + query);
      await page.locator('[name=username]').fill(config.username);
      await page.locator('[name=password]').fill(config.password);
      await page.getByRole('button', { name: '继续', exact: true }).click();
      await page.locator('[name=code]').waitFor();
      return { page, verifier, state: expectedState };
    };
    const exchange = async flow => {
      await flow.page.waitForURL(url => url.href.startsWith(config.redirect_uri));
      if (!callback || callback.searchParams.get('state') !== flow.state || callbackReferrer) {
        throw new Error('registered_callback_without_referrer_required');
      }
      const response = await flow.page.request.post(config.issuer + '/oauth/token', { form: {
        grant_type: 'authorization_code', client_id: config.client_id, redirect_uri: config.redirect_uri,
        code: callback.searchParams.get('code'), code_verifier: flow.verifier,
      }, timeout: Math.min(10000, remaining()) });
      if (response.status() !== 200) throw new Error('pkce_exchange_failed');
      return response.json();
    };
    const identity = async (page, accessToken, status) => {
      const response = await page.request.get(config.issuer + '/api/v1/identity/me', {
        params: { profile_ref: config.profile_ref, profile_version: String(config.profile_version) },
        headers: { Authorization: 'Bearer ' + accessToken }, timeout: Math.min(10000, remaining()),
      });
      if (response.status() !== status || (status === 200 && (await response.json()).principal_ref !== config.principal_ref)) {
        throw new Error('business_identity_check_failed');
      }
    };
    const recover = async (flow, code) => {
      await flow.page.getByText('无法使用认证器', { exact: true }).click();
      await flow.page.locator('[name=recovery_code]').fill(code);
      const [response] = await Promise.all([
        flow.page.waitForResponse(value => value.url() === config.issuer + '/oauth/login' && value.request().method() === 'POST'),
        flow.page.getByRole('button', { name: '恢复并重新绑定', exact: true }).click(),
      ]);
      return response;
    };

    phase = 'fresh_login_totp_step';
    await freshStep();
    phase = 'initial_password_form';
    const initial = await password(await context());
    phase = 'initial_totp_login';
    await initial.page.locator('[name=code]').fill(otp(config.otp_secret));
    await initial.page.getByRole('button', { name: '验证', exact: true }).click();
    const firstTokens = await exchange(initial);
    await identity(initial.page, firstTokens.access_token, 200);
    checks.push('real_browser_password_totp', 'initial_registered_callback_and_pkce', 'initial_same_person_identity');

    phase = 'fresh_reset_totp_step';
    await freshStep();
    phase = 'reset_returns_one_time_rebind_code';
    const reset = await initial.page.request.post(config.issuer + '/api/v1/account/totp/reset', {
      data: { password: config.password, otp: otp(config.otp_secret) },
      headers: { Authorization: 'Bearer ' + firstTokens.access_token }, timeout: Math.min(10000, remaining()),
    });
    if (reset.status() !== 200 || reset.headers()['cache-control'] !== 'no-store') throw new Error('reset_failed');
    const resetBody = await reset.json();
    if (typeof resetBody.enrollment_code !== 'string' || resetBody.enrollment_code.length < 20) {
      throw new Error('rebind_code_required');
    }
    const enrollmentCode = resetBody.enrollment_code;
    config.pending_enrollment_code = enrollmentCode;
    save();
    checks.push('reset_returns_noncacheable_one_time_rebind_code');
    phase = 'old_session_rejected';
    await identity(initial.page, firstTokens.access_token, 401);
    const refresh = await initial.page.request.post(config.issuer + '/oauth/token', { form: {
      grant_type: 'refresh_token', client_id: config.client_id, refresh_token: firstTokens.refresh_token,
    }, timeout: Math.min(10000, remaining()) });
    if (refresh.status() !== 400) throw new Error('old_refresh_not_revoked');
    checks.push('old_business_token_rejected', 'old_refresh_rejected');

    phase = 'fresh_browser_password_cannot_enroll';
    const rebound = await password(await context());
    if (await rebound.page.locator('#enrollment-secret').count()) throw new Error('password_only_enrollment');
    checks.push('fresh_browser_password_alone_has_no_enrollment_secret');
    phase = 'rebind_code_then_new_totp';
    if ((await recover(rebound, enrollmentCode)).status() !== 200) throw new Error('rebind_code_rejected');
    const newSecret = await rebound.page.locator('#enrollment-secret').innerText();
    if (!newSecret || newSecret === config.otp_secret) throw new Error('new_factor_required');
    await rebound.page.locator('[name=code]').fill(otp(newSecret));
    await rebound.page.getByRole('button', { name: '验证', exact: true }).click();
    await rebound.page.locator('#recovery-codes').waitFor();
    const recoveryCodes = await rebound.page.locator('#recovery-codes code').allTextContents();
    if (recoveryCodes.length !== 10) throw new Error('recovery_codes_required');
    config.otp_secret = newSecret;
    config.recovery_codes = recoveryCodes;
    config.last_totp_step = Math.floor(Date.now() / 30000);
    delete config.pending_enrollment_code;
    save();
    checks.push('one_time_rebind_code_unlocks_enrollment', 'new_totp_verified', 'replacement_recovery_codes_saved_privately');
    phase = 'rebound_registered_callback';
    await rebound.page.getByRole('button', { name: '返回应用', exact: true }).click();
    const reboundTokens = await exchange(rebound);
    await identity(rebound.page, reboundTokens.access_token, 200);
    checks.push('rebound_registered_callback_and_pkce', 'rebound_same_person_business_identity');

    phase = 'used_rebind_code_rejected';
    const replay = await password(await context());
    const rejected = await recover(replay, enrollmentCode);
    if (rejected.status() !== 400 || await replay.page.locator('#enrollment-secret').count()) {
      throw new Error('used_rebind_code_accepted');
    }
    checks.push('used_rebind_code_cannot_reopen_enrollment');
    phase = 'final_session_revoked';
    const revoked = await rebound.page.request.post(config.issuer + '/oauth/revoke', { form: {
      client_id: config.client_id, token: reboundTokens.refresh_token,
    }, timeout: Math.min(10000, remaining()) });
    if (revoked.status() !== 200) throw new Error('final_revocation_failed');
    await identity(rebound.page, reboundTokens.access_token, 401);
    checks.push('final_session_revoked');
    console.log(JSON.stringify({ browser: 'Chromium', passed: checks.length, checks }));
  } finally {
    clearTimeout(timer);
    try { if (browser) await browser.close(); }
    finally { await new Promise(resolve => listener.close(resolve)); }
  }
})().catch(error => {
  console.error(JSON.stringify({ error: 'native_recovery_browser_smoke_failed', phase, type: error.name }));
  process.exitCode = 1;
});
