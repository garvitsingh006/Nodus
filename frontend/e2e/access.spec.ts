import { expect, test } from '@playwright/test'

for (const width of [390, 1280]) {
  test('mocked HTTP scope: header, project switching, access entry and token change at width ' + width, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 850 })
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.route('**/api/v1/session', route => route.fulfill({
      json: { subject: 'nexus:person', tenant_id: 'nexus-tenant', permissions: ['READ_GRAPH'] },
    }))
    await page.route('**/api/v1/nexus/session', route => route.fulfill({
      json: {
        employee_id: 'person', display_name: 'Test person', company_id: 'nexus', role_code: 'INTERN',
        project_ids: ['A1', 'B2'], source_ids: [],
        capabilities: { request_access: true, review_access_requests: false },
      },
    }))
    await page.goto('/')
    await expect(page.getByText('NEXUS Technologies')).toHaveCount(0)
    await page.getByLabel('Access token').fill('4')
    await page.getByLabel('Access token').press('Enter')
    await expect(page.getByText('NEXUS Technologies')).toBeVisible()
    await expect(page.getByLabel('Authenticated role')).toHaveText('INTERN')
    await expect(page.getByRole('button', { name: 'Review access requests' })).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'A1', exact: true })).toBeVisible()
    await expect(page.getByRole('option')).toHaveText(['Choose an authorized project', 'A1', 'B2'])
    await page.getByLabel('What would you like to investigate?').fill('Private question')
    await page.getByLabel('Switch project').selectOption('B2')
    await expect(page.getByRole('heading', { name: 'B2', exact: true })).toBeVisible()
    await expect(page.getByLabel('What would you like to investigate?')).toHaveValue('')
    await page.getByRole('button', { name: 'Request additional access' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('complementary')).toContainText('No request has been submitted')
    await expect(page.getByRole('button', { name: 'Continue' })).toBeDisabled()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(await page.getByLabel('Authorized workspace').evaluate(el => getComputedStyle(el).animationName)).toBe('none')
    await page.screenshot({ path: testInfo.outputPath('workspace.png') })
    await page.getByRole('button', { name: 'Change token' }).click()
    await expect(page.getByLabel('Authorized workspace')).toHaveCount(0)
    await expect(page.getByLabel('Access token')).toHaveValue('')
    expect(await page.evaluate(() => localStorage.length)).toBe(0)
    expect(new URL(page.url()).pathname).toBe('/')
  })
}

test('mocked HTTP revocation removes the active project and all objective content', async ({ page }) => {
  let revoked = false
  await page.route('**/api/v1/session', route => route.fulfill({
    status: revoked ? 401 : 200,
    json: revoked ? { error: { message: 'Invalid credentials' } }
      : { subject: 'nexus:person', tenant_id: 'nexus-tenant', permissions: ['ADMIN'] },
  }))
  await page.route('**/api/v1/nexus/session', route => route.fulfill({
    json: {
      employee_id: 'person', display_name: 'Test admin', company_id: 'nexus', role_code: 'ADMIN',
      project_ids: ['A1', 'B2'], source_ids: [],
      capabilities: { review_access_requests: true, request_access: true },
    },
  }))
  await page.goto('/')
  await page.getByLabel('Access token').fill('opaque')
  await page.getByLabel('Access token').press('Enter')
  await expect(page.getByRole('button', { name: 'Review access requests' })).toBeVisible()
  await page.getByLabel('What would you like to investigate?').fill('Private objective')
  revoked = true
  await page.getByLabel('Switch project').selectOption('B2')
  await expect(page.getByRole('alert')).toContainText('not accepted or has expired')
  await expect(page.getByLabel('Authorized workspace')).toHaveCount(0)
  await expect(page.getByText('NEXUS Technologies')).toHaveCount(0)
})
