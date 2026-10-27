/* Real Chromium activation against one private, disposable employee fixture.
 * Never prints credentials, response bodies, browser logs, or secret URLs.
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
let phase = 'fixture';
const checks = [];
const deadline = Date.now() + 90000;

function remaining() {
  const value = deadline - Date.now();
  if (value <= 0) throw new Error('time_budget_exhausted');
  return value;
}

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
  const issuer = new URL(config.issuer);
  if (config.purpose !== 'isolated-employee-activation-browser-smoke' || issuer.protocol !== 'http:' ||
      issuer.hostname !== '127.0.0.1' || issuer.username || issuer.password || issuer.pathname !== '/' ||
      issuer.search || issuer.hash || typeof config.activation_code !== 'string' || typeof config.password !== 'string') {
    throw new Error('isolated_loopback_fixture_required');
  }
  const { chromium } = require(process.env.STAGE_PLAYWRIGHT_MODULE || 'playwright');
  const browser = await chromium.launch({
    headless: true, executablePath: process.env.STAGE_CHROMIUM_EXECUTABLE || undefined,
    timeout: Math.min(10000, remaining()), args: process.getuid() === 0 ? ['--no-sandbox'] : [],
  });
  const timer = setTimeout(() => { browser.close().catch(() => {}); }, remaining());
  try {
    const context = await browser.newContext();
    context.setDefaultTimeout(10000);
    context.setDefaultNavigationTimeout(10000);
    const page = await context.newPage();
    const assertPrivate = async response => {
      const headers = await response.allHeaders();
      if (headers['cache-control'] !== 'no-store' || headers['x-frame-options'] !== 'DENY' ||
          headers['referrer-policy'] !== 'same-origin' ||
          headers['content-security-policy'] !== "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'") {
        throw new Error('private_issuer_document_required');
      }
    };
    const post = async (route, action) => {
      const [response] = await Promise.all([
        page.waitForResponse(value => value.url() === config.issuer + route && value.request().method() === 'POST'),
        action(),
      ]);
      if (response.status() !== 200 || (await response.request().allHeaders()).origin !== config.issuer) {
        throw new Error('same_origin_form_submission_required');
      }
      await assertPrivate(response);
      return response;
    };
    const fillInitial = async selectedPage => {
      const form = selectedPage.locator('form[method=post]');
      await form.locator('[name=activation_ref]').fill(config.activation_ref);
      await form.locator('[name=code]').fill(config.activation_code);
      await form.locator('[name=password]').fill(config.password);
    };

    phase = 'activation_entry';
    const entry = await page.goto(config.issuer + '/oauth/activate');
    if (!entry || entry.status() !== 200) throw new Error('entry_failed');
    await assertPrivate(entry);
    const initialCookies = await context.cookies();
    if (initialCookies.length !== 1 || initialCookies[0].name !== 'stage_activation' ||
        !initialCookies[0].httpOnly || initialCookies[0].sameSite !== 'Lax') throw new Error('dedicated_cookie_required');
    checks.push('issuer_entry_private_and_dedicated_cookie');

    phase = 'activation_secrets_never_in_query';
    const rejectedQuery = await page.request.get(config.issuer + '/oauth/activate?code=synthetic-query-only');
    if (rejectedQuery.status() !== 400 || (await rejectedQuery.text()).includes('synthetic-query-only')) {
      throw new Error('query_secret_accepted');
    }
    checks.push('query_secrets_rejected');

    phase = 'password_then_enrollment';
    await fillInitial(page);
    const begun = await post('/oauth/activate', () => page.getByRole('button', {
      name: '设置密码并绑定认证器', exact: true,
    }).click());
    const secret = await page.locator('#enrollment-secret').innerText();
    if (!secret || (await begun.text()).includes(config.activation_code) || (await begun.text()).includes(config.password)) {
      throw new Error('enrollment_secret_or_initial_secret_reflection');
    }
    checks.push('real_browser_password_consumes_activation_code');
    const csrf = await page.locator('[name=csrf_token]').inputValue();
    const statusUrl = config.issuer + '/oauth/activate/status?' + new URLSearchParams({ activation_ref: config.activation_ref });
    const recovered = await page.goto(statusUrl);
    if (!recovered || recovered.status() !== 200 || await page.locator('#enrollment-secret').innerText() !== secret) {
      throw new Error('same_browser_progress_failed');
    }
    await assertPrivate(recovered);
    checks.push('same_browser_status_resumes_enrollment');

    phase = 'different_browser_has_no_enrollment_secret';
    const stranger = await browser.newContext();
    const other = await stranger.newPage();
    const unavailable = await other.goto(statusUrl);
    if (!unavailable || unavailable.status() !== 400 || await other.locator('#enrollment-secret').count()) {
      throw new Error('unbound_browser_read_enrollment');
    }
    await other.goto(config.issuer + '/oauth/activate');
    await fillInitial(other);
    await other.getByRole('button', { name: '设置密码并绑定认证器', exact: true }).click();
    if (await other.locator('#enrollment-secret').count() || !(await other.locator('[role=alert]').count())) {
      throw new Error('consumed_code_reopened_enrollment');
    }
    checks.push('different_browser_and_used_code_cannot_reopen_enrollment');

    phase = 'bad_otp_preserves_enrollment';
    const bad = await page.request.post(config.issuer + '/oauth/activate/complete', {
      form: { activation_ref: config.activation_ref, csrf_token: csrf, otp: 'not-a-code' },
      headers: { Origin: config.issuer },
    });
    if (bad.status() !== 400 || !(await bad.text()).includes(secret)) throw new Error('factor_retry_failed');
    checks.push('invalid_totp_does_not_activate');

    phase = 'real_totp_completion';
    const python = process.env.STAGE_SMOKE_PYTHON || path.resolve(__dirname, '../.venv/bin/python');
    const otp = execFileSync(python, ['-c', 'import pyotp,sys;print(pyotp.TOTP(sys.stdin.read()).now())'], {
      encoding: 'utf8', input: secret, timeout: Math.min(5000, remaining()), stdio: ['pipe', 'pipe', 'pipe'],
    }).trim();
    await page.locator('[name=otp]').fill(otp);
    const completed = await post('/oauth/activate/complete', () => page.getByRole('button', {
      name: '完成激活', exact: true,
    }).click());
    const codes = await page.locator('#recovery-codes code').allTextContents();
    if (codes.length !== 10 || (await completed.text()).includes(secret)) throw new Error('one_time_codes_required');
    checks.push('real_totp_activates_and_displays_ten_recovery_codes');
    const cookies = await context.cookies();
    if (cookies.some(cookie => cookie.name !== 'stage_activation') || completed.headers().location) {
      throw new Error('activation_created_login_state');
    }
    checks.push('activation_does_not_create_browser_login');

    phase = 'completed_status_has_no_recovery_codes';
    await page.goto(statusUrl);
    if (await page.locator('#recovery-codes').count() || !(await page.getByRole('heading', { name: '账号激活完成' }).count())) {
      throw new Error('completed_status_replays_recovery_codes');
    }
    const replay = await page.request.post(config.issuer + '/oauth/activate/complete', {
      form: { activation_ref: config.activation_ref, csrf_token: csrf, otp }, headers: { Origin: config.issuer },
    });
    const replayBody = await replay.text();
    if (replay.status() !== 200 || codes.some(code => replayBody.includes(code))) throw new Error('completion_replays_recovery_codes');
    checks.push('status_refresh_and_post_replay_never_repeat_recovery_codes');
    console.log(JSON.stringify({ browser: 'Chromium', passed: checks.length, checks }));
  } finally {
    clearTimeout(timer);
    await browser.close();
  }
})().catch(error => {
  console.error(JSON.stringify({ error: 'employee_activation_browser_smoke_failed', phase, type: error.name }));
  process.exitCode = 1;
});
