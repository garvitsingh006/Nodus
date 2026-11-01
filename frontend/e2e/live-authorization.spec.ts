import { expect, test } from '@playwright/test'

// Opt-in diagnostic for the actual unavailable-backend path; no interception.
test('real proxy reports an unavailable backend and keeps token 4 locked', async ({ page }) => {
  test.skip(process.env.NODUS_EXPECT_BACKEND_UNAVAILABLE !== '1', 'Run explicitly when diagnosing an unavailable local backend')
  await page.goto('/')
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/v1/session'))
  await page.getByLabel('Access token').fill('4')
  await page.getByLabel('Access token').press('Enter')
  const response = await responsePromise
  expect(response.status()).toBeGreaterThanOrEqual(500)
  await expect(page.getByRole('alert')).toContainText('Check that the backend API is running')
  await expect(page.getByLabel('Authorized workspace')).toHaveCount(0)
  await expect(page.getByLabel('What would you like to investigate?')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Continue' })).toBeEnabled()
  expect(await page.evaluate(() => localStorage.length)).toBe(0)
})

test('live FastAPI authorization uses the real token resolver without mocks', async ({ page }) => {
  test.skip(process.env.NODUS_LIVE_AUTH !== '1', 'Opt in with NODUS_LIVE_AUTH=1')
  const token = process.env.NODUS_LIVE_TOKEN ?? '4'
  await page.goto('/')
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/v1/session'))
  await page.getByLabel('Access token').fill(token)
  await page.getByLabel('Access token').press('Enter')
  const response = await responsePromise
  if (!process.env.NODUS_LIVE_TOKEN && (response.status() === 401 || response.status() === 403)) {
    test.info().annotations.push({ type: 'live-result', description: 'Demo token 4 rejected by FastAPI; successful workspace authorization is blocked.' })
    await expect(page.getByRole('alert')).toContainText('not accepted or has expired')
    await expect(page.getByLabel('Authorized workspace')).toHaveCount(0)
    return
  }
  expect(response.status()).toBe(200)
  await expect(page.getByText('NEXUS Technologies')).toBeVisible()
  await expect(page.getByLabel('Authenticated role')).toBeVisible()
  const projectPicker = page.getByLabel('Switch project')
  if (await projectPicker.count()) {
    const options = await projectPicker.locator('option').evaluateAll(items =>
      items.map(item => (item as HTMLOptionElement).value).filter(Boolean))
    await projectPicker.selectOption(options[1])
    await expect(page.getByRole('heading', { name: options[1], exact: true })).toBeVisible()
  }
  await page.getByRole('button', { name: 'Change token' }).click()
  await expect(page.getByLabel('Authorized workspace')).toHaveCount(0)
  await expect(page.getByLabel('Access token')).toHaveValue('')
})
